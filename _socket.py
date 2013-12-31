# implements a spike of socket and select;
# it should be straighforward to add support of ssl.wrap, ssl.unwrap

import jarray
import sys
import time
from itertools import chain
from threading import Condition

from io.netty.bootstrap import Bootstrap, ChannelFactory
from io.netty.buffer import PooledByteBufAllocator, Unpooled
from io.netty.channel import ChannelInboundHandlerAdapter, ChannelInitializer, ChannelOption
from io.netty.channel.nio import NioEventLoopGroup
from io.netty.channel.socket.nio import NioSocketChannel
from io.netty.handler.ssl import SslHandler
from javax.net.ssl import SSLContext
from java.util import NoSuchElementException
from java.util.concurrent import TimeUnit
from java.util.concurrent import LinkedBlockingQueue


NIO_GROUP = NioEventLoopGroup()

def _shutdown_threadpool():
    print >> sys.stderr, "Shutting down thread pool..."
    NIO_GROUP.shutdown()
    print >> sys.stderr, "Shut down thread pool."

# Ensure deallocation of thread pool if PySystemState.cleanup is
# called; this includes in the event of sigterm
sys.registerCloser(_shutdown_threadpool)

# Keep the highest possible precision from converting from Python's
# use of floating point for time intervals to Java's use of a long and
# a specific unit, in this case TimeUnit.NANOSECONDS
TO_NANOSECONDS = 1000000000


SHUT_RD, SHUT_WR = 1, 2
SHUT_RDWR = SHUT_RD | SHUT_WR
CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED = range(3)


_END_RECV_DATA = object()



class ReadAdapter(ChannelInboundHandlerAdapter):

    def __init__(self, sock):
        self.sock = sock

    def channelRead(self, ctx, msg):
        # put msg buffs on incoming as they come in;
        # only guarantee on recv that we receive at most bufferlen;
        print "Got data", self.sock
        msg.retain()  # bump ref count so it can be used in the blocking queue
        self.sock.incoming.put(msg)
        ctx.fireChannelRead(msg)


class ReadSelector(ChannelInboundHandlerAdapter):

    def __init__(self, selector, sock):
        self.selector = selector
        self.sock = sock

    def channelRead(self, ctx, msg):
        print "Ready for read", self.sock
        self.selector.selected_rlist.add(self.sock)
        cv = self.selector.cv
        with cv:
            cv.notify()
        ctx.fireChannelRead(msg)


class WriteSelector(ChannelInboundHandlerAdapter):

    def __init__(self, selector, sock):
        self.selector = selector
        self.sock = sock

    def channelWritabilityChanged(self, ctx):
        print "Ready for write", self.sock
        self.selector.selected_wlist.add(self.sock)
        cv = self.selector.cv
        with cv:
            cv.notify()
        ctx.fireChannelWritabilityChanged()


class ExceptionSelector(ChannelInboundHandlerAdapter):

    def __init__(self, selector, sock):
        self.selector = selector
        self.sock = sock

    def exceptionCaught(self, ctx, cause):
        print "Ready for exception", self.sock, cause
        self.selector.selected_xlist.add(self.sock)
        cv = self.selector.cv
        with cv:
            cv.notify()
        ctx.fireExceptionCaught(cause) 


class _Select(object):

    def __init__(self, rlist, wlist, xlist):
        self.cv = Condition()

        # Checking if sockets are ready (readable OR writable)
        # converts selection from detecting edges to detecting levels;
        # Doing this check here will have the effect of immediately
        # exiting the select loop below
        self.selected_rlist = set(sock for sock in rlist if sock._readable())
        self.selected_wlist = set(sock for sock in wlist if sock._writable())

        # Not clear how to do level triggers on xlist, since it seems
        # to be both poorly defined AND rarely used
        self.selected_xlist = set()

        # Connections can tell us we are writable, but they use a
        # separate notification mechanism. We may not care, so keep
        # intent separate from registration step below of
        # WriteSelector. Note that peer close has similar semantics
        # for read notification.
        self.rlist = set(rlist)
        self.wlist = set(wlist)

        self.registered_rlist = [sock._register_handler(ReadSelector, self) for sock in rlist]
        self.registered_wlist = [sock._register_handler(WriteSelector, self) for sock in wlist]
        self.registered_xlist = [sock._register_handler(ExceptionSelector, self) for sock in xlist]

    def unregister(self):
        for handler in chain(self.registered_rlist, self.registered_wlist, self.registered_xlist):
            sock = handler.sock
            sock._unregister_handler(handler)

    def __call__(self, timeout):
        with self.cv:
            # As usual with condition variables, we need to ensure
            # there's not a spurious wakeup; this test also helps
            # shortcircuit if the socket was in fact ready before the
            # select call
            while not (self.selected_rlist or self.selected_wlist or self.selected_xlist):
                print "waiting on", self.registered_rlist, self.registered_wlist, self.registered_xlist
                print "selected  ", self.selected_rlist, self.selected_wlist, self.selected_xlist
                self.cv.wait(timeout)
            # Need to be in the context of the condition variable to avoid racing on unregistration
            self.unregister()

        print "selected 2", self.selected_rlist, self.selected_wlist, self.selected_xlist
        return sorted(self.selected_rlist), sorted(self.selected_wlist), sorted(self.selected_xlist)




# FIXME how much difference between server and peer sockets?

# shutdown should be straightforward - we get to choose what to do
# with a server socket in terms of accepting new connections


class _socketobject(object):

    def __init__(self, family=None, type=None, proto=None):
        # FOR NOW, assume socket.AF_INET, socket.SOCK_STREAM
        # change all fields below to _FIELD
        self.blocking = True
        self.timeout = None
        self.channel = None
        self.incoming = LinkedBlockingQueue()  # list of read buffers
        self.incoming_head = None  # allows msg buffers to be broken up
        self.selectors = set()  # weak consistency in iteration is probably OK
        self.read_adapter = None
        self.can_write = True
        self.connect_handlers = []

    def _register_handler(self, handler_class, selector):
        handler = handler_class(selector, self)
        self.channel.pipeline().addLast(handler)
        self.selectors.add(selector)
        return handler

    def _unregister_handler(self, handler):
        self.channel.pipeline().remove(handler)
        self.selectors.remove(handler.selector)

    def _handle_channel_future(self, future, reason):
        if self.blocking:
            if self.timeout is None:
                return future.sync()
            else:
                future.await(self.timeout * TO_NANOSECONDS, TimeUnit.NANOSECONDS)
                return future
        else:
            # need to know if we have any registered interest - 
            # this should signal rlist/wlist if successful, otherwise xlist,
            # through selectors above

            def notify_selectors(f):
                for selector in self.selectors:
                    print "Selector", reason, selector.__dict__
                    with selector.cv:
                        if self._writable() and self in selector.wlist:
                            selector.selected_wlist.add(self)
                            print "Notifying connection has happened", reason, selector
                            selector.cv.notify()

            future.addListener(notify_selectors)
            return future

    def setblocking(self, mode):
        self.blocking = mode

    def settimeout(self, timeout):
        if not timeout:
            self.blocking = False
        else:
            self.timeout = timeout

    def connect(self, addr):
        host, port = addr
        self.read_adapter = ReadAdapter(self)
        bootstrap = Bootstrap().group(NIO_GROUP).channel(NioSocketChannel)
        if self.connect_handlers:
            for handler in self.connect_handlers:
                print "Adding connect handler", handler
                bootstrap.handler(handler)
        else:
            bootstrap.handler(self.read_adapter)
        # FIXME also support any options here
        future = bootstrap.connect(host, port)
        self.channel = future.channel()
        self._handle_channel_future(future, "connect")
        if self.connect_handlers:
            self.channel.pipeline().addLast(self.read_adapter)
        
        def say_im_closed(x):
            # FIXME can x be an exception we would like to raise in the recv?
            print "My channel is closed, it's pointless to keep reading", x
            self.incoming.put(_END_RECV_DATA)
            for selector in self.selectors:
                with selector.cv:
                    if self in selector.rlist:
                        selector.selected_rlist.add(self)
                        print "Notifying connection close by peer has happened", selector
                        selector.cv.notify()

        self.channel.closeFuture().addListener(say_im_closed)

    def close(self):
        future = self.channel.close()
        self._handle_channel_future(future, "close")

    def shutdown(self, how):
        if how & SHUT_RD:
            self.channel.pipeline().remove(self.read_adapter)
        if how & SHUT_WR:
            self.can_write = False

    def send(self, data):
        if not self.can_write:
            raise Exception("Cannot write to closed socket")  # FIXME use actual exception
        future = self.channel.writeAndFlush(Unpooled.wrappedBuffer(data))
        self._handle_channel_future(future, "send")
    
    def _get_incoming_msg(self):
        if self.incoming_head is not None:
            return
        if self.blocking:
            if self.timeout is None:
                self.incoming_head = self.incoming.take()
            else:
                self.incoming_head = self.incoming.poll(self.timeout * TO_NANOSECONDS, TimeUnit.NANOSECONDS)
        else:
            self.incoming_head = self.incoming.poll()
        return

    def _readable(self):
        return ((self.incoming_head is not None and self.incoming_head.readableBytes()) or
                self.incoming.poll())

    def _writable(self):
        return self.channel.isActive() and self.channel.isWritable()

    def recv(self, bufsize, flags=0):
        # For obvious reasons, concurrent reads on the same socket
        # have to be locked; I don't believe it is the job of recv to
        # do this; in particular this is the policy of SocketChannel,
        # which underlies Netty's support for such channels.
        self._get_incoming_msg()
        msg = self.incoming_head
        if msg is None:
            return None
        elif msg is _END_RECV_DATA:
            self.incoming_head = None
            return ""
        msg_length = msg.readableBytes()
        buf = jarray.zeros(min(msg_length, bufsize), "b")
        msg.readBytes(buf)
        if msg.readableBytes() == 0:
            msg.release()  # return msg ByteBuf back to Netty's pool
            self.incoming_head = None
        return buf.tostring()


# ssl.wrap_socket essentially creates a SSLEngine instance, then adds the
# handler; the engine needs to be kept around for the duration of the wrap for
# later potential usage, such as getting peer certificates from the
# handshake

# An initializer like this is necessary for any peer sockets built by a server socket;
# otherwise presumably we can just add to a peer socket in client mode. Must try now!

class SSLInitializer(ChannelInitializer):

    def __init__(self, ssl_handler):
        self.ssl_handler = ssl_handler

    def initChannel(self, ch):
        pipeline = ch.pipeline()
        pipeline.addLast("ssl", self.ssl_handler) 



# Need a delegation wrapper just in case users of this class want to
# access certs and other info from the underlying SSLEngine

class SSLSocket(object):
    
    def __init__(self, sock):
        self.sock = sock
        self.engine = SSLContext.getDefault().createSSLEngine()
        self.engine.setUseClientMode(True)  # FIXME honor wrap_socket option for this
        self.ssl_handler = SslHandler(self.engine)

        def handshake_step(x):
            print "Handshaking", x
            for selector in self.sock.selectors:
                with selector.cv:
                    if self in selector.wlist:
                        selector.selected_wlist.add(self)
                        print "Notifying connection we can write", selector
                        selector.cv.notify()

        self.ssl_handler.handshakeFuture().addListener(handshake_step)

        # if already connected, do this
        # self.sock.channel.pipeline().addLast("ssl", SslHandler(self.engine))
        # else add the SSLInitializer:
        self.sock.connect_handlers.append(SSLInitializer(self.ssl_handler))

    def connect(self, addr):
        print "Connecting SSL socket"
        self.sock.connect(addr)
        print "Connected"

    def send(self, data):
        print "Sending data over SSL socket..."
        self.sock.send(data)
        print "Sent data"

    def recv(self, bufsize, flags=0):
        return self.sock.recv(bufsize, flags)
        
    def close(self):
        self.sock.close()

    def shutdown(self, how):
        self.sock.shutdown(how)

    def _readable(self):
        return self.sock._readable()

    def _writable(self):
        return self.sock._writable()

    def _register_handler(self, handler_class, selector):
        return self.sock._register_handler(handler_class, selector)

    def _unregister_handler(self, handler):
        return self.sock._unregister_handler(handler)


# helpful advice for being able to manage ca_certs outside of Java's keystore
# specifically the example ReloadableX509TrustManager
# http://jcalcote.wordpress.com/2010/06/22/managing-a-dynamic-java-trust-store/

# in the case of http://docs.python.org/2/library/ssl.html#ssl.CERT_REQUIRED

# http://docs.python.org/2/library/ssl.html#ssl.CERT_NONE
# https://github.com/rackerlabs/romper/blob/master/romper/trust.py#L15
#
# it looks like CERT_OPTIONAL simply validates certificates if
# provided, probably something in checkServerTrusted - maybe a None
# arg? need to verify as usual with a real system... :)

# http://alesaudate.wordpress.com/2010/08/09/how-to-dynamically-select-a-certificate-alias-when-invoking-web-services/
# is somewhat relevant for managing the keyfile, certfile


# EXPORTED constructors

def socket(family=None, type=None, proto=None):
    return _socketobject(family, type, proto)


def select(rlist, wlist, xlist, timeout=None):
    return _Select(rlist, wlist, xlist)(timeout)


def wrap_socket(sock, keyfile=None, certfile=None, server_side=False, cert_reqs=CERT_NONE,
                ssl_version=None, ca_certs=None, do_handshake_on_connect=True,
                suppress_ragged_eofs=True, ciphers=None):
    # instantiates a SSLEngine, with the following set:
    # do_handshake_on_connect is always True, since it's always nonblocking... verify this works with Python code
    # suppress_ragged_eofs - presumably this is an exception we can detect in Netty, the underlying SSLEngine certainly does
    # ssl_version - use SSLEngine.setEnabledProtocols(java.lang.String[])
    # ciphers - SSLEngine.setEnabledCipherSuites(String[] suites)
    return SSLSocket(sock)


def unwrap_socket(sock):
    pass
