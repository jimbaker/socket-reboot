import sys
from socket import socket
from select import select
from ssl import wrap_socket


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
        if chunk == "":
            print "No more data, ending"
            break
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
        if chunk == "":
            print "No more data, ending"
            break
        print "Got this chunk:", repr(chunk)
        data += chunk
        response, headers, content = parse_http_response(data)
        if "Content-Length" in headers and int(headers["Content-Length"]) == len(content):
            break
    print "Completed reading"
    sys.stdout.write(data)
    s.close()  # not blocking, what we should we test here? FIXME


def test_blocking_ssl_client():
    s = socket()
    s = wrap_socket(s)
    s.connect(("www.verisign.com", 443))
    s.send("GET / HTTP/1.0\r\nHost: www.verisign.com\r\n\r\n")
    data = ""
    while True:  # FIXME terminate after a certain period of time
        chunk = s.recv(100)
        if chunk == "":
            print "No more data, ending"
            break
        print "Got this chunk:", repr(chunk)
        data += chunk
        response, headers, content = parse_http_response(data)
        # FIXME verisign doesn't return such content - presumably we can detect otherwise by getting an "" on recv
        if "Content-Length" in headers and int(headers["Content-Length"]) == len(content):
            break
    print "Completed reading"
    sys.stdout.write(data)
    s.close()  # blocks


def test_nonblocking_ssl_client():
    s = socket()
    s.setblocking(False)
    s = wrap_socket(s)
    s.connect(("www.verisign.com", 443))
    print "connected"
    r, w, x = select([], [s], [])
    print "write select returned", r, w, x
    assert w == [s]
    print "writing"
    s.send("GET / HTTP/1.0\r\nHost: www.verisign.com\r\n\r\n")
    data = ""
    while True:  # FIXME terminate after a certain period of time
        r, w, x = select([s], [], [])  # verify we got s back
        print "read select returned", r, w, x
        #assert r == [s]
        chunk = s.recv(100)
        if chunk == "":
            print "No more data, ending"
            break
        print "Got this chunk:", repr(chunk)
        data += chunk
        response, headers, content = parse_http_response(data)
        # FIXME verisign doesn't return such content - presumably we can detect otherwise by getting an "" on recv
        if "Content-Length" in headers and int(headers["Content-Length"]) == len(content):
            break
    print "Completed reading"
    sys.stdout.write(data)
    s.close()



def main():
    # FIXME actually make these "tests" real tests that assert results
    # stop using python.org :) use a local CPython server instead for actual testing/compliance
    #test_blocking_client()
    #test_nonblocking_client()
    #test_blocking_ssl_client()
    test_nonblocking_ssl_client()


if __name__ == "__main__":
    main()
