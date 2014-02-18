from io.netty.channel import ChannelInitializer
from io.netty.handler.ssl import SslHandler

from _socket import error
from _sslcerts import _get_ssl_context


CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED = range(3)

class SSLError(error): pass


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
    
    def __init__(self, sock,
                 keyfile, certfile, ca_certs,
                 do_handshake_on_connect, server_side):
        self.sock = sock
        self.engine = _get_ssl_context(keyfile, certfile, ca_certs).createSSLEngine()
        self.engine.setUseClientMode(not server_side)
        self.ssl_handler = SslHandler(self.engine)
        self.already_handshaked = False
        self.do_handshake_on_connect = do_handshake_on_connect

        if self.do_handshake_on_connect and hasattr(self.sock, "connected") and self.sock.connected:
            self.already_handshaked = True
            print "Adding SSL handler to pipeline..."
            self.sock.channel.pipeline().addFirst("ssl", self.ssl_handler)
            self.sock._post_connect()
            self.sock._notify_selectors()
            self.sock._unlatch()

        def handshake_step(x):
            print "Handshaking result", x
            if not hasattr(self.sock, "activity_latch"):  # need a better discriminant
                self.sock._post_connect()
            self.sock._notify_selectors()

        self.ssl_handler.handshakeFuture().addListener(handshake_step)

    def connect(self, addr):
        print "SSL connect", self.do_handshake_on_connect
        self.sock._connect(addr)
        if self.do_handshake_on_connect:
            self.already_handshaked = True
            if self.sock.connected:
                print "Adding SSL handler to pipeline..."
                self.sock.channel.pipeline().addFirst("ssl", self.ssl_handler)
            else:
                print "Adding connect handlers to setup..."
                self.sock.connect_handlers.append(SSLInitializer(self.ssl_handler))

    def send(self, data):
        return self.sock.send(data)

    def recv(self, bufsize, flags=0):
        return self.sock.recv(bufsize, flags)

    def close(self):
        # should this also ssl unwrap the channel?
        self.sock.close()

    def shutdown(self, how):
        self.sock.shutdown(how)

    def _readable(self):
        return self.sock._readable()

    def _writable(self):
        return self.sock._writable()

    def _register_selector(self, selector):
        self.sock._register_selector(selector)

    def _unregister_selector(self, selector):
        return self.sock._unregister_selector(selector)

    def _notify_selectors(self):
        self.sock._notify_selectors()

    def do_handshake(self):
        if not self.already_handshaked:
            print "Not handshaked, so adding SSL handler"
            self.already_handshaked = True
            self.sock.channel.pipeline().addFirst("ssl", self.ssl_handler)

    def getpeername(self):
        return self.sock.getpeername()

    def fileno(self):
        return self.sock


def wrap_socket(sock, keyfile=None, certfile=None, server_side=False, cert_reqs=CERT_NONE,
                ssl_version=None, ca_certs=None, do_handshake_on_connect=True,
                suppress_ragged_eofs=True, ciphers=None):
    # instantiates a SSLEngine, with the following things to keep in mind:
    # suppress_ragged_eofs - presumably this is an exception we can detect in Netty, the underlying SSLEngine certainly does
    # ssl_version - use SSLEngine.setEnabledProtocols(java.lang.String[])
    # ciphers - SSLEngine.setEnabledCipherSuites(String[] suites)
    return SSLSocket(sock, 
                     keyfile=keyfile, certfile=certfile, ca_certs=ca_certs,
                     server_side=server_side,
                     do_handshake_on_connect=do_handshake_on_connect)


def unwrap_socket(sock):
    # FIXME removing SSL handler from pipeline should suffice, but low pri for now
    pass




