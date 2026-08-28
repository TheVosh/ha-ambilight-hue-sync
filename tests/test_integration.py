"""Integration-level tests on the Home Assistant test harness.

These need ``pytest-homeassistant-custom-component`` (see requirements_test.txt);
they are skipped under the plain nix shell.
"""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.hue_entertainment import const  # noqa: E402
from custom_components.hue_entertainment.config_flow import (  # noqa: E402
    HueSetupError,
    async_pair_hue_entertainment,
)
from custom_components.hue_entertainment.const import (  # noqa: E402
    BACKEND_HOME_ASSISTANT,
    BACKEND_HUE,
    CONF_API_PORT,
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_BRIGHTNESS_MULTIPLIER,
    CONF_ENTERTAINMENT_PORT,
    CONF_HUE_APP_KEY,
    CONF_HUE_AREA_CHANNELS,
    CONF_HUE_AREA_ID,
    CONF_HUE_AREA_NAME,
    CONF_HUE_BRIDGE_NAME,
    CONF_HUE_CLIENT_KEY,
    CONF_HUE_HOST,
    CONF_INPUT_MODE,
    CONF_LIGHTS,
    CONF_OUTPUT_BACKEND,
    CONF_OUTPUT_CONFIGURED,
    CONF_PAIR_NOW,
    CONF_TV_API_VERSION,
    CONF_TV_CHANNEL_MAPPINGS,
    CONF_TV_HOST,
    CONF_TV_PASSWORD,
    CONF_TV_POLL_FPS,
    CONF_TV_PORT,
    CONF_TV_USERNAME,
    CONF_TV_VERIFY_SSL,
    DEFAULT_INPUT_MODE,
    DEFAULT_OUTPUT_BACKEND,
    DOMAIN,
    INPUT_LEGACY_HUESTREAM,
    INPUT_PHILIPS_JOINTSPACE,
)

BRIDGE_ID = "001788FFFE0AB1C2"
LIGHTS = ["light.a", "light.b"]


def _configured_jointspace_hue_entry() -> MockConfigEntry:
    """Return a complete synthetic modern-mode entry for options-flow tests."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic",
        data={
            CONF_BRIDGE_ID: BRIDGE_ID,
            CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
            CONF_OUTPUT_BACKEND: BACKEND_HUE,
            CONF_OUTPUT_CONFIGURED: True,
            CONF_TV_HOST: "synthetic-tv",
            CONF_TV_USERNAME: "synthetic-user",
            CONF_TV_PASSWORD: "synthetic-password",
            CONF_TV_API_VERSION: 6,
            CONF_TV_PORT: 1926,
            CONF_TV_VERIFY_SSL: False,
            CONF_TV_POLL_FPS: 10,
            CONF_BRIGHTNESS_MULTIPLIER: 1.0,
            CONF_HUE_HOST: "synthetic-bridge",
            CONF_HUE_BRIDGE_NAME: "Living room Bridge",
            CONF_HUE_APP_KEY: "synthetic-app-key",
            CONF_HUE_CLIENT_KEY: "synthetic-client-key",
            CONF_HUE_AREA_ID: "area-one",
            CONF_HUE_AREA_NAME: "Area One",
            CONF_HUE_AREA_CHANNELS: [
                {
                    "channel_id": 0,
                    "name": "Play Left",
                    "position": [-0.8, 0.4, 0.0],
                    "tv_mapping": "left_top",
                }
            ],
            CONF_TV_CHANNEL_MAPPINGS: {"1": "left_top"},
            CONF_LIGHTS: [],
        },
    )


def _effective_entry_data(entry: MockConfigEntry, result: dict) -> dict:
    """Combine immutable entry data with options returned by an options flow."""
    return {**dict(entry.data), **dict(result["data"])}


pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "mock_async_zeroconf")


async def test_physical_hue_pairing_validates_areas_with_generated_app_key(monkeypatch) -> None:
    """A successful registration must not query CLIP v2 anonymously."""
    created_with: list[str | None] = []

    class FakeHueEntertainmentAPI:
        def __init__(self, _host: str, app_key: str | None = None) -> None:
            created_with.append(app_key)

        async def pair(self, _device_type: str) -> dict[str, str]:
            return {"username": "generated-test-key", "clientkey": "generated-test-client-key"}

        async def get_entertainment_areas(self) -> list[str]:
            assert created_with[-1] == "generated-test-key"
            return ["area"]

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "hue_entertainment",
        SimpleNamespace(HueEntertainmentAPI=FakeHueEntertainmentAPI),
    )
    credentials, areas = await async_pair_hue_entertainment("test-bridge")
    assert credentials["username"] == "generated-test-key"
    assert areas == ["area"]
    assert created_with == [None, "generated-test-key"]


async def test_physical_hue_pairing_keeps_credential_validation_failure_distinct(
    monkeypatch,
) -> None:
    """A post-registration 403 must never be presented as a button failure."""

    class FakeHueEntertainmentAPI:
        def __init__(self, _host: str, app_key: str | None = None) -> None:
            self.app_key = app_key

        async def pair(self, _device_type: str) -> dict[str, str]:
            return {"username": "generated-test-key", "clientkey": "generated-test-client-key"}

        async def get_entertainment_areas(self) -> list[str]:
            raise aiohttp.ClientResponseError(None, (), status=403)

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "hue_entertainment",
        SimpleNamespace(HueEntertainmentAPI=FakeHueEntertainmentAPI),
    )
    with pytest.raises(HueSetupError, match="invalid_generated_credentials"):
        await async_pair_hue_entertainment("test-bridge")


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
    assert entry.entry_id in device.config_entries

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


async def test_pre_feature_entry_loads_with_legacy_defaults(hass: HomeAssistant) -> None:
    """An original upstream entry loads without modern fields or migration."""
    from custom_components.hue_entertainment.backends import HomeAssistantLightBackend

    entry = _entry()
    original_data = deepcopy(dict(entry.data))
    original_entry_id = entry.entry_id

    with (
        patch(
            "custom_components.hue_entertainment.PhilipsJointSpaceSource",
            side_effect=AssertionError("JointSpace must not initialize"),
        ),
        patch(
            "custom_components.hue_entertainment.HueEntertainmentBackend",
            side_effect=AssertionError("physical Hue must not initialize"),
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            side_effect=AssertionError("physical Hue authorization must not run"),
        ),
    ):
        await _setup(hass, entry)

    resolved_input = entry.options.get(
        CONF_INPUT_MODE, entry.data.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE)
    )
    resolved_backend = entry.options.get(
        CONF_OUTPUT_BACKEND, entry.data.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
    )
    assert resolved_input == INPUT_LEGACY_HUESTREAM
    assert resolved_backend == BACKEND_HOME_ASSISTANT
    assert entry.runtime_data.jointspace_source is None
    assert isinstance(entry.runtime_data.backend, HomeAssistantLightBackend)
    assert entry.entry_id == original_entry_id
    assert dict(entry.data) == original_data
    assert not entry.options
    assert await hass.config_entries.async_unload(entry.entry_id)


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


async def test_diagnostics_recursively_redact_entry_options(hass: HomeAssistant) -> None:
    """Sensitive options are redacted without mutating the config entry."""
    from custom_components.hue_entertainment.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    options = {
        CONF_TV_PASSWORD: "synthetic-tv-password",
        CONF_HUE_APP_KEY: "synthetic-application-key",
        CONF_HUE_CLIENT_KEY: "synthetic-clientkey",
        "nested": {"items": [{"authorization": "synthetic-authorization"}]},
        "harmless": {"label": "visible"},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data=dict(_entry().data),
        options=deepcopy(options),
        title="Hue Entertainment",
    )
    await _setup(hass, entry)
    original_options = deepcopy(dict(entry.options))

    diag = await async_get_config_entry_diagnostics(hass, entry)
    redacted = diag["entry"]["options"]
    assert redacted[CONF_TV_PASSWORD] == "**REDACTED**"
    assert redacted[CONF_HUE_APP_KEY] == "**REDACTED**"
    assert redacted[CONF_HUE_CLIENT_KEY] == "**REDACTED**"
    assert redacted["nested"]["items"][0]["authorization"] == "**REDACTED**"
    assert redacted["harmless"] == {"label": "visible"}
    assert dict(entry.options) == original_options
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_recursive_diagnostics_redaction_does_not_mutate_input() -> None:
    """Options and nested diagnostic payloads never leak credentials."""
    from custom_components.hue_entertainment.diagnostics import redact_diagnostics

    original = {
        "safe": "visible",
        "nested": [{"hue_client_key": "synthetic-secret"}, {"password": "synthetic-password"}],
        "tv_username": "synthetic-user",
    }
    redacted = redact_diagnostics(original)
    assert redacted == {
        "safe": "visible",
        "nested": [{"hue_client_key": "**REDACTED**"}, {"password": "**REDACTED**"}],
        "tv_username": "**REDACTED**",
    }
    assert original["safe"] == "visible"
    assert original["nested"][0]["hue_client_key"] == "synthetic-secret"


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


async def test_jointspace_validation_retains_latest_form_values(hass: HomeAssistant) -> None:
    """A retry must not make the user paste TV credentials again."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_LIGHTS: LIGHTS,
            CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
        },
    )
    assert result["step_id"] == "jointspace"
    values = {
        CONF_TV_HOST: "test-tv",
        CONF_TV_USERNAME: "test-user",
        CONF_TV_PASSWORD: "test-pass",
        CONF_TV_API_VERSION: 6,
        CONF_TV_PORT: 1926,
        CONF_TV_VERIFY_SSL: False,
    }
    with patch(
        "custom_components.hue_entertainment.config_flow.async_validate_jointspace",
        side_effect=asyncio.TimeoutError,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "timeout"}
    defaults = result["data_schema"]({})
    assert {key: defaults[key] for key in values} == values


async def test_modern_initial_flow_persists_authenticated_hue_setup(hass: HomeAssistant) -> None:
    """Modern setup persists only the validated TV and synthetic Hue state."""
    area = SimpleNamespace(id="synthetic-area", name="Living room", channels=[])
    tv = {
        CONF_TV_HOST: "synthetic-tv",
        CONF_TV_USERNAME: "synthetic-user",
        CONF_TV_PASSWORD: "synthetic-password",
        CONF_TV_API_VERSION: 6,
        CONF_TV_PORT: 1926,
        CONF_TV_VERIFY_SSL: False,
    }
    with (
        patch(
            "custom_components.hue_entertainment.config_flow.async_validate_jointspace",
            AsyncMock(return_value={"left": 1}),
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_known_hue_bridges",
            return_value=[],
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            AsyncMock(
                return_value=(
                    {"username": "synthetic-app-key", "clientkey": "synthetic-client-key"},
                    [area],
                )
            ),
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
                CONF_OUTPUT_BACKEND: BACKEND_HUE,
                CONF_LIGHTS: [],
            },
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], tv)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HUE_HOST: "synthetic-bridge"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HUE_AREA_ID: "synthetic-area"}
        )
    data = result["data"]
    assert (
        data[CONF_INPUT_MODE] == INPUT_PHILIPS_JOINTSPACE
        and data[CONF_OUTPUT_BACKEND] == BACKEND_HUE
        and data[CONF_OUTPUT_CONFIGURED]
    )
    assert data[CONF_TV_HOST] == tv[CONF_TV_HOST] and data[CONF_HUE_HOST] == "synthetic-bridge"
    assert (
        data[CONF_HUE_APP_KEY] == "synthetic-app-key"
        and data[CONF_HUE_CLIENT_KEY] == "synthetic-client-key"
    )
    assert data[CONF_HUE_AREA_ID] == "synthetic-area" and data[CONF_HUE_AREA_NAME] == "Living room"


async def test_deferred_hue_setup_preserves_jointspace_configuration(hass: HomeAssistant) -> None:
    """Deferred output setup completes later without revisiting TV credentials."""
    tv = {
        CONF_TV_HOST: "synthetic-tv",
        CONF_TV_USERNAME: "synthetic-user",
        CONF_TV_PASSWORD: "synthetic-password",
        CONF_TV_API_VERSION: 6,
        CONF_TV_PORT: 1926,
        CONF_TV_VERIFY_SSL: False,
    }
    with (
        patch(
            "custom_components.hue_entertainment.config_flow.async_validate_jointspace",
            AsyncMock(return_value={"left": 1}),
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_known_hue_bridges",
            return_value=[],
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
                CONF_OUTPUT_BACKEND: BACKEND_HUE,
                CONF_LIGHTS: [],
            },
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], tv)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HUE_HOST: "synthetic-bridge"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"skip_hue_pairing": True}
        )
    entry = MockConfigEntry(domain=DOMAIN, data=result["data"], title="Synthetic")
    entry.add_to_hass(hass)
    area = SimpleNamespace(id="synthetic-area", name="Living room", channels=[])
    with (
        patch(
            "custom_components.hue_entertainment.config_flow.async_known_hue_bridges",
            return_value=[],
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            AsyncMock(
                return_value=(
                    {"username": "synthetic-app-key", "clientkey": "synthetic-client-key"},
                    [area],
                )
            ),
        ),
    ):
        options = await hass.config_entries.options.async_init(entry.entry_id)
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {"management_action": "reauthorize"}
        )
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {CONF_HUE_HOST: "synthetic-bridge"}
        )
        options = await hass.config_entries.options.async_configure(options["flow_id"], {})
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {CONF_HUE_AREA_ID: "synthetic-area"}
        )
    assert (
        options["data"][CONF_OUTPUT_CONFIGURED]
        and entry.data[CONF_TV_PASSWORD] == tv[CONF_TV_PASSWORD]
    )
    assert (
        options["data"][CONF_HUE_APP_KEY] == "synthetic-app-key"
        and options["data"][CONF_HUE_AREA_NAME] == "Living room"
    )


async def test_management_menu_never_starts_hue_pairing(hass: HomeAssistant) -> None:
    """Opening Configure only exposes stored management metadata."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic",
        data={
            CONF_BRIDGE_ID: BRIDGE_ID,
            CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
            CONF_OUTPUT_BACKEND: BACKEND_HUE,
            CONF_OUTPUT_CONFIGURED: True,
            CONF_TV_HOST: "synthetic-tv",
            CONF_TV_USERNAME: "synthetic-user",
            CONF_TV_PASSWORD: "synthetic-password",
            CONF_HUE_HOST: "synthetic-bridge",
            CONF_HUE_BRIDGE_NAME: "Living room Bridge",
            CONF_HUE_APP_KEY: "synthetic-app-key",
            CONF_HUE_CLIENT_KEY: "synthetic-client-key",
            CONF_HUE_AREA_ID: "synthetic-area",
            CONF_HUE_AREA_NAME: "Living room",
            CONF_LIGHTS: [],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
        side_effect=AssertionError("pair must not run"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    text = str(result["description_placeholders"])
    assert "Living room Bridge" in text and "Living room" in text
    assert (
        entry.data[CONF_HUE_APP_KEY] == "synthetic-app-key"
        and entry.data[CONF_TV_PASSWORD] == "synthetic-password"
    )


async def test_hue_management_prefers_stored_friendly_metadata(hass: HomeAssistant) -> None:
    """The Hue management page uses display metadata without pairing or I/O."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BRIDGE_ID: BRIDGE_ID,
            CONF_INPUT_MODE: INPUT_PHILIPS_JOINTSPACE,
            CONF_OUTPUT_BACKEND: BACKEND_HUE,
            CONF_HUE_HOST: "synthetic-host",
            CONF_HUE_BRIDGE_NAME: "Living room Bridge",
            CONF_HUE_APP_KEY: "synthetic-app-key",
            CONF_HUE_CLIENT_KEY: "synthetic-client-key",
            CONF_HUE_AREA_ID: "synthetic-area-id",
            CONF_HUE_AREA_NAME: "Movie night",
        },
        title="Synthetic",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
        side_effect=AssertionError("pair must not run"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "hue"}
        )
    values = str(result["description_placeholders"])
    assert "Living room Bridge" in values and "Movie night" in values
    assert "synthetic-host" not in values and "synthetic-area-id" not in values


async def _run_hue_test_connection(
    hass: HomeAssistant,
    *,
    areas: list | None = None,
    error: Exception | None = None,
) -> tuple[dict, dict[str, object]]:
    """Run Test connection through the management flow and verify it is read-only."""
    entry = _configured_jointspace_hue_entry()
    entry.add_to_hass(hass)
    before_data = deepcopy(dict(entry.data))
    before_options = deepcopy(dict(entry.options))
    calls: dict[str, object] = {"clients": [], "discoveries": 0, "pairs": 0}

    class FakeHueEntertainmentAPI:
        def __init__(self, host: str, app_key: str) -> None:
            clients = calls["clients"]
            assert isinstance(clients, list)
            clients.append((host, app_key))

        async def get_entertainment_areas(self):
            calls["discoveries"] = int(calls["discoveries"]) + 1
            if error is not None:
                raise error
            return areas

        async def pair(self, _device_type: str):
            calls["pairs"] = int(calls["pairs"]) + 1
            raise AssertionError("pair must not run")

        async def close(self) -> None:
            return None

    with (
        patch(
            "custom_components.hue_entertainment.config_flow._hue_api_type",
            return_value=FakeHueEntertainmentAPI,
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            side_effect=AssertionError("pairing helper must not run"),
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "hue"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"hue_action": "test"}
        )

    assert calls["clients"] == [(entry.data[CONF_HUE_HOST], entry.data[CONF_HUE_APP_KEY])]
    assert calls["discoveries"] == 1
    assert calls["pairs"] == 0
    assert dict(entry.data) == before_data
    assert dict(entry.options) == before_options
    return result, calls


async def test_hue_test_connection_succeeds_read_only(hass: HomeAssistant) -> None:
    """Test connection authenticates and discovers Areas without changing state."""
    result, _calls = await _run_hue_test_connection(
        hass,
        areas=[SimpleNamespace(id="synthetic-area", name="Synthetic Area", channels=[])],
    )

    assert result["step_id"] == "hue_manage"
    assert "successful" in result["description_placeholders"]["result"].lower()
    assert not result["errors"]


async def test_hue_test_connection_classifies_invalid_credentials(
    hass: HomeAssistant,
) -> None:
    """An authenticated CLIP v2 rejection is reported as invalid credentials."""
    error = aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://synthetic-bridge/clip/v2/resource"),
        history=(),
        status=403,
    )
    result, _calls = await _run_hue_test_connection(hass, error=error)

    assert result["errors"] == {"base": "invalid_credentials"}


async def test_hue_test_connection_classifies_unreachable_bridge(
    hass: HomeAssistant,
) -> None:
    """A connection failure is reported without entering authorization."""
    result, _calls = await _run_hue_test_connection(
        hass, error=aiohttp.ClientConnectionError("synthetic connection failure")
    )

    assert result["errors"] == {"base": "bridge_unreachable"}


async def test_hue_test_connection_classifies_timeout(hass: HomeAssistant) -> None:
    """An API timeout remains distinct from an unreachable Bridge."""
    result, _calls = await _run_hue_test_connection(hass, error=asyncio.TimeoutError())

    assert result["errors"] == {"base": "timeout"}


async def test_hue_test_connection_requires_entertainment_area(
    hass: HomeAssistant,
) -> None:
    """An authenticated response without Areas has its own recoverable error."""
    result, _calls = await _run_hue_test_connection(hass, areas=[])

    assert result["errors"] == {"base": "no_entertainment_areas"}
    assert result["errors"]["base"] != "invalid_credentials"


async def test_tv_only_edit_preserves_physical_hue_configuration(
    hass: HomeAssistant,
) -> None:
    """Changing JointSpace settings leaves all Hue output state untouched."""
    entry = _configured_jointspace_hue_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.hue_entertainment.config_flow.async_validate_jointspace",
            AsyncMock(return_value=SimpleNamespace()),
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            side_effect=AssertionError("pair must not run"),
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "tv"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_TV_HOST: entry.data[CONF_TV_HOST],
                CONF_TV_USERNAME: entry.data[CONF_TV_USERNAME],
                CONF_TV_PASSWORD: "",
                CONF_TV_API_VERSION: entry.data[CONF_TV_API_VERSION],
                CONF_TV_PORT: 1927,
                CONF_TV_VERIFY_SSL: entry.data[CONF_TV_VERIFY_SSL],
            },
        )

    effective = _effective_entry_data(entry, result)
    assert effective[CONF_TV_PORT] == 1927
    assert effective[CONF_HUE_HOST] == entry.data[CONF_HUE_HOST]
    assert effective[CONF_HUE_BRIDGE_NAME] == entry.data[CONF_HUE_BRIDGE_NAME]
    assert effective[CONF_HUE_APP_KEY] == entry.data[CONF_HUE_APP_KEY]
    assert effective[CONF_HUE_CLIENT_KEY] == entry.data[CONF_HUE_CLIENT_KEY]
    assert effective[CONF_HUE_AREA_ID] == entry.data[CONF_HUE_AREA_ID]
    assert effective[CONF_HUE_AREA_NAME] == entry.data[CONF_HUE_AREA_NAME]
    assert effective[CONF_TV_CHANNEL_MAPPINGS] == entry.data[CONF_TV_CHANNEL_MAPPINGS]
    assert effective[CONF_OUTPUT_CONFIGURED] is True


async def test_area_only_edit_preserves_other_configuration(hass: HomeAssistant) -> None:
    """Changing an Entertainment Area retains TV, Bridge, and credential state."""
    entry = _configured_jointspace_hue_entry()
    entry.add_to_hass(hass)
    areas = [
        SimpleNamespace(
            id="area-one",
            name="Area One",
            channels=[SimpleNamespace(channel_id=0, name="Play Left", position=(-0.8, 0.4, 0.0))],
        ),
        SimpleNamespace(
            id="area-two",
            name="Area Two",
            channels=[SimpleNamespace(channel_id=0, name="Play Left", position=(-0.7, 0.3, 0.0))],
        ),
    ]

    class FakeHueEntertainmentAPI:
        def __init__(self, host: str, app_key: str) -> None:
            assert host == entry.data[CONF_HUE_HOST]
            assert app_key == entry.data[CONF_HUE_APP_KEY]

        async def get_entertainment_areas(self):
            return areas

        async def close(self) -> None:
            return None

    module = SimpleNamespace(HueEntertainmentAPI=FakeHueEntertainmentAPI)
    with (
        patch.dict(sys.modules, {"hue_entertainment": module}),
        patch(
            "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
            side_effect=AssertionError("pair must not run"),
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "area"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_HUE_HOST: entry.data[CONF_HUE_HOST], CONF_HUE_AREA_ID: "area-two"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"tv_mapping": "left_top"}
        )

    effective = _effective_entry_data(entry, result)
    assert effective[CONF_HUE_AREA_ID] == "area-two"
    assert effective[CONF_HUE_AREA_NAME] == "Area Two"
    assert effective[CONF_TV_HOST] == entry.data[CONF_TV_HOST]
    assert effective[CONF_TV_USERNAME] == entry.data[CONF_TV_USERNAME]
    assert effective[CONF_TV_PASSWORD] == entry.data[CONF_TV_PASSWORD]
    assert effective[CONF_HUE_HOST] == entry.data[CONF_HUE_HOST]
    assert effective[CONF_HUE_BRIDGE_NAME] == entry.data[CONF_HUE_BRIDGE_NAME]
    assert effective[CONF_HUE_APP_KEY] == entry.data[CONF_HUE_APP_KEY]
    assert effective[CONF_HUE_CLIENT_KEY] == entry.data[CONF_HUE_CLIENT_KEY]


async def test_mapping_only_edit_preserves_other_configuration(hass: HomeAssistant) -> None:
    """Changing Ambilight mapping does not reauthorize or replace source/output data."""
    entry = _configured_jointspace_hue_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
        side_effect=AssertionError("pair must not run"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "mapping"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"tv_mapping": "right_bottom"}
        )

    effective = _effective_entry_data(entry, result)
    assert effective[CONF_TV_CHANNEL_MAPPINGS] == {"1": "right_bottom"}
    assert effective[CONF_TV_HOST] == entry.data[CONF_TV_HOST]
    assert effective[CONF_HUE_HOST] == entry.data[CONF_HUE_HOST]
    assert effective[CONF_HUE_BRIDGE_NAME] == entry.data[CONF_HUE_BRIDGE_NAME]
    assert effective[CONF_HUE_APP_KEY] == entry.data[CONF_HUE_APP_KEY]
    assert effective[CONF_HUE_CLIENT_KEY] == entry.data[CONF_HUE_CLIENT_KEY]
    assert effective[CONF_HUE_AREA_ID] == entry.data[CONF_HUE_AREA_ID]
    assert effective[CONF_HUE_AREA_NAME] == entry.data[CONF_HUE_AREA_NAME]


async def test_performance_only_edit_preserves_other_configuration(
    hass: HomeAssistant,
) -> None:
    """Changing performance options leaves TV, Hue, Area, and mapping data intact."""
    entry = _configured_jointspace_hue_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.hue_entertainment.config_flow.async_pair_hue_entertainment",
        side_effect=AssertionError("pair must not run"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"management_action": "performance"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_TV_POLL_FPS: 12}
        )

    effective = _effective_entry_data(entry, result)
    assert effective[CONF_TV_POLL_FPS] == 12
    assert effective[CONF_TV_HOST] == entry.data[CONF_TV_HOST]
    assert effective[CONF_TV_USERNAME] == entry.data[CONF_TV_USERNAME]
    assert effective[CONF_TV_PASSWORD] == entry.data[CONF_TV_PASSWORD]
    assert effective[CONF_HUE_HOST] == entry.data[CONF_HUE_HOST]
    assert effective[CONF_HUE_BRIDGE_NAME] == entry.data[CONF_HUE_BRIDGE_NAME]
    assert effective[CONF_HUE_APP_KEY] == entry.data[CONF_HUE_APP_KEY]
    assert effective[CONF_HUE_CLIENT_KEY] == entry.data[CONF_HUE_CLIENT_KEY]
    assert effective[CONF_HUE_AREA_ID] == entry.data[CONF_HUE_AREA_ID]
    assert effective[CONF_HUE_AREA_NAME] == entry.data[CONF_HUE_AREA_NAME]
    assert effective[CONF_TV_CHANNEL_MAPPINGS] == entry.data[CONF_TV_CHANNEL_MAPPINGS]


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


@pytest.mark.parametrize(
    ("remote", "lan"),
    [
        ("10.86.2.252", True),
        ("192.168.1.20", True),
        ("172.16.0.9", True),
        ("127.0.0.1", True),
        ("fe80::1", True),
        ("fd59:f72e:dc72:d0::443", True),
        ("8.8.8.8", False),
        ("2606:4700::1111", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
def test_is_lan_request(remote, lan):
    from unittest.mock import Mock  # noqa: PLC0415

    from custom_components.hue_entertainment.ha_http import is_lan_request  # noqa: PLC0415

    assert is_lan_request(Mock(remote=remote, headers={})) is lan


def test_proxied_requests_are_never_lan():
    from unittest.mock import Mock  # noqa: PLC0415

    from custom_components.hue_entertainment.ha_http import is_lan_request  # noqa: PLC0415

    assert not is_lan_request(Mock(remote="10.0.0.5", headers={"X-Forwarded-For": "10.0.0.9"}))


async def test_hosted_wildcards_do_not_capture_other_api_paths(hass, hass_client_no_auth):
    """/api/webhook/<id> etc. must not resolve to the Hue catch-alls."""
    assert await async_setup_component(hass, "api", {})
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    anon = await hass_client_no_auth()
    resp = await anon.get("/api/webhook/abc")  # no webhook view registered → plain 404, not {}
    assert resp.status == 404 and await resp.text() != "{}"
    resp = await anon.post("/api/webhook/abc", json={})
    assert resp.status == 404
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200 and "whitelist" not in await resp.json()
    # A real (32-hex) but unknown username still gets the Hue-style 401
    resp = await anon.get("/api/" + "0" * 32 + "/lights")
    assert resp.status == 200 and (await resp.json())[0]["error"]["type"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hue_routes_hidden_from_non_lan_clients(hass, hass_client_no_auth, hass_client):
    assert await async_setup_component(hass, "api", {})
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    anon = await hass_client_no_auth()
    with patch("custom_components.hue_entertainment.ha_http.is_lan_request", return_value=False):
        resp = await anon.get("/api/nouser/config")
        assert resp.status == 404
        resp = await anon.get("/description.xml")
        assert resp.status == 404
        resp = await anon.get("/api/config")  # falls through to HA: 401 for anonymous
        assert resp.status == 401
        authed = await hass_client()
        resp = await authed.get("/api/config")
        assert resp.status == 200 and "components" in await resp.json()
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# pause / resume / release services and the status sensor
# ---------------------------------------------------------------------------

import voluptuous as vol  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402


def _sensor_ids(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str]:
    reg = er.async_get(hass)
    active = reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
    )
    status = reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status")
    assert active and status
    return active, status


async def test_status_sensor_is_named_and_idle_by_default(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    _, status = _sensor_ids(hass, entry)
    assert status == "sensor.hue_entertainment_bridge_status"
    state = hass.states.get(status)
    assert state.state == "idle"
    assert state.attributes["device_class"] == "enum"
    assert set(state.attributes["options"]) == {
        "idle",
        "streaming",
        "classic",
        "paused",
        "releasing",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pause_and_resume_services_drive_both_sensors(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    active, status = _sensor_ids(hass, entry)

    await data.api_server._set_entertainment_active(True, "tvuser")
    assert hass.states.get(active).state == "on"
    assert hass.states.get(status).state == "streaming"

    await hass.services.async_call(DOMAIN, "pause", {"seconds": 10}, blocking=True)
    assert hass.states.get(active).state == "off"  # paused counts as "not driving"
    paused = hass.states.get(status)
    assert paused.state == "paused"
    assert 0 < paused.attributes["paused_remaining_seconds"] <= 10
    assert paused.attributes["underlying_activity"] == "streaming"
    assert data.engine.is_active  # the session itself is untouched

    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    assert hass.states.get(active).state == "on"
    assert hass.states.get(status).state == "streaming"

    await data.api_server._set_entertainment_active(False)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pause_auto_expires_on_the_real_timer(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    _, status = _sensor_ids(hass, entry)
    await hass.services.async_call(DOMAIN, "pause", {"seconds": 0.2}, blocking=True)
    assert hass.states.get(status).state == "paused"
    await asyncio.sleep(0.4)
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "idle"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_release_service_forces_teardown_and_skips_restore(hass: HomeAssistant) -> None:
    """A non-compliant TV (keeps streaming) is torn down after grace + FRAME_TIMEOUT,
    and the lights are NOT restored to their pre-session state — the caller's
    sweep is the new truth."""
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 0.5),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.1),
        patch(
            "custom_components.hue_entertainment.entertainment.async_reproduce_state",
            AsyncMock(),
        ) as reproduce,
    ):
        hass.states.async_set("light.a", "on", {"brightness": 200})
        entry = await _setup(hass, _entry())
        data = entry.runtime_data
        active, status = _sensor_ids(hass, entry)

        await data.api_server._set_entertainment_active(True, "tvuser")
        assert data.engine._saved_states  # snapshot taken

        await hass.services.async_call(DOMAIN, "release", {"seconds": 0.3}, blocking=True)
        assert hass.states.get(active).state == "off"
        assert hass.states.get(status).state == "releasing"
        assert not data.api_server.entertainment_active  # stream.active flag flipped
        assert data.engine.is_active  # ...but the session is still up, waiting on the TV

        await asyncio.sleep(1.2)  # grace (0.3) + FRAME_TIMEOUT (0.5) + watchdog slack
        await hass.async_block_till_done()
        assert hass.states.get(status).state == "idle"
        assert not data.engine.is_active
        reproduce.assert_not_called()

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_release_resolves_when_tv_complies(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    _, status = _sensor_ids(hass, entry)
    await data.api_server._set_entertainment_active(True, "tvuser")
    await hass.services.async_call(DOMAIN, "release", {"seconds": 30}, blocking=True)
    assert hass.states.get(status).state == "releasing"
    # The TV notices stream.active=false and stops on its own
    await data.api_server._set_entertainment_active(False)
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "idle"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_service_schema_caps_seconds(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "pause", {"seconds": 31}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "release", {"seconds": -1}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_raise_when_no_bridge_is_loaded(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "pause")  # registered for HA's lifetime
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "release", {}, blocking=True)
