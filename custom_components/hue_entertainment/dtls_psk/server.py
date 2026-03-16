"""Async DTLS-PSK server built on OpenSSL via cffi."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any

from ._openssl import (
    BIO_NOCLOSE,
    SSL_ERROR_ZERO_RETURN,
    ffi,
    get_error_string,
    libssl,
)

_LOGGER = logging.getLogger(__name__)

# Read buffer size — entertainment frames are small (<200 bytes)
_READ_BUF_SIZE = 4096

# Cookie secret for DTLSv1_listen DoS protection
_cookie_secret = os.urandom(32)


# Module-level cookie callbacks — these don't need instance context.
# Must be kept alive as module-level references to prevent GC.
@ffi.callback("int(SSL *, unsigned char *, unsigned int *)")
def _cookie_generate(ssl, cookie, cookie_len):
    """Generate a cookie for DTLS ClientHello verification."""
    tag = hmac.new(_cookie_secret, b"dtls-cookie", "sha256").digest()[:16]
    ffi.memmove(cookie, tag, len(tag))
    cookie_len[0] = len(tag)
    return 1


@ffi.callback("int(SSL *, const unsigned char *, unsigned int)")
def _cookie_verify(ssl, cookie, cookie_len):
    """Verify a cookie from a DTLS ClientHello retry."""
    tag = hmac.new(_cookie_secret, b"dtls-cookie", "sha256").digest()[:16]
    if cookie_len != len(tag):
        return 0
    received = ffi.buffer(cookie, cookie_len)[:]
    return 1 if hmac.compare_digest(received, tag) else 0


class DTLSPSKServer:
    """DTLS server using Pre-Shared Key authentication.

    Runs the blocking DTLS accept/read loop in a background thread,
    dispatches decrypted frames to an async callback via the event loop.
    """

    def __init__(
        self,
        host: str,
        port: int,
        psk_callback: Callable[[str], bytes | None],
        frame_callback: Callable[[bytes], Any],
        loop: asyncio.AbstractEventLoop | None = None,
        read_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._psk_callback = psk_callback
        self._frame_callback = frame_callback
        self._loop = loop
        self._read_timeout = read_timeout

        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._ssl_ctx = ffi.NULL
        # Must keep a reference to the cffi callback to prevent GC
        self._psk_cb_handle: Any = None

    async def async_start(self) -> None:
        """Start the DTLS server in a background thread."""
        if self._running:
            return

        self._loop = self._loop or asyncio.get_running_loop()
        self._running = True
        self._setup_ctx()

        self._thread = threading.Thread(
            target=self._serve_loop,
            name="dtls-psk-server",
            daemon=True,
        )
        self._thread.start()
        _LOGGER.info("DTLS-PSK server started on %s:%d", self._host, self._port)

    async def async_stop(self) -> None:
        """Stop the server and clean up."""
        self._running = False

        # Unblock the listening socket
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        if self._ssl_ctx != ffi.NULL:
            libssl.SSL_CTX_free(self._ssl_ctx)
            self._ssl_ctx = ffi.NULL

        self._psk_cb_handle = None
        _LOGGER.info("DTLS-PSK server stopped")

    def _psk_server_callback(self, ssl, identity, psk, max_psk_len):
        """OpenSSL PSK callback — look up the key for a given identity."""
        try:
            identity_str = ffi.string(identity).decode()
            key = self._psk_callback(identity_str)
            if key is None:
                _LOGGER.warning("PSK lookup failed for identity: %s", identity_str)
                return 0
            if len(key) > max_psk_len:
                _LOGGER.error("PSK too long (%d > %d)", len(key), max_psk_len)
                return 0
            ffi.memmove(psk, key, len(key))
            _LOGGER.debug("PSK provided for identity: %s", identity_str)
            return len(key)
        except Exception:
            _LOGGER.exception("Error in PSK callback")
            return 0

    def _setup_ctx(self) -> None:
        """Create and configure the SSL_CTX."""
        method = libssl.DTLS_server_method()
        self._ssl_ctx = libssl.SSL_CTX_new(method)
        if self._ssl_ctx == ffi.NULL:
            raise RuntimeError(f"SSL_CTX_new failed: {get_error_string()}")

        # Create and set PSK callback — must keep reference
        self._psk_cb_handle = ffi.callback(
            "unsigned int(SSL *, const char *, unsigned char *, unsigned int)",
            self._psk_server_callback,
        )
        libssl.SSL_CTX_set_psk_server_callback(self._ssl_ctx, self._psk_cb_handle)

        # Cookie callbacks for DTLSv1_listen
        libssl.SSL_CTX_set_cookie_generate_cb(self._ssl_ctx, _cookie_generate)
        libssl.SSL_CTX_set_cookie_verify_cb(self._ssl_ctx, _cookie_verify)

    def _serve_loop(self) -> None:
        """Main server loop — runs in a background thread."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self._host, self._port))
            # SO_RCVTIMEO: unblock SSL_read after read_timeout seconds so hard
            # power-off doesn't leave the read loop blocked forever.
            self._sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_RCVTIMEO,
                struct.pack("ll", int(self._read_timeout), 0),
            )
            _LOGGER.debug("UDP socket bound to %s:%d", self._host, self._port)
        except OSError:
            _LOGGER.exception("Failed to bind UDP socket")
            return

        while self._running:
            try:
                self._accept_and_stream()
            except OSError:
                if self._running:
                    _LOGGER.exception("Error in DTLS accept loop")
            except Exception:
                if self._running:
                    _LOGGER.exception("Unexpected error in DTLS server")

    def _accept_and_stream(self) -> None:
        """Wait for a DTLS client, handshake, and read frames."""
        ssl = libssl.SSL_new(self._ssl_ctx)
        if ssl == ffi.NULL:
            raise RuntimeError(f"SSL_new failed: {get_error_string()}")

        try:
            self._do_accept_and_stream(ssl)
        finally:
            libssl.SSL_shutdown(ssl)
            libssl.SSL_free(ssl)

    def _do_accept_and_stream(self, ssl) -> None:
        """Inner accept + stream logic for a single DTLS session."""
        # Create a datagram BIO from our socket
        assert self._sock is not None, "socket closed before accept"
        bio = libssl.BIO_new_dgram(self._sock.fileno(), BIO_NOCLOSE)
        if bio == ffi.NULL:
            raise RuntimeError(f"BIO_new_dgram failed: {get_error_string()}")

        libssl.SSL_set_bio(ssl, bio, bio)

        # Listen for a ClientHello (with cookie exchange)
        client_addr = libssl.BIO_ADDR_new()
        if client_addr == ffi.NULL:
            raise RuntimeError("BIO_ADDR_new failed")

        try:
            _LOGGER.debug("Waiting for DTLS ClientHello...")
            ret = libssl.DTLSv1_listen(ssl, client_addr)
            if ret <= 0:
                err = libssl.SSL_get_error(ssl, ret)
                if not self._running:
                    return
                if ret == 0:
                    # Non-fatal: stale packet or incomplete ClientHello — caller retries
                    _LOGGER.debug("DTLSv1_listen: non-fatal (ret=0, err=%d), retrying", err)
                    return
                raise RuntimeError(
                    f"DTLSv1_listen failed (ret={ret}, err={err}): {get_error_string()}"
                )
        finally:
            libssl.BIO_ADDR_free(client_addr)

        _LOGGER.debug("DTLS ClientHello received, performing handshake...")

        # Complete the handshake
        ret = libssl.SSL_do_handshake(ssl)
        if ret != 1:
            err = libssl.SSL_get_error(ssl, ret)
            raise RuntimeError(
                f"SSL_do_handshake failed (ret={ret}, err={err}): {get_error_string()}"
            )

        _LOGGER.info("DTLS handshake complete, streaming active")

        # Read loop
        buf = ffi.new(f"unsigned char[{_READ_BUF_SIZE}]")
        while self._running:
            ret = libssl.SSL_read(ssl, buf, _READ_BUF_SIZE)
            if ret <= 0:
                err = libssl.SSL_get_error(ssl, ret)
                if err == SSL_ERROR_ZERO_RETURN:
                    _LOGGER.info("DTLS client disconnected cleanly")
                    break
                if not self._running:
                    break
                _LOGGER.warning(
                    "SSL_read error (ret=%d, err=%d): %s",
                    ret,
                    err,
                    get_error_string(),
                )
                break

            frame = bytes(ffi.buffer(buf, ret))
            self._dispatch_frame(frame)

        _LOGGER.info("DTLS streaming session ended")

    def _dispatch_frame(self, frame: bytes) -> None:
        """Send a decrypted frame to the callback."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._frame_callback, frame)
        else:
            self._frame_callback(frame)
