# implements a spike of socket, select, and ssl support
 

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
from java.util.concurrent import CopyOnWriteArrayList, LinkedBlockingQueue, TimeUnit


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


_PEER_CLOSED = object()




class _Select(object):

    def __init__(self, rlist, wlist, xlist):
        self.cv = Condition()
        self.rlist = frozenset(rlist)
        self.wlist = frozenset(wlist)
        self.xlist = frozenset(xlist)

    def notify(self):
        with self.cv:
            self.cv.notify()

    def __str__(self):
        return "_Select(r={},w={},x={})".format(list(self.rlist), list(self.wlist), list(self.xlist))

    def __call__(self, timeout):
        with self.cv:
            for sock in chain(self.rlist, self.wlist, self.xlist):
                sock._register_selector(self)

            # As usual with condition variables, we need to ensure
            # there's not a spurious wakeup; this test also helps
            # shortcircuit if the socket was in fact ready before the
            # select call
            while True:
                # Checking if sockets are ready (readable OR writable)
                # converts selection from detecting edges to detecting levels
                selected_rlist = set(sock for sock in self.rlist if sock._readable())
                selected_wlist = set(sock for sock in self.wlist if sock._writable())
                # FIXME add support for exceptions
                selected_xlist = []
                #print "Checking levels for", self
                if selected_rlist or selected_wlist:
                    break
                #print "Waiting on", self
                self.cv.wait(timeout)
                #print "Completed waiting for", self

            for sock in chain(self.rlist, self.wlist, self.xlist):
                sock._unregister_selector(self)
            return sorted(selected_rlist), sorted(selected_wlist), sorted(selected_xlist)


class PythonInboundHandler(ChannelInboundHandlerAdapter):

    def __init__(self, sock):
        self.sock = sock

    def channelRead(self, ctx, msg):
        # put msg buffs on incoming as they come in;
        # only guarantee on recv that we receive at most bufferlen;
        self.sock.data_available = True
        print "Ready for read", self.sock, msg
        msg.retain()  # bump ref count so it can be used in the blocking queue
        self.sock.incoming.put(msg)
        self.sock.data_available = False
        self.sock._notify_selectors()
        ctx.fireChannelRead(msg)

    def channelWritabilityChanged(self, ctx):
        print "Ready for write", self.sock
        self.sock._notify_selectors()
        ctx.fireChannelWritabilityChanged()

    def exceptionCaught(self, ctx, cause):
        print "Ready for exception", self.sock, cause
        self.sock._notify_selectors()
        ctx.fireExceptionCaught(cause) 


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
        self.selectors = CopyOnWriteArrayList()
        self.python_inbound_handler = None
        self.can_write = True
        self.connect_handlers = []
        self.peer_closed = False

    def _register_selector(self, selector):
        self.selectors.addIfAbsent(selector)

    def _unregister_selector(self, selector):
        return self.selectors.remove(selector)

    def _notify_selectors(self):
        for selector in self.selectors:
            selector.notify()

    def _handle_channel_future(self, future, reason):
        if self.blocking:
            if self.timeout is None:
                return future.sync()
            else:
                future.await(self.timeout * TO_NANOSECONDS, TimeUnit.NANOSECONDS)
                return future
        else:
            def workaround_jython_bug(x):
                # print "Notifying selectors", self
                self._notify_selectors()

            future.addListener(workaround_jython_bug)
            return future

    def setblocking(self, mode):
        self.blocking = mode

    def settimeout(self, timeout):
        if not timeout:
            self.blocking = False
        else:
            self.timeout = timeout

    def _connect(self, addr):
        host, port = addr
        self.python_inbound_handler = PythonInboundHandler(self)
        bootstrap = Bootstrap().group(NIO_GROUP).channel(NioSocketChannel)

        # FIXME really this is just for SSL handling
        if self.connect_handlers:
            for handler in self.connect_handlers:
                print "Adding connect handler", handler
                bootstrap.handler(handler)
        else:
            print "Adding read adapter", self.python_inbound_handler
            bootstrap.handler(self.python_inbound_handler)
        # FIXME also support any options here
        future = bootstrap.connect(host, port)
        self._handle_channel_future(future, "connect")
        self.channel = future.channel()

    def _post_connect(self):
        # Post-connect step is necessary to handle SSL setup,
        # otherwise the read adapter can race in seeing encrypted
        # messages from the peer
        if self.connect_handlers:
            print "Adding read adapter", self.python_inbound_handler
            self.channel.pipeline().addLast(self.python_inbound_handler)
        
        def peer_closed(x):
            print "Peer closed channel {} {}".format(self, x)
            self.incoming.put(_PEER_CLOSED)
            self._notify_selectors()

        self.channel.closeFuture().addListener(peer_closed)

    def connect(self, addr):
        # Unwrapped sockets can immediately perform the post-connect step
        self._connect(addr)
        self._post_connect()

    def close(self):
        future = self.channel.close()
        self._handle_channel_future(future, "close")

    def shutdown(self, how):
        if how & SHUT_RD:
            self.channel.pipeline().remove(self.python_inbound_handler)
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
                self.incoming.peek())

    def _writable(self):
        return self.channel.isActive() and self.channel.isWritable()

    def recv(self, bufsize, flags=0):
        # For obvious reasons, concurrent reads on the same socket
        # have to be locked; I don't believe it is the job of recv to
        # do this; in particular this is the policy of SocketChannel,
        # which underlies Netty's support for such channels.
        self._get_incoming_msg()
        msg = self.incoming_head
        #print "recv msg=", msg
        if msg is None:
            return None
        elif msg is _PEER_CLOSED:
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
# FIXME we should use ABC support to make this a subtype of the socket class

class SSLSocket(object):
    
    def __init__(self, sock):
        self.sock = sock
        self.engine = SSLContext.getDefault().createSSLEngine()
        self.engine.setUseClientMode(True)  # FIXME honor wrap_socket option for this
        self.ssl_handler = SslHandler(self.engine)
        self.ssl_writable = False

        def handshake_step(x):
            print "Handshaking result", x
            self.sock._post_connect()
            self.ssl_writable = True
            self.sock._notify_selectors()

        self.ssl_handler.handshakeFuture().addListener(handshake_step)

        # FIXME presumably if already connected, do this:
        # self.sock.channel.pipeline().addFirst("ssl", SslHandler(self.engine)), or maybe addBefore the python_inbound_handler
        self.sock.connect_handlers.append(SSLInitializer(self.ssl_handler))

    def connect(self, addr):
        print "Connecting SSL socket"
        self.sock._connect(addr)

    def send(self, data):
        print "Sending data over SSL socket..."
        self.ssl_writable = False  # special writability step after negotiation
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
        return self.ssl_writable or self.sock._writable()

    def _register_selector(self, selector):
        self.sock._register_selector(selector)

    def _unregister_selector(self, selector):
        return self.sock._unregister_selector(selector)

    def _notify_selectors(self):
        self.sock._notify_selectors()



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
