"""Unit tests for the Hue v1 REST API server (hue_api.py)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# ---------------------------------------------------------------------------
# Bootstrap: load const + hue_api without a real HA install
# ---------------------------------------------------------------------------

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"

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
_load("hue_entertainment.user_store", "user_store.py")
_hue_api = _load("hue_entertainment.hue_api", "hue_api.py")

HueAPIServer = _hue_api.HueAPIServer
BRIDGE_MODEL_ID = _const.BRIDGE_MODEL_ID
BRIDGE_API_VERSION = _const.BRIDGE_API_VERSION
BRIDGE_SW_VERSION = _const.BRIDGE_SW_VERSION

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BRIDGE_ID = "001788FFFE123456"
_MAC = "00:17:88:12:34:56"
_HOST_IP = "192.168.1.100"
_LIGHTS = ["light.left", "light.center", "light.right"]

_PRE_PAIRED_USER = "someuser"


def _make_server(light_entities: list[str] = _LIGHTS) -> HueAPIServer:
    server = HueAPIServer(
        bridge_id=_BRIDGE_ID,
        mac=_MAC,
        host_ip=_HOST_IP,
        http_port=8080,
        channel_count=len(light_entities),
        light_entities=light_entities,
    )
    server._user_store.add(_PRE_PAIRED_USER, "aabbccdd" * 4, "test-device")
    server.set_link_button(True)
    return server


@pytest_asyncio.fixture
async def api():
    """TestClient wired to a 3-light HueAPIServer."""
    server = _make_server()
    app = web.Application()
    server._register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, server


@pytest_asyncio.fixture
async def api_one():
    """TestClient wired to a 1-light HueAPIServer."""
    server = _make_server(["light.only"])
    app = web.Application()
    server._register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, server


@pytest_asyncio.fixture
async def api_zero():
    """TestClient wired to a 0-light HueAPIServer."""
    server = _make_server([])
    app = web.Application()
    server._register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, server


# ---------------------------------------------------------------------------
# 1. Discovery config
# ---------------------------------------------------------------------------


class TestDiscoveryConfig:
    @pytest.mark.asyncio
    async def test_nouser_config_returns_200(self, api):
        client, _ = api
        resp = await client.get("/api/nouser/config")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_nouser_config_fields(self, api):
        client, _ = api
        resp = await client.get("/api/nouser/config")
        data = await resp.json()
        assert data["bridgeid"] == _BRIDGE_ID.upper()
        assert data["mac"] == _MAC
        assert data["modelid"] == BRIDGE_MODEL_ID
        assert data["apiversion"] == BRIDGE_API_VERSION
        assert data["swversion"] == BRIDGE_SW_VERSION
        assert data["ipaddress"] == _HOST_IP
        assert "whitelist" in data
        assert "zigbeechannel" in data

    @pytest.mark.asyncio
    async def test_auth_config_same_payload(self, api):
        client, _ = api
        nouser = await (await client.get("/api/nouser/config")).json()
        auth = await (await client.get("/api/someuser/config")).json()
        assert nouser == auth

    @pytest.mark.asyncio
    async def test_bridgeid_uppercased(self, api):
        client, _ = api
        resp = await client.get("/api/nouser/config")
        data = await resp.json()
        assert data["bridgeid"] == data["bridgeid"].upper()

    @pytest.mark.asyncio
    async def test_api_config_alias_returns_same(self, api):
        client, _ = api
        nouser = await (await client.get("/api/nouser/config")).json()
        alias = await (await client.get("/api/config")).json()
        assert nouser == alias


# ---------------------------------------------------------------------------
# 2. Pairing — POST /api
# ---------------------------------------------------------------------------


class TestPairing:
    @pytest.mark.asyncio
    async def test_pair_with_clientkey(self, api):
        client, _ = api
        resp = await client.post("/api", json={"devicetype": "test", "generateclientkey": True})
        assert resp.status == 200
        body = await resp.json()
        assert len(body) == 1
        assert "success" in body[0]
        assert "username" in body[0]["success"]
        assert "clientkey" in body[0]["success"]

    @pytest.mark.asyncio
    async def test_pair_without_clientkey(self, api):
        client, _ = api
        resp = await client.post("/api", json={"devicetype": "test", "generateclientkey": False})
        body = await resp.json()
        assert "username" in body[0]["success"]
        assert "clientkey" not in body[0]["success"]

    @pytest.mark.asyncio
    async def test_paired_user_stored(self, api):
        client, server = api
        resp = await client.post("/api", json={"devicetype": "test", "generateclientkey": True})
        body = await resp.json()
        username = body[0]["success"]["username"]
        assert username in server._user_store.users

    @pytest.mark.asyncio
    async def test_get_user_psk_returns_clientkey(self, api):
        client, server = api
        resp = await client.post("/api", json={"devicetype": "test", "generateclientkey": True})
        body = await resp.json()
        username = body[0]["success"]["username"]
        expected_key = body[0]["success"]["clientkey"]
        assert server.get_user_psk(username) == expected_key

    @pytest.mark.asyncio
    async def test_get_user_psk_unknown_returns_none(self, api):
        _, server = api
        assert server.get_user_psk("nonexistent-user") is None

    @pytest.mark.asyncio
    async def test_link_button_disabled_returns_error_101(self, api):
        client, server = api
        server.set_link_button(False)
        resp = await client.post("/api", json={"devicetype": "test", "generateclientkey": True})
        body = await resp.json()
        assert "error" in body[0]
        assert body[0]["error"]["type"] == 101


# ---------------------------------------------------------------------------
# 3. Datastore auth
# ---------------------------------------------------------------------------


class TestDatastoreAuth:
    @pytest.mark.asyncio
    async def test_unknown_user_returns_error(self, api):
        client, _ = api
        resp = await client.get("/api/null")
        data = await resp.json()
        assert "error" in data[0]
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_known_user_returns_datastore(self, api):
        client, _ = api
        resp = await client.get("/api/someuser")
        data = await resp.json()
        assert "lights" in data
        assert "groups" in data
        assert "config" in data


# ---------------------------------------------------------------------------
# 4. Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_capabilities_returns_200(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/capabilities")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_capabilities_has_streaming(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/capabilities")
        data = await resp.json()
        assert "streaming" in data
        assert data["streaming"]["total"] == 1

    @pytest.mark.asyncio
    async def test_capabilities_channel_count(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/capabilities")
        data = await resp.json()
        assert data["streaming"]["channels"] == len(_LIGHTS)


# ---------------------------------------------------------------------------
# 5. V1 lights
# ---------------------------------------------------------------------------


class TestV1Lights:
    @pytest.mark.asyncio
    async def test_returns_correct_count(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/lights")
        data = await resp.json()
        assert len(data) == len(_LIGHTS)

    @pytest.mark.asyncio
    async def test_light_is_extended_color(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/lights")
        data = await resp.json()
        assert data["1"]["type"] == "Extended color light"

    @pytest.mark.asyncio
    async def test_light_streaming_renderer(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/lights")
        data = await resp.json()
        assert data["1"]["capabilities"]["streaming"]["renderer"] is True

    @pytest.mark.asyncio
    async def test_lights_also_in_datastore(self, api):
        client, _ = api
        resp = await client.get("/api/someuser")
        data = await resp.json()
        assert len(data["lights"]) == len(_LIGHTS)

    @pytest.mark.asyncio
    async def test_light_by_id_returns_full_object(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/lights/1")
        data = await resp.json()
        assert data["type"] == "Extended color light"
        assert "state" in data
        assert "capabilities" in data

    @pytest.mark.asyncio
    async def test_light_by_id_unknown_returns_404(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/lights/99")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_light_state_put_echoes_success(self, api):
        client, _ = api
        resp = await client.put("/api/someuser/lights/1/state", json={"on": True, "bri": 200})
        data = await resp.json()
        assert len(data) == 2
        assert data[0]["success"]["/lights/1/state/on"] is True


# ---------------------------------------------------------------------------
# 6. V1 groups
# ---------------------------------------------------------------------------


class TestV1Groups:
    @pytest.mark.asyncio
    async def test_entertainment_group_exists(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert "1" in data
        assert data["1"]["type"] == "Entertainment"

    @pytest.mark.asyncio
    async def test_group_class_tv(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert data["1"]["class"] == "TV"

    @pytest.mark.asyncio
    async def test_group_lights_match_count(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert len(data["1"]["lights"]) == len(_LIGHTS)

    @pytest.mark.asyncio
    async def test_groups_also_in_datastore(self, api):
        client, _ = api
        resp = await client.get("/api/someuser")
        data = await resp.json()
        assert "1" in data["groups"]
        assert data["groups"]["1"]["type"] == "Entertainment"

    @pytest.mark.asyncio
    async def test_group_by_id_returns_full_object(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/groups/1")
        data = await resp.json()
        assert data["type"] == "Entertainment"
        assert "stream" in data

    @pytest.mark.asyncio
    async def test_group_by_id_unknown_returns_404(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/groups/201")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_zero_lights_returns_empty_groups(self, api_zero):
        client, _ = api_zero
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert data == {}


# ---------------------------------------------------------------------------
# 7. V1 entertainment stream start/stop
# ---------------------------------------------------------------------------


class TestV1Stream:
    @pytest.mark.asyncio
    async def test_group_put_stream_start(self, api):
        """PUT /groups/{id} with {"stream": {"active": true}} starts entertainment."""
        client, server = api
        resp = await client.put("/api/someuser/groups/1", json={"stream": {"active": True}})
        assert resp.status == 200
        assert server.entertainment_active is True
        assert server.entertainment_owner == "someuser"

    @pytest.mark.asyncio
    async def test_group_put_stream_stop(self, api):
        client, server = api
        server._entertainment_active = True  # set internal state for test setup
        resp = await client.put("/api/someuser/groups/1", json={"stream": {"active": False}})
        assert resp.status == 200
        assert server.entertainment_active is False

    @pytest.mark.asyncio
    async def test_stream_subpath_nested_format(self, api):
        client, server = api
        resp = await client.put("/api/someuser/groups/1/stream", json={"stream": {"active": True}})
        assert resp.status == 200
        assert server.entertainment_active is True

    @pytest.mark.asyncio
    async def test_stream_subpath_flat_format(self, api):
        client, server = api
        resp = await client.put("/api/someuser/groups/1/stream", json={"active": True})
        assert resp.status == 200
        assert server.entertainment_active is True

    @pytest.mark.asyncio
    async def test_on_start_callback_invoked(self, api):
        client, server = api
        cb = AsyncMock()
        server.set_entertainment_callbacks(on_start=cb, on_stop=AsyncMock())
        await client.put("/api/someuser/groups/1", json={"stream": {"active": True}})
        cb.assert_awaited_once_with("someuser")

    @pytest.mark.asyncio
    async def test_on_stop_callback_invoked(self, api):
        client, server = api
        cb = AsyncMock()
        server.set_entertainment_callbacks(on_start=AsyncMock(), on_stop=cb)
        await client.put("/api/someuser/groups/1", json={"stream": {"active": False}})
        cb.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. V1 catch-alls
# ---------------------------------------------------------------------------


class TestV1Catchalls:
    @pytest.mark.asyncio
    async def test_unknown_get_resource_returns_200(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/scenes")
        assert resp.status == 200
        assert await resp.json() == {}

    @pytest.mark.asyncio
    async def test_unknown_get_resource_with_id_returns_200(self, api):
        client, _ = api
        resp = await client.get("/api/someuser/scenes/abc123")
        assert resp.status == 200
        assert await resp.json() == {}

    @pytest.mark.asyncio
    async def test_unknown_put_echoes_success(self, api):
        client, _ = api
        resp = await client.put("/api/someuser/rules/1", json={"name": "test"})
        assert resp.status == 200
        data = await resp.json()
        assert data[0]["success"]["/rules/1/name"] == "test"

    @pytest.mark.asyncio
    async def test_unknown_put_with_param_echoes_success(self, api):
        client, _ = api
        resp = await client.put("/api/someuser/rules/1/conditions", json={"a": "b"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_unknown_post_returns_success(self, api):
        client, _ = api
        resp = await client.post("/api/someuser/scenes", json={"name": "test"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_unknown_delete_returns_success(self, api):
        client, _ = api
        resp = await client.delete("/api/someuser/rules/1")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# 9. Description XML
# ---------------------------------------------------------------------------


class TestDescriptionXml:
    @pytest.mark.asyncio
    async def test_returns_xml(self, api):
        client, _ = api
        resp = await client.get("/description.xml")
        assert resp.status == 200
        assert "text/xml" in resp.content_type

    @pytest.mark.asyncio
    async def test_contains_model_number(self, api):
        client, _ = api
        resp = await client.get("/description.xml")
        text = await resp.text()
        assert "BSB002" in text


# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------


class TestSingleLightEdgeCase:
    @pytest.mark.asyncio
    async def test_single_light_position(self, api_one):
        """With 1 light, position x starts at -1.0."""
        client, _ = api_one
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert data["1"]["locations"]["1"] == [-1.0, 1.0, 0.0]


class TestZeroLightsEdgeCase:
    @pytest.mark.asyncio
    async def test_lights_empty(self, api_zero):
        client, _ = api_zero
        resp = await client.get("/api/someuser/lights")
        data = await resp.json()
        assert data == {}

    @pytest.mark.asyncio
    async def test_groups_empty(self, api_zero):
        client, _ = api_zero
        resp = await client.get("/api/someuser/groups")
        data = await resp.json()
        assert data == {}


# ---------------------------------------------------------------------------
# 11. Auth required — unknown usernames rejected with error type 1
# ---------------------------------------------------------------------------


class TestAuthRequired:
    @pytest.mark.asyncio
    async def test_config_auth_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/config")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_capabilities_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/capabilities")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_lights_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/lights")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_groups_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/groups")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_light_by_id_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/lights/1")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_group_by_id_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.get("/api/baduser/groups/1")
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_group_put_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.put("/api/baduser/groups/1", json={"stream": {"active": True}})
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_stream_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.put("/api/baduser/groups/1/stream", json={"active": True})
        data = await resp.json()
        assert data[0]["error"]["type"] == 1

    @pytest.mark.asyncio
    async def test_light_state_rejects_unknown_user(self, api):
        client, _ = api
        resp = await client.put("/api/baduser/lights/1/state", json={"on": True})
        data = await resp.json()
        assert data[0]["error"]["type"] == 1


# ---------------------------------------------------------------------------
# 12. Malformed JSON — returns error type 2
# ---------------------------------------------------------------------------


class TestMalformedJson:
    @pytest.mark.asyncio
    async def test_create_user_malformed_json(self, api):
        client, _ = api
        resp = await client.post(
            "/api",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        data = await resp.json()
        assert data[0]["error"]["type"] == 2

    @pytest.mark.asyncio
    async def test_group_put_malformed_json(self, api):
        client, _ = api
        resp = await client.put(
            "/api/someuser/groups/1",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        data = await resp.json()
        assert data[0]["error"]["type"] == 2

    @pytest.mark.asyncio
    async def test_stream_malformed_json(self, api):
        client, _ = api
        resp = await client.put(
            "/api/someuser/groups/1/stream",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        data = await resp.json()
        assert data[0]["error"]["type"] == 2

    @pytest.mark.asyncio
    async def test_light_state_malformed_json(self, api):
        client, _ = api
        resp = await client.put(
            "/api/someuser/lights/1/state",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        data = await resp.json()
        assert data[0]["error"]["type"] == 2


# ---------------------------------------------------------------------------
# 13. Idempotent pairing — same devicetype returns same credentials
# ---------------------------------------------------------------------------


class TestIdempotentPairing:
    @pytest.mark.asyncio
    async def test_same_devicetype_returns_same_credentials(self, api):
        client, _ = api
        resp1 = await client.post(
            "/api", json={"devicetype": "mydevice#one", "generateclientkey": True}
        )
        body1 = await resp1.json()
        resp2 = await client.post(
            "/api", json={"devicetype": "mydevice#one", "generateclientkey": True}
        )
        body2 = await resp2.json()
        assert body1[0]["success"]["username"] == body2[0]["success"]["username"]
        assert body1[0]["success"]["clientkey"] == body2[0]["success"]["clientkey"]


# ---------------------------------------------------------------------------
# 14. Public entertainment properties
# ---------------------------------------------------------------------------


class TestPublicProperties:
    @pytest.mark.asyncio
    async def test_entertainment_active_starts_false(self, api):
        _, server = api
        assert server.entertainment_active is False

    @pytest.mark.asyncio
    async def test_entertainment_owner_starts_none(self, api):
        _, server = api
        assert server.entertainment_owner is None

    @pytest.mark.asyncio
    async def test_clear_entertainment_resets_state(self, api):
        client, server = api
        # Start streaming first
        await client.put("/api/someuser/groups/1", json={"stream": {"active": True}})
        assert server.entertainment_active is True
        assert server.entertainment_owner == "someuser"
        # Now clear
        server.clear_entertainment()
        assert server.entertainment_active is False
        assert server.entertainment_owner is None
