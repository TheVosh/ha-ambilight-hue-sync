"""Integration-level tests on the Home Assistant test harness.

These need ``pytest-homeassistant-custom-component`` (see requirements_test.txt);
they are skipped under the plain nix shell.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.hue_entertainment import const  # noqa: E402
from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_API_PORT,
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_LIGHTS,
    CONF_PAIR_NOW,
    DOMAIN,
)

BRIDGE_ID = "001788FFFE0AB1C2"
LIGHTS = ["light.a", "light.b"]

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "mock_async_zeroconf")


@pytest.fixture(autouse=True)
def _source_ip():
    with (
        patch(
            "custom_components.hue_entertainment.async_get_source_ip",
            return_value="127.0.0.1",
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_get_source_ip",
            return_value="127.0.0.1",
        ),
    ):
        yield


def _entry(**data) -> MockConfigEntry:
    base = {
        CONF_BRIDGE_ID: BRIDGE_ID,
        CONF_LIGHTS: LIGHTS,
        CONF_API_PORT: 0,  # ephemeral ports: the tests never talk to the servers
        CONF_ENTERTAINMENT_PORT: 0,
    }
    base.update(data)
    return MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=base, title="Hue Entertainment")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


# ---------------------------------------------------------------------------
# Setup / unload
# ---------------------------------------------------------------------------


async def test_setup_creates_device_and_sensor_and_unloads_cleanly(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BRIDGE_ID)})
    assert device is not None and device.model == const.BRIDGE_MODEL_ID

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "off"
    assert data.dtls_server._thread is not None and data.dtls_server._thread.is_alive()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not data.dtls_server._thread.is_alive()


async def test_bind_failure_raises_not_ready(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hue_entertainment.HueAPIServer.async_start",
        side_effect=OSError(98, "Address already in use"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# Entertainment lifecycle
# ---------------------------------------------------------------------------


async def test_stream_start_turns_sensor_on_and_watchdog_stops_it(hass: HomeAssistant) -> None:
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 1.0),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.1),
    ):
        entry = await _setup(hass, _entry())
        data = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
        )

        await data.api_server._set_entertainment_active(True, "tvuser")
        assert hass.states.get(entity_id).state == "on"  # dispatcher delivers synchronously
        assert hass.states.get(entity_id).attributes["owner"] == "tvuser"
        assert data.engine.is_active

        # No frames arrive → watchdog auto-stops after FRAME_TIMEOUT
        await asyncio.sleep(1.6)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "off"
        assert not data.engine.is_active
        assert not data.api_server.entertainment_active

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_change_reloads_entry(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    before = entry.runtime_data.api_server
    hass.config_entries.async_update_entry(entry, options={CONF_LIGHTS: ["light.c"]})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.api_server is not before
    assert entry.runtime_data.engine.stats["lights"] == ["light.c"]
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_redact_credentials(hass: HomeAssistant) -> None:
    from custom_components.hue_entertainment.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(
        hass,
        _entry(initial_users={"abcdef0123456789": {"clientkey": "deadbeef", "devicetype": "tv"}}),
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["initial_users"] == "**REDACTED**"
    assert diag["bridge"]["paired_users"] == [{"username": "abcdef…", "devicetype": "tv"}]
    assert "deadbeef" not in str(diag)
    assert diag["engine"]["lights"] == LIGHTS
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


async def test_config_flow_pairs_and_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    assert result["step_id"] == "pre_pairing"

    started = {}

    async def fake_start(self):
        started["server"] = self  # no real bind on :80 in tests

    with (
        patch(
            "custom_components.hue_entertainment.config_flow.HueAPIServer.async_start", fake_start
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.HueAPIServer.async_stop", AsyncMock()
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "waiting_for_tv"

        # The "TV" pairs
        started["server"]._user_store.add("newuser", "cafebabe", "philips#tv")
        await asyncio.sleep(0.6)  # _wait_for_new_user polls every 0.5 s
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LIGHTS] == LIGHTS
    assert result["data"]["initial_users"]["newuser"]["clientkey"] == "cafebabe"
    assert len(result["data"][CONF_BRIDGE_ID]) == 16
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.user_store.get_psk("newuser") == "cafebabe"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_aborts_when_port_in_use(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    with patch(
        "custom_components.hue_entertainment.config_flow.HueAPIServer.async_start",
        side_effect=OSError(98, "Address already in use"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "port_in_use"


async def test_options_flow_validates_bind_ip(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS, CONF_PAIR_NOW: False, CONF_BIND_IP: "not-an-ip"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BIND_IP: "invalid_ip"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS, CONF_PAIR_NOW: False, CONF_BIND_IP: "127.0.0.1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == {
        CONF_LIGHTS: LIGHTS,
        CONF_BIND_IP: "127.0.0.1",
        CONF_HTTP_MODE: "auto",
    }
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Hue API on Home Assistant's own HTTP server
# ---------------------------------------------------------------------------

from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_HTTP_MODE,
    HTTP_MODE_HOMEASSISTANT,
)


async def test_ha_http_mode_serves_hue_api_on_hass_http(hass, hass_client_no_auth, hass_client):
    assert await async_setup_component(hass, "api", {})  # HA owns /api/config
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    data = entry.runtime_data
    assert data.api_server.uses_ha_http
    assert data.api_server.http_port == hass.http.server_port

    anon = await hass_client_no_auth()
    resp = await anon.get("/description.xml")
    assert resp.status == 200 and BRIDGE_ID[-6:].lower() in (await resp.text()).lower()
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID
    # Unauthenticated /api/config → Hue config (shim); authenticated → HA's own
    resp = await anon.get("/api/config")
    assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID
    authed = await hass_client()
    resp = await authed.get("/api/config")
    assert resp.status == 200 and "components" in await resp.json()
    # HA's own API still works alongside
    resp = await authed.get("/api/")
    assert resp.status == 200

    # Pairing through HA's server
    data.api_server.set_link_button(True)
    resp = await anon.post("/api", json={"devicetype": "test#tv", "generateclientkey": True})
    body = await resp.json()
    assert resp.status == 200 and "username" in body[0]["success"]
    username = body[0]["success"]["username"]
    resp = await anon.get(f"/api/{username}/lights/1")
    assert resp.status == 200 and (await resp.json())["name"] == LIGHTS[0]
    assert data.user_store.get_psk(username) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 503
    resp = await authed.get("/api/config")  # HA's handler untouched after detach
    assert resp.status == 200 and "components" in await resp.json()


async def _ha_on_port_80(hass) -> None:
    """Pretend HA serves plain HTTP on :80 (nothing is bound in the harness)."""
    assert await async_setup_component(hass, "http", {})
    hass.http.server_port = 80


async def test_auto_mode_uses_hass_http_when_ha_listens_on_80(hass):
    await _ha_on_port_80(hass)
    entry = await _setup(hass, _entry())
    assert entry.runtime_data.api_server.uses_ha_http
    assert entry.runtime_data.api_server.http_port == 80
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auto_mode_stays_standalone_on_8123(hass):
    entry = await _setup(hass, _entry())
    assert not entry.runtime_data.api_server.uses_ha_http
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_pairs_through_hass_http(hass, hass_client_no_auth):
    await _ha_on_port_80(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    anon = await hass_client_no_auth()
    resp = await anon.post("/api", json={"devicetype": "test#tv", "generateclientkey": True})
    assert resp.status == 200 and "username" in (await resp.json())[0]["success"]
    await asyncio.sleep(0.6)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.api_server.uses_ha_http
    # The entry's server took over the shared views from the pairing server
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200
    assert await hass.config_entries.async_unload(entry.entry_id)
