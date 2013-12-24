# Rough translation of http://docs.python.org/2/library/ssl.html#client-side-operation

from io.netty.bootstrap import Bootstrap, ChannelFactory
from io.netty.buffer import PooledByteBufAllocator, Unpooled
from io.netty.channel import ChannelInboundHandlerAdapter, ChannelInitializer, ChannelOption
from io.netty.channel.nio import NioEventLoopGroup
from io.netty.channel.socket.nio import NioSocketChannel
from io.netty.handler.ssl import SslHandler
from javax.net.ssl import SSLContext
from java.util.concurrent import TimeUnit

import jarray
from threading import Condition


class SSLInitializer(ChannelInitializer):

    def initChannel(self, ch):
        pipeline = ch.pipeline()
        engine = SSLContext.getDefault().createSSLEngine()
        engine.setUseClientMode(True);
        pipeline.addLast("ssl", SslHandler(engine))


def make_request(group, host, port, req):
    bootstrap = Bootstrap().\
        group(group).\
        channel(NioSocketChannel).\
        handler(SSLInitializer()).\
        option(ChannelOption.TCP_NODELAY, True).\
        option(ChannelOption.ALLOCATOR, PooledByteBufAllocator.DEFAULT)
    channel = bootstrap.connect(host, port).sync().channel()
    data = [None]
    cv = Condition()

    class ReadAdapter(ChannelInboundHandlerAdapter):
        def channelRead(self, ctx, msg):
            try:
                length = msg.writerIndex()
                print "length=", length
                data[0] = buf = jarray.zeros(length, "b")
                msg.getBytes(0, buf)
                cv.acquire()
                cv.notify()
                cv.release()
            finally:
                msg.release()

        def channelReadComplete(self, ctx):
            print "Partial read"
            # partial reads; this seems to be seen in SSL handshaking/wrap/unwrap
            pass

    channel.pipeline().addLast(ReadAdapter())
    channel.writeAndFlush(Unpooled.wrappedBuffer(req)).sync()
    channel.read()

    # block until we get one full read; note that any real usage would
    # require parsing the HTTP header (Content-Length) for the number
    # of bytes to be read
    cv.acquire()
    while not data[0]:
        cv.wait()
    cv.release()
    channel.close().sync()
    return data[0].tostring()


def main():
    group = NioEventLoopGroup()
    try:
        host = "www.verisign.com"
        port = 443
        req = """GET / HTTP/1.0\r
    Host: {}\r\n\r\n""".format(host)
        print make_request(group, host, port, req)
    finally:
        print "Shutting down group threadpool"
        group.shutdown()
        # Above is deprecated, but still probably works better in the context of Python;
        # we expect complete cleanup, or for errors to be reported at shutdown
        # group.shutdownGracefully(10, 20, TimeUnit.MILLISECONDS)


if __name__ == "__main__":
    main()
