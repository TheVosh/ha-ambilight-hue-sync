"""cffi bindings to OpenSSL's DTLS and PSK functions."""

from __future__ import annotations

import ctypes.util
import sys

import cffi

ffi = cffi.FFI()

ffi.cdef("""
    /* Opaque types */
    typedef ... SSL_METHOD;
    typedef ... SSL_CTX;
    typedef ... SSL;
    typedef ... BIO;
    typedef ... BIO_METHOD;
    typedef ... BIO_ADDR;

    /* Error handling */
    unsigned long ERR_get_error(void);
    void ERR_error_string_n(unsigned long e, char *buf, size_t len);

    /* SSL method */
    const SSL_METHOD *DTLS_server_method(void);

    /* SSL context */
    SSL_CTX *SSL_CTX_new(const SSL_METHOD *method);
    void SSL_CTX_free(SSL_CTX *ctx);
    long SSL_CTX_set_options(SSL_CTX *ctx, long options);

    /* PSK server callback type */
    typedef unsigned int (*psk_server_cb_t)(
        SSL *ssl, const char *identity,
        unsigned char *psk, unsigned int max_psk_len
    );
    void SSL_CTX_set_psk_server_callback(SSL_CTX *ctx, psk_server_cb_t cb);

    /* Cookie callback types */
    typedef int (*cookie_generate_cb_t)(
        SSL *ssl, unsigned char *cookie, unsigned int *cookie_len
    );
    typedef int (*cookie_verify_cb_t)(
        SSL *ssl, const unsigned char *cookie, unsigned int cookie_len
    );
    void SSL_CTX_set_cookie_generate_cb(SSL_CTX *ctx, cookie_generate_cb_t cb);
    void SSL_CTX_set_cookie_verify_cb(SSL_CTX *ctx, cookie_verify_cb_t cb);

    /* SSL object */
    SSL *SSL_new(SSL_CTX *ctx);
    void SSL_free(SSL *ssl);
    int SSL_get_error(const SSL *ssl, int ret);

    /* BIO */
    BIO *BIO_new_dgram(int fd, int close_flag);
    void SSL_set_bio(SSL *ssl, BIO *rbio, BIO *wbio);
    long BIO_ctrl(BIO *bp, int cmd, long larg, void *parg);

    /* BIO_ADDR for DTLSv1_listen */
    BIO_ADDR *BIO_ADDR_new(void);
    void BIO_ADDR_free(BIO_ADDR *addr);

    /* DTLS */
    int DTLSv1_listen(SSL *ssl, BIO_ADDR *client_addr);
    int SSL_do_handshake(SSL *ssl);
    int SSL_read(SSL *ssl, void *buf, int num);
    int SSL_write(SSL *ssl, const void *buf, int num);
    int SSL_shutdown(SSL *ssl);
    int SSL_set_fd(SSL *ssl, int fd);
    int SSL_set_mtu(SSL *ssl, long mtu);

    /* SSL_ERROR_* are macros, defined as Python constants below */

    /* BIO_NOCLOSE is a macro (0x00), not available as a runtime symbol */
""")


def _find_lib(name: str) -> str:
    """Find the OpenSSL shared library path."""
    if sys.platform == "linux":
        return f"lib{name}.so.3"
    path = ctypes.util.find_library(name)
    if path:
        return path
    if sys.platform == "darwin":
        return f"lib{name}.3.dylib"
    return f"lib{name}.so.3"


libssl = ffi.dlopen(_find_lib("ssl"))
libcrypto = ffi.dlopen(_find_lib("crypto"))

# Macro constants not available as runtime symbols in ABI mode
BIO_NOCLOSE = 0x00
SSL_ERROR_NONE = 0
SSL_ERROR_SSL = 1
SSL_ERROR_WANT_READ = 2
SSL_ERROR_WANT_WRITE = 3
SSL_ERROR_SYSCALL = 5
SSL_ERROR_ZERO_RETURN = 6


def get_error_string() -> str:
    """Get the most recent OpenSSL error as a string."""
    err = libcrypto.ERR_get_error()
    if err == 0:
        return "no error"
    buf = ffi.new("char[256]")
    libcrypto.ERR_error_string_n(err, buf, 256)
    return ffi.string(buf).decode()
