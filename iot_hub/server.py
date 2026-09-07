"""The HTTP server process.

``ThreadingHTTPServer`` from the standard library is enough here, and "enough"
is the point: no wheels to download, no C extensions to compile, nothing to
license. The sizing is deliberate -- see docs/ARCHITECTURE.md -- but the short
version is that 450 vehicles uploading a batch every 5 seconds is well under
100 requests/second, while the long-polling connections are almost always idle.
"""

from __future__ import annotations

import http.server
import logging
import os
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time

from .api import Api, Request, Response
from .config import Config
from .hub import Hub

log = logging.getLogger("iot_hub.server")

SERVER_NAME = "iot-hub"

#: Long-poll parks one thread per waiting vehicle. The default 8 MiB stack
#: would reserve gigabytes of address space for a 450-vehicle fleet, so ask for
#: something appropriate to threads that only wait on an event.
THREAD_STACK_BYTES = 512 * 1024


class HubHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    #: Depot switches can hand us a burst of reconnects after a power blip;
    #: a deep accept queue means those become slow connects, not failures.
    request_queue_size = 128
    allow_reuse_address = True

    def __init__(self, address, handler_cls, api: Api, config: Config) -> None:
        self.api = api
        self.config = config
        super().__init__(address, handler_cls)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class HubRequestHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 keeps connections alive, so a vehicle pays the TCP handshake
    # once per shift rather than once per upload.
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        api: Api = self.server.api           # type: ignore[attr-defined]
        config: Config = self.server.config  # type: ignore[attr-defined]
        try:
            body = self._read_body(config.max_body_bytes)
        except _BodyTooLarge as exc:
            self._send(Response(413, f'{{"error":{{"status":413,"message":"{exc}"}}}}'.encode()))
            return
        except (ConnectionError, socket.timeout):
            return

        headers = {key: value for key, value in self.headers.items()}
        request = Request(method=method, raw_path=self.path, headers=headers, body=body)
        response = api.handle(request)
        self._send(response, accept_encoding=self.headers.get("Accept-Encoding", ""))

    def _read_body(self, limit: int) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                return self._read_chunked(limit)
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            raise _BodyTooLarge("invalid Content-Length") from None
        if length < 0 or length > limit:
            raise _BodyTooLarge(f"body exceeds {limit} bytes")
        return self.rfile.read(length) if length else b""

    def _read_chunked(self, limit: int) -> bytes:
        chunks = bytearray()
        while True:
            line = self.rfile.readline(64).strip()
            size = int(line.split(b";")[0] or b"0", 16)
            if size == 0:
                self.rfile.readline(8)          # trailing CRLF
                return bytes(chunks)
            if len(chunks) + size > limit:
                raise _BodyTooLarge(f"body exceeds {limit} bytes")
            chunks += self.rfile.read(size)
            self.rfile.readline(8)

    def _send(self, response: Response, accept_encoding: str = "") -> None:
        body = response.body
        headers = dict(response.headers)
        # Fleet-wide map responses compress ~10x; worth it on shared depot Wi-Fi.
        if len(body) > 1024 and "gzip" in accept_encoding.lower():
            import gzip
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (ConnectionError, socket.timeout):
            # A vehicle that drove out of range mid-response is routine, not an
            # error worth a stack trace.
            log.debug("client disconnected before the response was written")

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


class _BodyTooLarge(Exception):
    pass


def build_server(hub: Hub) -> HubHTTPServer:
    config = hub.config
    api = Api(hub)
    # Long-poll holds a socket open for up to max_long_poll_seconds; give the
    # read timeout headroom above that so we cut off only genuinely dead peers.
    HubRequestHandler.timeout = config.max_long_poll_seconds + 30

    try:
        threading.stack_size(THREAD_STACK_BYTES)
    except (ValueError, RuntimeError):
        pass                                    # platform refused; the default still works

    server = HubHTTPServer((config.host, config.port), HubRequestHandler, api, config)

    if config.tls_enabled:
        # The certificate comes from the depot's own offline CA. No public CA,
        # no OCSP, no certificate transparency log -- none of which are
        # reachable here anyway.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=config.tls_cert, keyfile=config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(config: Config | None = None) -> int:
    config = config or Config.from_env()
    logging.basicConfig(
        level=os.environ.get("HUB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    hub = Hub(config)
    hub.start_background_tasks()
    server = build_server(hub)

    scheme = "https" if config.tls_enabled else "http"
    log.info(
        "iot-hub listening on %s://%s:%d (fleet size %d, %d vehicles provisioned, db %s)",
        scheme, config.host, config.port, config.fleet_size, hub.registry.count(), config.db_path,
    )
    if not config.tls_enabled:
        log.info("TLS is off: requests are still HMAC-signed, so they cannot be forged or replayed")
    if not config.operator_key:
        log.warning("HUB_OPERATOR_KEY is unset: the operator API will refuse every request")

    stopping = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        hub.shutdown()
        log.info("iot-hub stopped")
    return 0


if __name__ == "__main__":
    sys.exit(serve())
