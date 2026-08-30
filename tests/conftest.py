"""Shared test fixtures."""

import sys
import types
from pathlib import Path

# Repo root on the path so `custom_components.hue_entertainment` imports under a
# bare `pytest tests/` (CI) as well as `python -m pytest`; custom_components/
# itself so the protocol tests can import `hue_entertainment.dtls_psk`.
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "custom_components"))

# Runtime Home Assistant installs the external ``hue-entertainment`` requirement
# before importing the integration. The test environment deliberately omits that
# network dependency and also loads local protocol modules under the same top-level
# package name, so expose only the two import-time symbols backend tests replace.
_hue_package = types.ModuleType("hue_entertainment")
_hue_package.__path__ = [str(_ROOT / "custom_components" / "hue_entertainment")]
_hue_package.__package__ = "hue_entertainment"
_hue_package.EntertainmentSession = object
_hue_package.LightColorCommand = object
sys.modules.setdefault("hue_entertainment", _hue_package)


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
