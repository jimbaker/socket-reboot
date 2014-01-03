Motivation
==========

The socket, select, and ssl modules provide the core, low-level
networking semantics for Python. However, implementing these modules
in their entirety is difficult for Jython given some differences
between these APIs and the underlying Java platform. This is
especially true with respect to supporting IO completions on
nonblocking sockets (using either select or poll) **and** SSL
handshaking.

This spike demonstrates that for Jython 2.7 we can use Netty 4 to
readily implement these core semantics, with minor exceptions. In
particular, this spike mostly looks at the implications of
implementing the select function, which is both minimally documented
and tested in Python. The other element addressed is the management of
Netty's thread pool, especially with respect to cleaning up a
`PySystemState`.

FIXME there are some other things we look at architecturally; also
significant carryover of other functionality from previous work.


What was not covered
====================

This has intentionally been a limited [FIXME spike][], which is a
generally good thing to do with spikes.

In particular, the milestone for releasing Jython 2.7 beta 2 is to
support pip, which in turn requires support for requests, which in
turn is blocking on nonblocking SSL support for peer (client) sockets.

So this means there's no support for server sockets in this spike;
however I did some [FIXME quick analysis][] of the Netty docs that
suggests this support should be straightforward. Although this should
not be necessary, there's an existing solution in Jython trunk for
server sockets without SSL, blocking or not.

Supporting datagram (UDP) sockets has similar reasoning.

**Still to be done is support for exceptions.** In particular, it is
not clear that `select(..., ..., xlist)` is a well-defined,
cross-platform concept for CPython. Appropriate mapping of exceptions
for socket methods is a potential risk, but likely mitigated by the
lack of good cross platform definition of exceptions, say Unix vs
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
  not possible in Java to switch from blocking to nonblocking.

* No direct support for working with SSL in a nonblocking way. Java
  supports `SSLSocket` and `SSLServerSocket`, but not a
  `SocketChannel` variant. Instead, one has to work explicitly with
  the SSL handshaking steps using `SSLEngine`, which does not
  correspond to Python semantics (except perhaps with respect to
  observability of the completions of steps by the `select`
  function`).

The current implementation in Jython 2.5 (and current Jython trunk)
does have a socket and select implementation that works around the
first problem, but at the cost of significant complexity in additional
corner cases and known warts. [FIXME refer to wiki docs, blog post]

Netty 4 allows us to sidestep both these issues seen previously in
Jython 2.5 and add SSL. We make the following observations in this
spike:

* As should be expected, we can layer blocking functionality on a
  common nonblocking implementation. But this is especially easy to do
  in Netty, where making nonblocking ops be blocking is simply a
  matter of waiting on a `ChannelFuture`, with possible timeout.

* Netty in particular mostly eliminates the need to manage any aspect
  of SSL, excepting its setup in a channel pipeline.

* The key challenge then is to implement IO completion support.


Implementing IO completions
===========================

First, we need to distinguish that Netty's completion model is usually
edge-based, in other words some change has happened (perhaps). Both
`select` and `poll` in Python are level-based: a socket is now ready
to read, for example. However, it is easy to layer a level-based model
on an edge-based model: simply add a test - is it ready to read? -
after getting a notification.

(Note that such testing can potentially race if multiple threads are
say selecting and reading on the same socket. Sockets in general are
not threadsafe this way, however.)

Such ready predicates work well with a model of using condition
variables for such notifications, due to inherent quality of CVs that
they may experience spurious wakeups.

There are actually two types of IO completions in Netty:

* `ChannelFuture` - such futures mostly correspond to a discrete state
  transition of the channel itself - connected to peer (implies DNS
  lookup, if necessary); SSL has handshaked; a peer socket has closed
  its side of the channel. Futures are also used for write completions
  (not necessarily at peer, of course).

* Channel handlers - Netty supports a pipeline for each channel,
  divided into inbound and outbound, where handlers on each side of
  the pipeline handle incoming/outgoing events. Some of these events
  carry messages, as wrapped in Netty's ByteBuf. In particular, we are
  interested in `ChannelInboundHandler`.

The reason for distinction in Netty is that it's both incredibly
useful to have explicit pipelines and it also avoids additional
synchronization overhead when going from one handler to
another. Fortunately for our purposes, it really doesn't matter that
this division exists.


Specific mapping
================

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
manages a [FIXME list of listeners] (selector, registered poll
objects) using a `CopyOnWriteArrayList`; normally we would expect a
size of no more than 1, but Python select/poll semantics do allow
multiple listeners.

The [FIXME usual pattern][] of working with a condition variable is
further extended in the case of the select mechanism, because we need
to explicitly register and unregister selectors. To avoid races of
unregistration and notification, this should be always nested in the
acquisition condition variable and the unregistration always performed
upon exit. Having `register_selectors` be a context manager (to be
implemented) ensures this will be the case:

````python
with cv, register_selectors(...):
    while True:
        result = some_test()
        if result:
            return result
        cv.wait(timeout)
````




SSL handshaking and events
--------------------------

To avoid races with SSL handshaking, it is important to add the
`PythonInboundHandler` **after* handshaking completes. I need some
actual experience here, but I believe it's not necessary to manipulate
the pipeline again in the case of SSL renegotiation - it seems to be
only a race of reading the first handshaking (encrypted) message. To
solve this, the listener on the handshake does this setup in a
post_connect step.

Note that we could potentially observe the handshake process by seeing
`SSL_ERROR_WANT_READ` and `SSL_ERROR_WANT_WRITE` exceptions in the
`SSLSocket.do_handshake()` method, then requiring the user to call
select, but this is pointless.




Existing module reuse
=====================

FIXME
From socket:
From jython-ssl


Mapping exceptions
==================

Need to determine how Netty 4 exposes exceptions and comparable Python
exceptions. There's already existing work here, but it has to be
remapped.


Overhead
========

[bring in buffer discussion]

In addition, the necessary jars we would need to add to Jython are
minimal in size, on the order of 1 MB, because there's no need for
codecs or most of the transports.


Import semantics
================

For now, we will assume that this new work is written in Python, using
Java (aka, pure Jython). Because of the use of Jar Jar Links, use of
the io.netty namespace may be in org.python.io.netty; we simply do the
following in an underlying _socket support module to pull in the Netty
namespace:

````python
try:
    import org.python.io.netty as netty
except ImportError:
    import io.netty as netty
````

Netty uses a `ThreadPoolGroup` to manage channels (including SSLEngine
tasks) and the corresponding event loop. Each group is connected with
the Bootstrap factory, which manages socket options. So _socket will
also instantiate a common `NioThreadPoolGroup` (possibly both worker
and boss) for all subsequent operations.
 
_socket should use sys.registerCloser(callback) to register a callback
hook that does `group.shutdown()`; note this is a deprecated method,
but `shutdownGracefully()` takes too long. In general, we should
expect code that is using sockets has done some sort of graceful
shutdown at the app level and any produced errors will demonstrate
where this code has not in fact done so. Given that the thread pool is
composed of daemon threads, this is likely not an issue regardless.

`sys` is equivalent to `PySystemState`; this cleanup is called by both
the JVM shutdown hook process as well as `PySystemState.cleanup` and
`PythonInterpreter.cleanup`.

Note that Bootstrap/ServerBootstrap, SSLEngine, and other resources
are fairly lightweight, so they can be simply constructed as needed.


Security manager considerations
===============================

In certain cases, imports of _socket and usage of Python sockets may
fail due to security manager restrictions:

* Thread FIXME specifically around thread groups
* Socket FIXME

My understanding is that restricting creation of threads is fairly
rare in containers, and would presumably be subsumed under socket
construction.


`socket._socketobject`
======================

Wraps channels and all necessary operations. FIXME


Unification of blocking/nonblocking socket support
==================================================

It seems to be a fairly common pattern for Python code to switch from
blocking to nonblocking support, generally to avoid such issues as
managing SSL handshaking asynchronously. (I'm not aware of code going
the opposite direction.)

The current implementation requires multiple implementations, and
switching from blocking to nonblocking introduces a variety of
limitations: no true zero blocking, issues with select, not to mention
lack of SSL.

So unification will be a big win.

To implement, we will use Netty's inherent support of asynchrony for
nonblocking support, then layer blocking support on as follows:

* Many operations in Netty return a `ChannelFuture`; to block, simply
  use `future.sync()`. A state variable in the Python socket wrapper
  can dictate.

* Other operations, notably read, use a `ChannelHandler`. This handler
  can simply notify a condition variable in the corresponding Python
  socket wrapper. (We do need to ensure that there is no race
  condition if a read is in progress, and the socket is switched from
  nonblocking to blocking, although this seems like a pathological
  case regardless.)


Late binding of `socket.bind`
=============================

Per these docs FIXME, the same restriction will apply for ephemeral
sockets as in current Jython - bind is intent, not final. This is a
fundamental limitation stemming from the Java API design separating
server sockets from client sockets, unlike how they are exposed in a C
API.



Implementing `socket.send`, `socket.sendall`
============================================

`socket.send` needs to take into account isWritable; `socket.sendall`
simply blocks accordingly (I don't believe this makes any sense for
nonblocking sockets TBD).


`socket.recv`
=============

Read handler, notify any blocking reads; make data available to the socket.


Server socket support
=====================

Implementing `socket.accept`

Netty's ServerSocketChannel objects use a [child handler][] to setup
newly created child sockets. This child handler can readily put on a
blocking queue for such newly created child sockets (per Python socket
wrapper):

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

`socket.accept` simply looks like the following:

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

Some notes:

* `poll` (fortunately) is nonblocking for a timeout of zero, instead of
  blocking infinitely.

* Not certain if we can use the backlog in `socket.listen` to
  determine capacity of the blocking queue - this may not be directly
  translatable to Netty.


`select.select`
===============

Need to adapt ChannelHandler events to appropriate readable, writable,
exception lists in `select.select`:

* Ready for reading by collecting `channelRead`
* Ready for writing can detect by triggering on:

     1. `channelWritabilityChanged`
     2. and checking whether `Channel.isWritable()`

* Errors by collecting with `exceptionCaught`

Collection works as follows:

A given `select.select` registers an addFirst handler for each channel
on its pipeline (presumes the usual convention - and way to write
bug-free socket code! - that there is not a large number of selects
concurrently watching every socket, but instead this is
partitioned). addFirst ensures that select will always be triggered,
and allows for simple propagation through the pipeline for any other
handlers (such propagation is a must).

(Do we really want addFirst? It may be possible that such things as
SSL may consume the pipeline, so we may want addLast. Regardless, the
pipeline is completely controlled by the Jython implementation.)

The handler knows the corresponding select object and can write to it.

Notification is via a `ConcurrentSkipListSet` per read/write/error
(choice of this set is to keep similar natural ordering semantics to
CPython and of course support concurrent completions). Notification
also triggers a condition variable associated with the select
object. The original `select.select` wakes up, then removes all
handlers from each channel pipeline, and then returns to the calling
function, listifying the sets. (Does the original order of file
descriptors have to be maintained?)

http://netty.io/4.0/api/io/netty/channel/ChannelInboundHandlerAdapter.html#channelRead(io.netty.channel.ChannelHandlerContext, java.lang.Object)

 http://netty.io/4.0/api/io/netty/channel/ChannelInboundHandlerAdapter.html#channelWritabilityChanged(io.netty.channel.ChannelHandlerContext)

http://netty.io/4.0/api/io/netty/channel/ChannelInboundHandlerAdapter.html#exceptionCaught(io.netty.channel.ChannelHandlerContext, java.lang.Throwable)


`select.poll`
=============

Construct a `poll` object as usual with `select.poll()`; then

* `poll.register` - use the same events (channelRead,
  channelWritabilityChanged, exceptionCaught) as `select.select`, but
  add handlers that enqueue such events to a LinkedBlockingQueue for
  the `poll` object. It's not clear if we need to combine events
  together for the same file descriptor - probably not.

* `poll.unregister` - check for `NoSuchElementException` when using
  `pipeline.remove(handler)`, convert to Python's `KeyError`

* `poll.poll` - poll the blocking queue with an appropriate timeout,
  include zero (default).


`ssl.unwrap`
============

* SslHandler can be added/removed from the ChannelPipeline at any
  time, so this allows support for ssl.wrap/ssl.unwrap

* The wrapping SSLSocket can simply note this state change, including
  ensuring child sockets have the appropriate SSL behavior.


Differences in socket framing
=============================

SSLSocket.do_handshake()

Per the docs (http://docs.python.org/2/library/ssl.html#ssl.SSLSocket.do_handshake)

> Perform a TLS/SSL handshake. If this is used with a non-blocking
  socket, it may raise SSLError with an arg[0] of SSL_ERROR_WANT_READ
  or SSL_ERROR_WANT_WRITE, in which case it must be called again until
  it completes successfully.

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




Exceptions
==========

The socket module currently provides a comprehensive mapping of Java
exceptions to Python versions. This needs to be revisited.


Other functionality
===================

The socket, ssl, and select modules provide other functionality;
however, much of this is already complete and can be copied over from
the existing implementation or my [experimental branch][] (eg peer
certificate introspection).





Notes
=====

`ConcurrentHashMap`
-------------------

Jython backs `set` with a `ConcurrentHashMap`. Although the semantics
of weakly consistent iteration of CHM is sufficient for notification,
Jython actually follows what CPython does and will detect a change in
size of a set during set iteration, throwing a `RuntimeError` if seen.


`Condition variable`
--------------------

In general, the pattern of using a condition variable is as follows:

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
directly testing. Note that `cv.wait` immediately releases `cv`, then
reacquires when woken up.

(See [FIXME Jython concurrency chapter section][] for more details.)


Effiency considerations
-----------------------

Given how Netty pipelines and that all operations are done with
respect to ByteBuf, it's likely that there would be little benefit in
translating to Java, except for perhaps a couple of hot spots.

Copying between ByteBuf and PyString introduces unfortunate overhead
and extra allocation costs. `socket.recv_into` probably doesn't help,
given the differences between the buffer API and ByteBuf.


`SO_TIMEOUT`
------------

* I do not think this applies: 

My reading of
http://docs.python.org/2/library/socket.html#socket.socket.settimeout

is that this is not a shortcut for setting a socket option SO_TIMEOUT,
but instead corresponds to "await timeout".

     * http://docs.python.org/2/library/socket.html#socket.socket.settimeout vs
     * http://netty.io/4.0/api/io/netty/channel/ChannelFuture.html 
       "Do not confuse I/O timeout and await timeout"


High priority
-------------

Maybe in Java 8?



UDP/datagram support
--------------------

FIXME - available in Netty 4 - apparently this is one advantage over
Async*Channel. Apparently async datagram channel support slipped to
Java 8.


Selectable files
-----------------

Jython currently does not support selectable files. However, Netty
(and the underlying Java platform, I believe this may require NIO2,
which is part of Java 7), now does support completions on
files. However, this would require revisiting our own implementation
of Python New IO. Lastly, JRuby now supports [selectable stdin][], but
presumably this requires bolting in a suitable stdin plugin for this
functionality into Netty.


Unix domain sockets
-------------------

Java native runtime project


Raw sockets
-----------

Supporting raw sockets. Presumably this could be done with something
like RockSaw (http://www.savarese.com/software/rocksaw/, Apache
licensed), but does not appear to be immediately pluggable into Netty.


<!-- references -->

FIXME add references to Jython wiki

  [experimental branch]: https://bitbucket.org/jimbaker/jython-ssl
  [Netty]: http://netty.io/wiki/user-guide-for-4.x.html
  [child handler]: http://netty.io/4.0/api/io/netty/bootstrap/ServerBootstrap.html#childHandler(io.netty.channel.ChannelHandler)
  [selectable stdin]: http://blog.headius.com/2013/06/the-pain-of-broken-subprocess.html
