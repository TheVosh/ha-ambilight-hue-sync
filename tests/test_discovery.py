"""Unit tests for mDNS advertisement (discovery.py)."""

from __future__ import annotations

import importlib.util
import socket
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: stub zeroconf + load const/discovery without a real HA install
# ---------------------------------------------------------------------------

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"

# Stub zeroconf before loading the module so the import doesn't bind sockets
_zeroconf_stub = types.ModuleType("zeroconf")
_zeroconf_stub.IPVersion = MagicMock()
_zeroconf_stub.IPVersion.V4Only = "V4Only"
sys.modules.setdefault("zeroconf", _zeroconf_stub)

_zeroconf_asyncio_stub = types.ModuleType("zeroconf.asyncio")
_zeroconf_asyncio_stub.AsyncZeroconf = MagicMock()
_zeroconf_asyncio_stub.AsyncServiceInfo = MagicMock()
sys.modules.setdefault("zeroconf.asyncio", _zeroconf_asyncio_stub)

_pkg_stub = types.ModuleType("hue_entertainment")
_pkg_stub.__path__ = [str(_base)]  # type: ignore[attr-defined]
_pkg_stub.__package__ = "hue_entertainment"
sys.modules.setdefault("hue_entertainment", _pkg_stub)


def _load(name: str, filename: str):
    path = _base / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_const = _load("hue_entertainment.const", "const.py")
_discovery = _load("hue_entertainment.discovery", "discovery.py")

HueBridgeDiscovery = _discovery.HueBridgeDiscovery
BRIDGE_MODEL_ID = _const.BRIDGE_MODEL_ID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BRIDGE_ID = "001788FFFE123456"
_HOST_IP = "192.168.1.100"
_PORT = 8443
_LAST6 = "123456"


def _make_discovery(**kwargs) -> HueBridgeDiscovery:
    defaults = dict(bridge_id=_BRIDGE_ID, host_ip=_HOST_IP, port=_PORT)
    defaults.update(kwargs)
    return HueBridgeDiscovery(**defaults)


def _make_mocks():
    """Return (mock_zc_instance, mock_info_cls, mock_info_instance)."""
    mock_zc = AsyncMock()
    mock_zc_cls = MagicMock(return_value=mock_zc)

    mock_info = MagicMock()
    mock_info_cls = MagicMock(return_value=mock_info)

    return mock_zc_cls, mock_zc, mock_info_cls, mock_info


# ---------------------------------------------------------------------------
# Service info construction
# ---------------------------------------------------------------------------


class TestServiceInfoConstruction:
    @pytest.mark.asyncio
    async def test_service_name_uses_last6_uppercased(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["name"] == f"Philips Hue - {_LAST6}._hue._tcp.local."

    @pytest.mark.asyncio
    async def test_service_type(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["type_"] == "_hue._tcp.local."

    @pytest.mark.asyncio
    async def test_txt_bridgeid_uppercased(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery(bridge_id="001788fffe123456").async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["properties"]["bridgeid"] == "001788FFFE123456"

    @pytest.mark.asyncio
    async def test_txt_modelid(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["properties"]["modelid"] == BRIDGE_MODEL_ID

    @pytest.mark.asyncio
    async def test_address_is_packed_ip(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["addresses"] == [socket.inet_aton(_HOST_IP)]

    @pytest.mark.asyncio
    async def test_port(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery(port=9443).async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["port"] == 9443

    @pytest.mark.asyncio
    async def test_server_name_format(self):
        mock_zc_cls, mock_zc, mock_info_cls, _ = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        _, kwargs = mock_info_cls.call_args
        assert kwargs["server"] == f"philips-hue-{_LAST6.lower()}.local."


# ---------------------------------------------------------------------------
# Registration / unregistration lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_registers_service(self):
        mock_zc_cls, mock_zc, mock_info_cls, mock_info = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            await _make_discovery().async_start()
        mock_zc.async_register_service.assert_awaited_once_with(mock_info)

    @pytest.mark.asyncio
    async def test_stop_unregisters_and_closes(self):
        mock_zc_cls, mock_zc, mock_info_cls, mock_info = _make_mocks()
        mock_zc.async_register_service = AsyncMock()
        mock_zc.async_unregister_service = AsyncMock()
        mock_zc.async_close = AsyncMock()
        with (
            patch.object(_discovery, "AsyncZeroconf", mock_zc_cls),
            patch.object(_discovery, "AsyncServiceInfo", mock_info_cls),
        ):
            disc = _make_discovery()
            await disc.async_start()
            await disc.async_stop()
        mock_zc.async_unregister_service.assert_awaited_once_with(mock_info)
        mock_zc.async_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_before_start_is_noop(self):
        """async_stop with no prior async_start must not raise."""
        disc = _make_discovery()
        await disc.async_stop()  # should not raise
