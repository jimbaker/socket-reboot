# implements a spike of socket, select, and ssl support
 
# FIXME add support for bind/listen/accept

import errno
import jarray
import sys
import time
from contextlib import contextmanager
from itertools import chain
from threading import Condition

from io.netty.bootstrap import Bootstrap, ChannelFactory, ServerBootstrap
from io.netty.buffer import PooledByteBufAllocator, Unpooled
from io.netty.channel import ChannelInboundHandlerAdapter, ChannelInitializer, ChannelOption
from io.netty.channel.nio import NioEventLoopGroup
from io.netty.channel.socket.nio import NioSocketChannel, NioServerSocketChannel
from java.util import NoSuchElementException
from java.util.concurrent import ArrayBlockingQueue, CopyOnWriteArrayList, LinkedBlockingQueue, TimeUnit


# FIXME fill in more constants

AF_UNSPEC, AF_INET, AF_INET6 = 0, 2, 23
SOCK_STREAM, SOCK_DGRAM = 1, 2
SHUT_RD, SHUT_WR = 1, 2
SHUT_RDWR = SHUT_RD | SHUT_WR
_GLOBAL_DEFAULT_TIMEOUT = object()

SOL_SOCKET = 0xFFFF
SO_ERROR = 4

# Specific constants for socket-reboot:

# Keep the highest possible precision for converting from Python's use
# of floating point for durations to Java's use of both a long
# duration and a specific unit, in this case TimeUnit.NANOSECONDS
_TO_NANOSECONDS = 1000000000

_PEER_CLOSED = object()

NIO_GROUP = NioEventLoopGroup()

def _shutdown_threadpool():
    print >> sys.stderr, "Shutting down thread pool..."
    # FIXME this timeout probably should be configurable; for client
    # usage that have completed this probably only produces scary
    # messages at worst, but TBD; in particular this may because we
    # are seeing closes both in SSL and at the socket level
    NIO_GROUP.shutdownGracefully(0, 100, TimeUnit.MILLISECONDS)
    print >> sys.stderr, "Shut down thread pool."

# Ensure deallocation of thread pool if PySystemState.cleanup is
# called; this includes in the event of sigterm
sys.registerCloser(_shutdown_threadpool)


class error(IOError): pass
class herror(error): pass
class gaierror(error): pass
class timeout(error): pass


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

    @contextmanager
    def _register_sockets(self, socks):
        socks = list(socks)
        for sock in socks:
            sock._register_selector(self)
        yield self
        for sock in socks:
            sock._unregister_selector(self)

    def __call__(self, timeout):
        with self.cv, self._register_sockets(chain(self.rlist, self.wlist, self.xlist)):
            while True:
                # Checking if sockets are ready (readable OR writable)
                # converts selection from detecting edges to detecting levels
                selected_rlist = set(sock for sock in self.rlist if sock._readable())
                selected_wlist = set(sock for sock in self.wlist if sock._writable())
                # FIXME add support for exceptions
                selected_xlist = []

                # As usual with condition variables, we need to ensure
                # there's not a spurious wakeup; this test also ensures
                # shortcircuiting if the socket was in fact ready for
                # reading/writing/exception before the select call
                if selected_rlist or selected_wlist:
                    return sorted(selected_rlist), sorted(selected_wlist), sorted(selected_xlist)
                self.cv.wait(timeout)


class PythonInboundHandler(ChannelInboundHandlerAdapter):

    def __init__(self, sock):
        self.sock = sock

    def channelActive(self, ctx):
        print "Channel is active {}".format(self.sock)
        self.sock._notify_selectors()
        ctx.fireChannelActive()

    def channelRead(self, ctx, msg):
        print "Channel read {}: {}".format(self.sock, msg)
        msg.retain()  # bump ref count so it can be used in the blocking queue
        self.sock.incoming.put(msg)
        self.sock._notify_selectors()
        ctx.fireChannelRead(msg)

    def channelWritabilityChanged(self, ctx):
        print "Ready for write {}".format(self.sock)
        self.sock._notify_selectors()
        ctx.fireChannelWritabilityChanged()

    def exceptionCaught(self, ctx, cause):
        print "Ready for exception {}: cause={}".format(self.sock, cause)
        self.sock._notify_selectors()
        ctx.fireExceptionCaught(cause) 


class ClientSocketHandler(ChannelInitializer):

    def __init__(self, parent_socket):
        self.parent_socket = parent_socket

    def initChannel(self, client_channel):
        client = _socketobject()
        client._init_client_mode(client_channel)
        self.parent_socket.client_queue.put(client)
        print "Notifing listeners of this server socket", self.parent_socket, "for", client
        self.parent_socket._notify_selectors()


# FIXME how much difference between server and peer sockets?

# shutdown should be straightforward - we get to choose what to do
# with a server socket in terms of accepting new connections

# FIXME raise exceptions for ops permitted on client socket, server socket
UNKNOWN_SOCKET, CLIENT_SOCKET, SERVER_SOCKET = range(3)


class _socketobject(object):

    def __init__(self, family=None, type=None, proto=None):
        # FIXME need something more vanilla to use for server socket,
        # client socket, and server-created client sockets

        # FOR NOW, assume socket.AF_INET, socket.SOCK_STREAM
        # change all fields below to _FIELD
        self.blocking = True
        self.timeout = None
        self.channel = None
        self.selectors = CopyOnWriteArrayList()
        self.bind_addr = None
        self.socket_type = UNKNOWN_SOCKET
        self.wrapper = None

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
                future.await(self.timeout * _TO_NANOSECONDS, TimeUnit.NANOSECONDS)
                return future
        else:
            def workaround_jython_bug_for_bound_methods(x):
                # print "Notifying selectors", self
                self._notify_selectors()

            future.addListener(workaround_jython_bug_for_bound_methods)
            return future

    def setblocking(self, mode):
        self.blocking = mode

    def settimeout(self, timeout):
        if not timeout:
            self.blocking = False
        else:
            self.timeout = timeout

    def bind(self, address):
        # Netty 4 supports binding a socket to multiple addresses;
        # apparently this is the not the case for C API sockets

        # FIXME this should resolve to a host, port or possibly an
        # inet addr, port - do some parsing to ensure the case
        self.bind_addr = address


    # CLIENT METHODS
    # Calling connect/connect_ex means this is a client socket; these
    # in turn use _connect, which uses Bootstrap, not ServerBootstrap

    def _init_client_mode(self, channel=None):
        # this is client socket specific 
        self.socket_type = CLIENT_SOCKET
        self.incoming = LinkedBlockingQueue()  # list of read buffers
        self.incoming_head = None  # allows msg buffers to be broken up
        self.python_inbound_handler = None
        self.can_write = True
        self.connect_handlers = []
        self.peer_closed = False
        self.connected = False
        if channel:
            # add support for SSL somehow FIXME
            self.channel = channel
            self.python_inbound_handler = PythonInboundHandler(self)
            self.connect_handlers = [self.python_inbound_handler]
            self._post_connect()

    def _connect(self, addr):
        print "Begin _connect"
        self._init_client_mode()
        self.connected = True
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

        def completed(f):
            self._notify_selectors()
            print "Connection future - connection completed", f
        
        host, port = addr
        future = bootstrap.connect(host, port)
        future.addListener(completed)
        self._handle_channel_future(future, "connect")
        self.channel = future.channel()
        print "Completed _connect on {}".format(self)

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
        print "Completed connect {} to {}".format(self, addr)

    def connect_ex(self, addr):
        self.connect(addr)
        if self.blocking:
            return errno.EISCONN
        else:
            return errno.EINPROGRESS


    # SERVER METHODS
    # Calling listen means this is a server socket

    def listen(self, backlog):
        self.socket_type = SERVER_SOCKET

        b = ServerBootstrap()
        b.group(NIO_GROUP)
        b.channel(NioServerSocketChannel)
        b.option(ChannelOption.SO_BACKLOG, backlog)
        # FIXME pass through child options from self; note that C API sockets do not distinguish
        # EXAMPLE - b.childOption(ChannelOption.SO_KEEPALIVE, True)

        # FIXME per http://stackoverflow.com/questions/9774023/netty-throttling-accept-on-boss-thread,
        # should set a parentHandler to ensure throttling to avoid denial of service attacks against this layer;
        # it's up to using Python code to do this, but at the very least there should be some sort of blocking
        # to ensure we don't exceed the desired backlog in this chunk of code;
        # right now, assumption is a ArrayBlockingQueue of sufficient size should suffice instead
        self.client_queue = ArrayBlockingQueue(backlog)

        # FIXME this should queue up sockets that are wrapped accordingly;
        # in particular they should be wrapped SSLSocket objects (inheriting SSLEngine settings) 
        b.childHandler(ClientSocketHandler(self))

        # returns a ChannelFuture, but regardless for blocking/nonblocking, return immediately
        # FIXME what if bind_addr is not set? should use ephemeral
        b.bind(self.bind_addr[1])  # FIXME for now just select the port

    def accept(self):
        s = self.client_queue.take()
        return s, s.getpeername()
                    
    
    # GENERAL METHODS
                                             
    def close(self):
        future = self.channel.close()
        self._handle_channel_future(future, "close")

    def shutdown(self, how):
        if how & SHUT_RD:
            self.channel.pipeline().remove(self.python_inbound_handler)
        if how & SHUT_WR:
            self.can_write = False
            
    def _readable(self):
        if self.socket_type == CLIENT_SOCKET:
            return ((self.incoming_head is not None and self.incoming_head.readableBytes()) or
                    self.incoming.peek())
        elif self.socket_type == SERVER_SOCKET:
            return bool(self.client_queue.peek())
        else:
            return False

    def _writable(self):
        return self.channel.isActive() and self.channel.isWritable()

    def send(self, data):
        data = str(data)  # FIXME temporary fix if data is of type buffer
        print "Sending data <<<{}>>>".format(data)
        if not self.can_write:
            raise Exception("Cannot write to closed socket")  # FIXME use actual exception
        future = self.channel.writeAndFlush(Unpooled.wrappedBuffer(data))
        self._handle_channel_future(future, "send")
        # FIXME are we sure we are going to be able to send this much data, especially async?
        return len(data)
    
    sendall = send   # see note above!

    def _get_incoming_msg(self):
        if self.incoming_head is None:
            if self.blocking:
                if self.timeout is None:
                    self.incoming_head = self.incoming.take()
                else:
                    self.incoming_head = self.incoming.poll(self.timeout * _TO_NANOSECONDS, TimeUnit.NANOSECONDS)
            else:
                self.incoming_head = self.incoming.poll()  # Could be None

        # Only return _PEER_CLOSED once
        msg = self.incoming_head
        if msg is _PEER_CLOSED:
            self.incoming_head = None
        return msg

    def recv(self, bufsize, flags=0):
        # For obvious reasons, concurrent reads on the same socket
        # have to be locked; I don't believe it is the job of recv to
        # do this; in particular this is the policy of SocketChannel,
        # which underlies Netty's support for such channels.
        self._may_be_readable = False
        msg = self._get_incoming_msg()
        if msg is None:
            return None
        elif msg is _PEER_CLOSED:
            return ""
        msg_length = msg.readableBytes()
        buf = jarray.zeros(min(msg_length, bufsize), "b")
        msg.readBytes(buf)
        if msg.readableBytes() == 0:
            msg.release()  # return msg ByteBuf back to Netty's pool
            self.incoming_head = None
        return buf.tostring()

    def fileno(self):
        return self

    def getsockopt(self, level, option):
        return 0

    def getpeername(self):
        remote_addr = self.channel.remoteAddress()
        return remote_addr.getHostString(), remote_addr.getPort()



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



def create_connection(address, timeout=_GLOBAL_DEFAULT_TIMEOUT,
                      source_address=None):
    """Connect to *address* and return the socket object.

    Convenience function.  Connect to *address* (a 2-tuple ``(host,
    port)``) and return the socket object.  Passing the optional
    *timeout* parameter will set the timeout on the socket instance
    before attempting to connect.  If no *timeout* is supplied, the
    global default timeout setting returned by :func:`getdefaulttimeout`
    is used.  If *source_address* is set it must be a tuple of (host, port)
    for the socket to bind as a source address before making the connection.
    An host of '' or port 0 tells the OS to use the default.
    """

    host, port = address
    err = None
    for res in getaddrinfo(host, port, 0, SOCK_STREAM):
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket(af, socktype, proto)
            if timeout is not _GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock

        except error as _:
            err = _
            if sock is not None:
                sock.close()

    if err is not None:
        raise err
    else:
        raise error("getaddrinfo returns an empty list")



