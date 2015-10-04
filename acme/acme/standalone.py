"""Support for standalone client challenge solvers. """
import argparse
import collections
import functools
import logging
import os
import socket
import sys

import six
from six.moves import BaseHTTPServer  # pylint: disable=import-error
from six.moves import http_client  # pylint: disable=import-error
from six.moves import socketserver  # pylint: disable=import-error

import OpenSSL

from acme import challenges
from acme import crypto_util


logger = logging.getLogger(__name__)

# six.moves.* | pylint: disable=no-member,attribute-defined-outside-init
# pylint: disable=too-few-public-methods,no-init


class TLSServer(socketserver.TCPServer):
    """Generic TLS Server."""

    def __init__(self, *args, **kwargs):
        self.certs = kwargs.pop("certs", {})
        self.method = kwargs.pop(
            # pylint: disable=protected-access
            "method", crypto_util._DEFAULT_DVSNI_SSL_METHOD)
        self.allow_reuse_address = kwargs.pop("allow_reuse_address", True)
        socketserver.TCPServer.__init__(self, *args, **kwargs)

    def _wrap_sock(self):
        self.socket = crypto_util.SSLSocket(
            self.socket, certs=self.certs, method=self.method)

    def server_bind(self):  # pylint: disable=missing-docstring
        self._wrap_sock()
        return socketserver.TCPServer.server_bind(self)


class HTTPSServer(TLSServer, BaseHTTPServer.HTTPServer):
    """HTTPS Server."""

    def server_bind(self):
        self._wrap_sock()
        BaseHTTPServer.HTTPServer.server_bind(self)


class ACMEServerMixin:  # pylint: disable=old-style-class,no-init
    """ACME server common settings mixin.

    .. warning::
       Subclasses have to init ``_stopped = False`` (it's not done here,
       because of old-style classes madness).

    """
    server_version = "ACME standalone client"
    allow_reuse_address = True

    def serve_forever2(self):
        """Serve forever, until other thread calls `shutdown2`."""
        while not self._stopped:
            self.handle_request()

    def shutdown2(self):
        """Shutdown server loop from `serve_forever2`."""
        self._stopped = True

        # dummy request to terminate last server_forever2.handle_request()
        sock = socket.socket()
        try:
            sock.connect(self.socket.getsockname())
        except socket.error:
            pass  # thread is probably already finished
        finally:
            sock.close()

        self.server_close()


class ACMETLSServer(HTTPSServer, ACMEServerMixin):
    """ACME TLS Server."""

    def __init__(self, *args, **kwargs):
        self._stopped = False
        HTTPSServer.__init__(self, *args, **kwargs)


class ACMEServer(BaseHTTPServer.HTTPServer, ACMEServerMixin):
    """ACME Server (non-TLS)."""

    def __init__(self, *args, **kwargs):
        self._stopped = False
        BaseHTTPServer.HTTPServer.__init__(self, *args, **kwargs)


class SimpleHTTPRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """SimpleHTTP challenge handler.

    Adheres to the stdlib"s `socketserver.BaseRequestHandler` interface.

    :ivar set simple_http_resources: A set of `SimpleHTTPResource`
        objects. TODO: better name?

    """
    SimpleHTTPResource = collections.namedtuple(
        "SimpleHTTPResource", "chall response validation")

    def __init__(self, *args, **kwargs):
        self.simple_http_resources = kwargs.pop("simple_http_resources", set())
        socketserver.BaseRequestHandler.__init__(self, *args, **kwargs)

    def do_GET(self):  # pylint: disable=invalid-name,missing-docstring
        if self.path == "/":
            self.handle_index()
        elif self.path.startswith("/" + challenges.SimpleHTTP.URI_ROOT_PATH):
            self.handle_simple_http_resource()
        else:
            self.handle_404()

    def handle_index(self):
        """Handle index page."""
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(self.server.server_version)

    def handle_404(self):
        """Handler 404 Not Found errors."""
        self.send_response(http_client.NOT_FOUND, message="Not Found")
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write("404")

    def handle_simple_http_resource(self):
        """Handle SimpleHTTP provisioned resources."""
        for resource in self.simple_http_resources:
            if resource.chall.path == self.path:
                logger.debug("Serving SimpleHTTP with token %r",
                             resource.chall.encode("token"))
                self.send_response(http_client.OK)
                self.send_header("Content-type", resource.response.CONTENT_TYPE)
                self.end_headers()
                self.wfile.write(resource.validation.json_dumps().encode())
                return
        else:  # pylint: disable=useless-else-on-loop
            logger.debug("No resources to serve")
        logger.debug("%s does not correspond to any resource. ignoring",
                     self.path)

    @classmethod
    def partial_init(cls, simple_http_resources):
        """Partially initialize this handler.

        This is useful because `socketserver.BaseServer` takes
        uninitialized handler and initializes it with the current
        request.

        """
        return functools.partial(
            cls, simple_http_resources=simple_http_resources)


class ACMERequestHandler(SimpleHTTPRequestHandler):
    """ACME request handler."""

    def handle_one_request(self):
        """Handle single request.

        Makes sure that DVSNI probers are ignored.

        """
        try:
            return SimpleHTTPRequestHandler.handle_one_request(self)
        except OpenSSL.SSL.ZeroReturnError:
            logger.debug("Client prematurely closed connection (prober?). "
                         "Ignoring request.")


def simple_server(cli_args, forever=True):
    """Run simple standalone client server."""
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-p", "--port", default=0, help="Port to serve at. By default "
        "picks random free port.")
    args = parser.parse_args(cli_args[1:])

    certs = {}
    resources = {}

    _, hosts, _ = next(os.walk('.'))
    for host in hosts:
        with open(os.path.join(host, "cert.pem")) as cert_file:
            cert_contents = cert_file.read()
        with open(os.path.join(host, "key.pem")) as key_file:
            key_contents = key_file.read()
        certs[host] = (
            OpenSSL.crypto.load_privatekey(
                OpenSSL.crypto.FILETYPE_PEM, key_contents),
            OpenSSL.crypto.load_certificate(
                OpenSSL.crypto.FILETYPE_PEM, cert_contents))

    handler = ACMERequestHandler.partial_init(
        simple_http_resources=resources)
    server = ACMETLSServer(('', int(args.port)), handler, certs=certs)
    six.print_("Serving at https://localhost:{0}...".format(
        server.socket.getsockname()[1]))
    if forever:  # pragma: no cover
        server.serve_forever()
    else:
        server.handle_request()


if __name__ == "__main__":
    sys.exit(simple_server(sys.argv))  # pragma: no cover
