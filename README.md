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
documented and tested in Python. The other element addressed is the
management of Netty's thread pool, especially with respect to cleaning
up a `PySystemState`.

FIXME there are some other things we look at architecturally; also
significant carryover of other functionality from previous work.

FIXME Unification of blocking/nonblocking socket support
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

First, Netty's completion model is usually **edge-based**, in other
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
to explicitly register and unregister the selector for each socket. To
avoid races of unregistration and notification, this should be always
nested in the acquisition of condition variable and the unregistration
always performed upon exit. Having `register_selectors` be a context
manager ensures this is the case:

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
select, but this is pointless.


Implementing `poll`
===================

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

The remaining complication is that `poll` works with [FIXME file
descriptors][], not sockets.



Implementing `socket.send`, `socket.sendall`
============================================

`socket.send` needs to take into account isWritable; `socket.sendall`
simply blocks accordingly (I don't believe this makes any sense for
nonblocking sockets TBD).


`socket.recv`
=============

Read handler, notify any blocking reads; make data available to the socket.



Potential issues
================


Overhead
--------

[bring in buffer discussion]

In addition, the necessary jars we would need to add to Jython are
minimal in size, on the order of 1 MB, because there's no need for
codecs or most of the transports.


Namespace import
----------------

Let's assume the actual implementation is written in Python, using
Java (aka, pure Jython). In the ant build, Jython uses the Jar Jar
Links tool to rerite Java namespaces to avoid potential conflicts with
certain containers, however, this rewriting does not take in account
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
fail due to security manager restrictions:

* Thread FIXME specifically around thread groups
* Socket FIXME

My understanding is that restricting creation of threads is fairly
rare in containers, and would presumably be subsumed under being able
to actually open sockets.


Late binding of `socket.bind`
-----------------------------

Per these docs FIXME, the same restriction will apply for ephemeral
sockets as in current Jython - bind is intent, not final. This is a
fundamental limitation stemming from the Java API design separating
server sockets from client sockets, unlike how they are exposed in a C
API.


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


Notes
=====

Various observations outside of the main narrative of this README.


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

FIXME also refcounting makes this discussion irrelevant

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


File descriptors for sockets
----------------------------

`socket.fileno()` returns a file descriptor, and this is used with
`poll` in particular. However, a file descriptor is not really a
meaningful concept in Java, so it seems most straightforward to simply
return the socket object itself. (Note that file descriptors in the
previous Jython implementation are objects, of type
`org.python.core.io.SocketIO`.) The alternative is to maintain a weap
bidrectional map of integers to sockets, which seems both pointless
and a lot of work.

(Bidirectional map is necessary to go from fd to socket, for poll
support above; and to go the reverse direction to support
`fileno()`. It must be weak with respect to any sockets to ensure GC
can happen of sockets.)

Likewise `fromfd` is not really a meaningful concept for Java, so it
should not be made available. See for example one interesting use
case:
https://forrst.com/posts/Sharing_Sockets_Across_Processes_in_Python-46s
- this is much like working with `fork`, another concept not portable
to Java.


<!-- references -->

FIXME add references to Jython wiki

  [experimental branch]: https://bitbucket.org/jimbaker/jython-ssl
  [Netty]: http://netty.io/wiki/user-guide-for-4.x.html
  [child handler]: http://netty.io/4.0/api/io/netty/bootstrap/ServerBootstrap.html#childHandler(io.netty.channel.ChannelHandler)
  [selectable stdin]: http://blog.headius.com/2013/06/the-pain-of-broken-subprocess.html
