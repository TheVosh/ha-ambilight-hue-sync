"""Shared test fixtures."""

import sys
from pathlib import Path

# Add custom_components/ to path so hue_entertainment.dtls_psk can be imported
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))


def free_udp_port() -> int:
    """Return a currently unused UDP port (tests run in parallel must not collide)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The Home Assistant test harness (pytest-homeassistant-custom-component) pulls
# in pytest-socket, which blocks socket creation by default.  The protocol-level
# tests here talk to real loopback sockets, so re-enable them when that plugin
# is present; without it (plain pytest in the nix shell) nothing is blocked.
try:
    import pytest_socket
except ImportError:  # pragma: no cover
    pytest_socket = None

if pytest_socket is not None:
    import pytest

    @pytest.fixture(autouse=True)
    def _enable_loopback_sockets():
        pytest_socket.enable_socket()
        yield
