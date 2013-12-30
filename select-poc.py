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
        # WriteSelector. Note that close probably has similar
        # semantics and may include read.
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


def select(rlist, wlist, xlist, timeout=None):
    selector = _Select(rlist, wlist, xlist)
    return selector(timeout)


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
        self.selectors = set()
        self.read_adapter = None
        self.can_write = True

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
        bootstrap = Bootstrap().group(NIO_GROUP).channel(NioSocketChannel).handler(self.read_adapter)
        future = bootstrap.connect(host, port)
        self.channel = future.channel()
        self._handle_channel_future(future, "connect")

    def close(self):
        future = self.channel.close()
        self._handle_channel_future(future, "close")

    # FIXME handle shutdown - basically this should remove the read
    # handler for read shutdown and raise an exception on future
    # writes

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

class SSLInitializer(ChannelInitializer):

    def initChannel(self, ch):
        pipeline = ch.pipeline()
        engine = SSLContext.getDefault().createSSLEngine()
        engine.setUseClientMode(True);
        pipeline.addLast("ssl", SslHandler(engine))



def socket(family=None, type=None, proto=None):
    return _socketobject(family, type, proto)



def parse_http_response(data):
    # an obviously ridiculous client parse
    # look for RESPONSE\r\nX: Y\r\n, up to \r\n\r\n, which separates content
    # this is so ridiculous maybe i should do it incrementally FIXME
    try:
        i = data.index("\r\n")
        response = data[:i]
        data = data[i+1:]
        headers, content = data.split("\r\n\r\n")
    except ValueError:
        return None, {}, None
    headers = headers.split("\r\n")
    parsed_headers = {}
    for header in headers:
        i = header.index(":")
        key = header[:i]
        value = header[i+1:]
        parsed_headers[key] = value
    return response, parsed_headers, content


def test_blocking_client():
    # FIXME add a separate thread that selects on read, to verify this works as expected
    s = socket()
    s.connect(("www.python.org", 80))
    s.send("GET / HTTP/1.0\r\n\r\n")
    data = ""
    while True:  # FIXME terminate after a certain period of time
        chunk = s.recv(13)  # use a small prime to ensure that Netty's buffers REALLY get broken up
        print "Got this chunk:", repr(chunk)
        data += chunk
        response, headers, content = parse_http_response(data)
        if "Content-Length" in headers and int(headers["Content-Length"]) == len(content):
            break
    print "Completed reading"
    sys.stdout.write(data)
    s.close()  # blocks


def test_nonblocking_client():
    s = socket()
    s.setblocking(False)
    # FIXME does non-blocking version of connect not block on DNS lookup?
    # that's what I would presume for Netty...
    s.connect(("www.python.org", 80))
    print "connected"
    r, w, x = select([], [s], [])
    print "write select returned", r, w, x
    assert w == [s]
    print "writing"
    s.send("GET / HTTP/1.0\r\n\r\n")
    data = ""
    while True:  # FIXME terminate after a certain period of time
        r, w, x = select([s], [], [])  # verify we got s back
        print "read select returned", r, w, x
        assert r == [s]
        chunk = s.recv(13)  # use a small prime to ensure that Netty's buffers REALLY get broken up
        print "Got this chunk:", repr(chunk)
        data += chunk
        response, headers, content = parse_http_response(data)
        if "Content-Length" in headers and int(headers["Content-Length"]) == len(content):
            break
    print "Completed reading"
    sys.stdout.write(data)
    s.close()  # not blocking, what we should we test here? FIXME


def main():
    # FIXME run the "tests" with ssl
    # stop using python.org :) use a local CPython server instead for actual testing/compliance
    test_blocking_client()
    test_nonblocking_client()
    

if __name__ == "__main__":
    main()
