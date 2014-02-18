Motivation
==========

The socket, select, and ssl modules provide the core, low-level
networking semantics for Python. However, implementing these modules
in their entirety is difficult for Jython given some differences
between these APIs and the underlying Java platform. This is
especially true with respect to supporting IO completions on
nonblocking sockets (using either select or poll) **and** SSL
handshaking.

This spike demonstrates with **working code** that for Jython 2.7 we
can use Netty 4 to readily implement these core semantics, with minor
exceptions. In particular, this spike mostly looks at the implications
of implementing the select function, which is both minimally
documented and tested in Python. The other major element addressed is
the management of Netty's thread pool, especially with respect to
cleaning up a `PySystemState`; such management is quite easy to
implement in practice.


What was not covered
====================

This has intentionally been a limited [spike][], which is a
generally good thing to do with spikes.

In particular, the milestone for releasing Jython 2.7 beta 2 is to
support pip, which in turn requires support for requests, which in
turn is blocking on nonblocking SSL support for peer (client) sockets.

FIXME server sockets are supported, blocking/nonblocking/ssl/non ssl

FIXME plan to add some limited functional tests - evolving into a design doc
with code to be copied over soon

So this means there's no support for server sockets in this spike;
however I did some quick analysis of the Netty docs that suggests that
such [support](#server-socket-support) should be straightforward. As a
fallback - although this should not be necessary - there's an
existing solution in Jython trunk for server sockets without SSL,
blocking or not.

Supporting datagram (UDP) sockets has a similar fallback.

Of the remaining issues to be addressed, **still to be done is support
for exceptions.** In particular, it is not clear that calling `select`
with an list of sockets in exception is a well-defined, cross-platform
concept for even CPython. So while appropriate mapping of exceptions
for socket methods may be a potential risk, it is likely mitigated by
the lack of good cross platform definition of exceptions, say Unix vs
Windows.


Mapping Python socket semantics to Java
=======================================

Common usage of blocking sockets in Python is quite similar to what
Java readily supports with blocking sockets; consequently Jython has
for some time supported such sockets.  The difficulty arises with
respect to nonblocking sockets as used with the select module:

* Java's `Selectable` sockets (`SocketChannel`, `ServerSocketChannel`)
  have a different API than blocking sockets (`Socket`,
  `ServerSocket`). Because they are different implementations, it is
  not possible in Java to switch from blocking to
  nonblocking. Unfortunately, it seems to be a fairly common pattern
  for Python code to switch from blocking to nonblocking support,
  generally to avoid such issues as managing SSL handshaking
  asynchronously. (I'm not aware of code going the opposite
  direction.)

* No direct support for working with SSL in a nonblocking way. Java
  supports `SSLSocket` and `SSLServerSocket`, but not a
  `SocketChannel` variant. Instead, one has to work explicitly with
  the SSL handshaking steps using `SSLEngine`, which does not
  correspond to Python semantics (except perhaps with respect to
  observability of the completions of steps by the `select`
  function`).

The current implementation in Jython 2.5 (and current Jython trunk)
does have a [socket][2.5 socket] and [select][2.5 select]
implementation that works around the first problem, but at the cost of
significant complexity in additional corner cases and known warts.

So unification will be a big win.

Netty 4 allows us to sidestep both these issues seen previously in
Jython 2.5, add SSL, and achieve this desired unification. We make the
following observations in this spike:

* As should be expected, we can layer blocking functionality on a
  common nonblocking implementation. But this is especially easy to do
  in Netty, where making nonblocking ops be blocking is simply a
  matter of waiting on a `ChannelFuture`, with possible timeout.

* Netty in particular mostly eliminates the need to manage any aspect
  of SSL, excepting its setup in a channel pipeline.

* The key challenge then is to implement IO completion support.


Implementing IO completions
===========================

First, Netty's completion model is generally **edge-based**, in other
words some change has happened (perhaps). In contrast, Python's
`select` and `poll` are **level-based**: a socket is now ready to
read, for example. However, it is easy to layer a level-based model on
an edge-based model: simply add a test - *is it ready to read?* -
after getting a notification.

(Note that such testing can potentially race if multiple threads are
say selecting and reading on the same socket. Sockets in general are
not threadsafe this way, however.)

Such ready predicates work well with a model of using condition
variables for such notifications, due to inherent quality of CVs that
they may experience spurious wakeups.

With that in mind, there are two types of IO completions in Netty:

* `ChannelFuture` - such futures mostly correspond to a discrete state
  transition of the channel itself - connected to peer (implies DNS
  lookup, if necessary); SSL has handshaked; a peer socket has closed
  its side of the channel. Futures are also used for write completions
  (not necessarily at peer, of course).

* Channel handlers - Netty supports a pipeline for each channel,
  divided into inbound and outbound, where handlers on each side of
  the pipeline handle incoming/outgoing events. In particular, we are
  interested in `ChannelInboundHandler` and its events. Some of these
  events carry messages, as wrapped in Netty's `ByteBuf`. For example,
  in the case of `channelRead`, we really do know it's ready to read
  since we have a message to read.

The reason for distinction in Netty is that it's both incredibly
useful to have explicit pipelines; it also avoids additional
synchronization overhead when going from one handler to another; and
it works well with the ref counting used by `ByteBuf`. For our
purposes, it really doesn't matter that this division exists.


Specific mapping
----------------

Rather than directly map to be ready-to-read/ready-to-write/error
state, notify condition variable for selector, then test levels in the
selector:

Edge event                   | Notes
---------------------------- | -----
Bootstrap.connect            |
socket.close                 |
CIH.channelRead              |
socket.send                  | Is this really an edge event?
CIH.exceptionCaught          |
CIH.isWritabilityChanged     |
SocketChannel.closeFuture    | recv will see an empty string (sentinel for peer close)
SSLHandler.handshakeFuture   | Also initiates post-connect phase that sets up PythonInboundHandler

(CIH = ChannelInboundHandler)

Notification from socket to selector is straightforward. Each socket
manages a [list of listeners](#managing-listeners) (selector,
registered poll objects) using a `CopyOnWriteArrayList`; normally we
would expect a size of no more than 1, but Python select/poll
semantics do allow multiple listeners.

The [usual pattern](#condition-variable) of working with a condition
variable is further extended in the case of the select mechanism,
because we need to explicitly register and unregister the selector for
each socket. To avoid races of unregistration and notification, this
should be always nested in the acquisition of condition variable and
the unregistration always performed upon exit. Having
`register_selectors` be a context manager ensures this is the case:

````python
with self.cv, self._register_sockets(chain(self.rlist, self.wlist, self.xlist)):
  while True:
    selected_rlist = set(sock for sock in self.rlist if sock._readable())
    selected_wlist = set(sock for sock in self.wlist if sock._writable())
    selected_xlist = []  # FIXME need to determine exception support
    if selected_rlist or selected_wlist:
      return sorted(selected_rlist), sorted(selected_wlist), sorted(selected_xlist)
    self.cv.wait(timeout)
````


SSL handshaking and events
--------------------------

To avoid races with SSL handshaking, it is important to add the
`PythonInboundHandler` **after** handshaking completes. I need some
actual experience here, but I believe it's not necessary to manipulate
the pipeline again in the case of SSL renegotiation - it seems to be
only a race of reading the first handshaking (encrypted) message. To
solve this, the listener on the handshake does this setup in a
post_connect step.

Note that we could potentially observe the handshake process by seeing
`SSL_ERROR_WANT_READ` and `SSL_ERROR_WANT_WRITE` exceptions in the
`SSLSocket.do_handshake()` method, then requiring the user to call
select, but this is pointless. The documentation of `do_handshake()`
makes this clear:

> Perform a TLS/SSL handshake. If this is used with a non-blocking
  socket, it may raise SSLError with an arg[0] of SSL_ERROR_WANT_READ
  or SSL_ERROR_WANT_WRITE, in which case it must be called again until
  it completes successfully.

(http://docs.python.org/2/library/ssl.html#ssl.SSLSocket.do_handshake)

The operative term is *may*, so we do not have to support this
behavior and just let the threadpool do it.

This is seen in the example in the docs, which simulates blocking
behavior on nonblocking SSL sockets:

````python
while True:
    try:
        s.do_handshake()
        break
    except ssl.SSLError as err:
        if err.args[0] == ssl.SSL_ERROR_WANT_READ:
            select.select([s], [], [])
        elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
            select.select([], [s], [])
        else:
            raise
````

Such code will never see `ssl.SSL_ERROR_WANT_READ` and
`ssl.SSL_ERROR_WANT_WRITE` exceptions, but will continue to be
correct.

Although not investigated in this spike, implementing
`ssl.unwrap_socket` should be easy given that a `SslHandler` can be
added/removed from the channel's pipeline at any time.


Implementing `poll`
-------------------

The select module defines a `poll` object, which supports more
efficient completions than the `select` function. This spike didn't
look at specific coding of `poll`, but it appears to be
straightforward to implement:

* `poll.register` registers this selector for a given socket. The
  selector in turn is edge notified and checks for any registered
  levels. Unlike `select`, we need a selector *for each* socket to
  maintain O(1) behavior.

* Use a blocking queue to collect any registered polling events of the
  desired type. As with `select`, such events indicate levels, so this
  level must be tested before putting in the queue.

* `poll.unregister` simply removes the poll selector from the list of
  selectors for a socket, returning `KeyError` if not in the list.

* `poll.poll` - poll the blocking queue for any events with an
  appropriate timeout (specified in milliseconds), include zero
  (default).

To be determined is the possibility of supporting `POLLPRI`; other
events are straightforward.

A minor issue is that `poll` works with [file
descriptors](#file-descriptors-for-sockets), not sockets.


Writing to the socket
=====================

Implementing `socket.send` is straightforward, although this is a good
example of where there's been no work on figuring out precise
exceptions yet:

````python
    def send(self, data):
        if not self.can_write:
            raise Exception("Cannot write to closed socket")  # FIXME use actual exception
        future = self.channel.writeAndFlush(Unpooled.wrappedBuffer(data))
        self._handle_channel_future(future, "send")
````


Reading from the socket
=======================

Netty does not directly support reading from a socket, as needed by
`socket.recv`. Instead any code needs to implement a handler,
specifically overriding `ChannelInboundHandler.channelRead`, then do
something with the received message:

````python
    def channelRead(self, ctx, msg):
        msg.retain()  # bump ref count so it can be used in the blocking queue
        self.sock.incoming.put(msg)
        self.sock._notify_selectors()
        ctx.fireChannelRead(msg)
````

In particular, each socket in this emulation has an incoming queue (a
`java.util.concurrent.LinkedBlockingQueue`) which buffers any read
messages. The one complexity in Netty is that messages are `ByteBuf`,
which is reference counted by Netty, so the ref count needs to be
increased by one to be used (temporarily) outside of Netty.

This makes `socket.recv` reasonably simple, with three cases to be
handled:

````python
    def recv(self, bufsize, flags=0):
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
````

(`flags` is not looked at, but as usual, this is the sort of thing
that's likely to be fairly non-portable. TBD.)

The interesting case here is breaking up a received message into
`bufsize` chunks. The other interesting detail is that this currently
involves two copies, one to the `byte[]` array allocated by `jarray`
(necessary to move the data out of Netty) and then to a `PyString`
(this second copy can be avoided by implementing and using
`socket.recv_into` with a `bytearray`).

The helper method `socket._get_incoming_msg` handles blocking (with
possible timeout) and nonblocking cases. In particular, because
`LinkedBlockingQueue` does not allow for pushing back onto the front
of a queue, the socket wrapper keeps a separate head:

````python
    def _get_incoming_msg(self):
        if self.incoming_head is None:
            if self.blocking:
                if self.timeout is None:
                    self.incoming_head = self.incoming.take()
                else:
                    self.incoming_head = self.incoming.poll(
                        self.timeout * TO_NANOSECONDS, TimeUnit.NANOSECONDS)
            else:
                self.incoming_head = self.incoming.poll()  # Could be None

        # Only return _PEER_CLOSED once
        msg = self.incoming_head
        if msg is _PEER_CLOSED:
            self.incoming_head = None
        return msg
````

As usual with such code, this method cannot be called concurrently by
multiple threads, but such thread safety is not guaranteed by
`socket.recv`.


Potential issues
================

Jar overhead
------------

To use Netty 4 with Jython, the following jars (as of the 4.0.13
version) are needed, for a total of approx 958K:

Jar                              | Size
-------------------------------- | ---:
netty-buffer-4.0.13.Final.jar    | 138K
netty-codec-4.0.13.Final.jar     | 134K
netty-common-4.0.13.Final.jar    | 336K
netty-handler-4.0.13.Final.jar   |  72K
netty-transport-4.0.13.Final.jar | 278K 

So this is a minimal addition to Jython's dependencies.


Writing in Python
-----------------

This spike is implemented in Python, specifically "pure Jython"
(Python code using Java). Except for some isolated hot spots,
performance is not likely to be improved by rewriting in Java because
everything is in terms of bulk ops.


Namespace import
----------------

Let's assume the actual implementation is written in Python. In the
ant build, Jython uses the Jar Jar Links tool to rewrite Java
namespaces to avoid potential conflicts with certain containers,
however, this rewriting does not (and reliably cannot) take in account
any using Python code. In particular, this means that use of the
io.netty namespace may actually be in org.python.io.netty.

Supporting either is simple: simple do the following in an underlying
_socket support module to pull in the Netty namespace:

````python
try:
    import org.python.io.netty as netty
except ImportError:
    import io.netty as netty
````


Thread pools
------------

The next potential issue is that Netty uses a `ThreadPoolGroup` to
manage channels (including `SSLEngine` tasks) and the corresponding
event loop. Each group is connected with the `Bootstrap` factory, which
manages socket options. So _socket will also instantiate a common
`NioThreadPoolGroup` (possibly both worker and boss) for all
subsequent operations.
 
In general, Jython avoids creating any threads except those that the
user creates. (The one current exception is the use of
`Runtime.addShutdownHook`.) This is in part because threads can cause
issues with class unloading.

However, this is straightforward to mitigate as seen in the spike. At
the module level, _socket simply uses `sys.registerCloser(callback)`
to register a callback hook that does `group.shutdown()`. Although
`shutdown` is a deprecated method, `shutdownGracefully()` takes too
long. Here's why we can use it. In general, we should expect code that
is using sockets has done its own graceful shutdown *at the app
level*. Therefore, any produced errors will demonstrate where this
code has not in fact done so. Given that the thread pool is composed
of daemon threads, this is likely not an issue regardless.

Note that `sys` is equivalent to `PySystemState`; this cleanup is
called by both the JVM shutdown hook process as well as
`PySystemState.cleanup` and `PythonInterpreter.cleanup`. In addition,
Bootstrap/ServerBootstrap, SSLEngine, and other resources are fairly
lightweight, so they can be simply constructed as needed and collected
by GC as usual.


Security manager considerations
-------------------------------

In certain cases, imports of _socket and usage of Python sockets may
fail due to [security manager][] restrictions:

* `SecurityManager.checkAccess` can restrict the construction of new
  threads for a given thread group, which could cause the failure of the thread pool.

* `SecurityManager.checkConnect` and `SecurityManager.checkListen` can
  be used to block the construction of peer and server sockets
  respectively.

My understanding is that restricting creation of threads is fairly
rare in containers, and would presumably be subsumed under being able
to actually open sockets. But it's up to the specific security
manager. In general, this is not an issue, especially for the use case
of supporting pip, which will be run from the command line or as part
of some tooling.


Late binding of `socket.bind`
-----------------------------

In Python it is possible to get the actual ephemeral socket address
before `listen` (or less likely, `connect`):

````python
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("localhost", 0))  # request ephemeral port by using 0
host, port = s.getsockname()
````

In Java, sockets can only be either client (`SocketChannel`) or server
sockets (`ServerSocketChannel`), unlike how they are exposed in the C
API. Therefore such bind settings are instead only **intent** until
either `listen` or `connect` allows this to be realized. It's not
clear to me the current behavior of returning `port=0` is correct in
this case however. See the section on [deferred creation][] of sockets
in the design notes of Jython's current socket implementation.


Server socket support
=====================

Implementing `socket.accept` requires using a [child handler][] to
setup newly created child sockets. This child handler can readily put
on a blocking queue for such newly created child sockets (per Python
socket wrapper):

````python
class ChildInitializer(ChannelInitializer):

    def __init__(self, parent_wrapper):
        self.parent_wrapper = parent_wrapper 

    def initChannel(self, ch):
	# ensure channel is setup per parent
	if self.parent_wrapper.ssl_wrapped:
	    pipeline = ch.pipeline()
	    engine = SSLContext.getDefault().createSSLEngine()
            engine.setUseClientMode(False);
            pipeline.addLast("ssl", SslHandler(engine)

    	child_sock = self.parent_wrapper.create_child_socket(ch)
	# above ensures calling something like:
	# self.parent_wrapper.new_children.put(child_sock)

	# also publish event for select.select/select.poll for
        # interested listeners (if any)

````

`socket.accept` then simply looks like the following:

````python
class _socketobject(object):
    ...

    def accept(self):
        if self.nonblocking:
            child_sock = self.new_children.poll(0, TimeUnit.SECONDS)
        else:
            child_sock = self.new_children.take()
        return child_sock, child_sock.address
````


Notes
=====

This section captures various observations that were made outside of
the main narrative of this document.


Exceptions
----------

The socket module currently provides a comprehensive mapping of Java
exceptions to Python versions. This needs to be revisited by looking
at how Netty 4 surfaces exceptions, in particular, are these wrapped,
or correspond to what we have already mapped, due to the underlying
implementation?


Other functionality
-------------------

The socket, ssl, and select modules provide other functionality;
however, much of this is already complete and can be copied over from
the existing implementation or my [experimental branch][] (eg peer
certificate introspection).


Managing listeners
------------------

Although Jython backs `set` with a `ConcurrentHashMap`, this is not
sufficient for a list of listeners, which can be concurrently
modified. Although the semantics of weakly consistent iteration of CHM
is sufficient for notification, Jython actually follows what CPython
does and will detect a change in size of a set during set iteration,
throwing a `RuntimeError` if seen.


`Condition variable`
--------------------

In general, the pattern of using [condition variables][] is as
follows:

````python
with cv:  # 1
    while True:
        result = some_test()  # 2
	if result:
	    return result
        cv.wait(timeout)
````

This snippet (1) acquires the condition variable `cv` such that it
always is released upon exit of the code block, such as a return; (2)
ensures both a non-spurious wakeup and potentially no waiting by
directly testing. Other threads using `cv` can progress given that
`cv.wait` immediately releases `cv`, then reacquires when woken up.


Selectable files
----------------

Jython currently does not support selectable files. However, Netty
(and the underlying Java platform, I believe this may require NIO2,
which is part of Java 7), now does support completions on
files. However, this would require revisiting our own implementation
of Python New IO. Lastly, JRuby now supports [selectable stdin][], but
presumably this requires bolting in a suitable stdin plugin for this
functionality into Netty.


Possible Netty plugins
----------------------

In addition to the stdin plugin for Netty, there a couple more to
potentially consider that would require writing a Netty plugin:

* Unix domain sockets. The [Java native runtime] project supports Unix
  domain sockets.

* Raw sockets. Standard Java libraries do not support raw sockets, but
  this could be done with something like [RockSaw][] (Apache
  licensed).

With such a plugin, and with some minimal extra glue, this should then
just work with the approach specified here.


File descriptors for sockets
----------------------------

`socket.fileno()` returns a file descriptor, and this is used with
`poll` in particular. However, a file descriptor is not really a
meaningful concept in Java, so it seems most straightforward to simply
return the socket object itself. (Note that file descriptors in the
previous Jython implementation are objects, of type
`org.python.core.io.SocketIO`.) The alternative is to maintain a weak
bidrectional map of integers to sockets, which seems both pointless
and a lot of work.

(Bidirectional map is necessary to go from fd to socket, for poll
support above; and to go the reverse direction to support
`fileno()`. It must be weak with respect to any sockets to ensure GC
can happen of sockets.)

Likewise `fromfd` is not really a meaningful concept for Java, so it
should not be made available. In particular, as seen in [this
example][fromfd example], using `fromfd` is much like working with `fork`,
another concept not portable to Java.


<!-- references -->

  [2.5 select]: https://wiki.python.org/jython/SelectModule
  [2.5 socket]: https://wiki.python.org/jython/NewSocketModule
  [child handler]: http://netty.io/4.0/api/io/netty/bootstrap/ServerBootstrap.html#childHandler(io.netty.channel.ChannelHandler)
  [condition variables]: http://www.jython.org/jythonbook/en/1.0/Concurrency.html#other-synchronization-objects
  [deferred creation]: https://wiki.python.org/jython/NewSocketModule#Deferred_socket_creation_on_jython
  [experimental branch]: https://bitbucket.org/jimbaker/jython-ssl
  [fromfd example]: https://forrst.com/posts/Sharing_Sockets_Across_Processes_in_Python-46s
  [Java native runtime]: https://github.com/jnr/jnr-unixsocket
  [Netty]: http://netty.io/wiki/user-guide-for-4.x.html
  [RockSaw]: http://www.savarese.com/software/rocksaw/
  [security manager]: http://docs.oracle.com/javase/7/docs/api/java/lang/SecurityManager.html
  [selectable stdin]: http://blog.headius.com/2013/06/the-pain-of-broken-subprocess.html
  [spike]: http://agiledictionary.com/209/spike/
