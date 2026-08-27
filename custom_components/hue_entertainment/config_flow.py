"""Config flow for Hue Entertainment Bridge."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.network import async_get_source_ip
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_HTTP_MODE,
    CONF_LIGHTS,
    CONF_PAIR_NOW,
    CONF_OUTPUT_BACKEND,
    CONF_HUE_HOST,
    CONF_HUE_APP_KEY,
    CONF_HUE_CLIENT_KEY,
    CONF_HUE_AREA_ID,
    CONF_HUE_AREA_CHANNELS,
    BACKEND_HOME_ASSISTANT,
    BACKEND_HUE,
    DEFAULT_OUTPUT_BACKEND,
    DEFAULT_API_PORT,
    DEFAULT_HTTP_MODE,
    DOMAIN,
    HTTP_MODE_AUTO,
    HTTP_MODE_HOMEASSISTANT,
    HTTP_MODE_STANDALONE,
    LINK_BUTTON_TIMEOUT,
)
from .discovery import HueBridgeDiscovery
from .ha_http import async_get_http_host, resolve_use_ha_http
from .hue_api import HueAPIServer
from .user_store import UserStore

PAIRING_TIMEOUT = LINK_BUTTON_TIMEOUT

_LOGGER = logging.getLogger(__name__)


def _is_ipv4(value: str) -> bool:
    """True for a well-formed dotted-quad IPv4 address."""
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


async def _wait_for_new_user(
    user_store: UserStore, initial_users: set[str], timeout: float
) -> bool:
    """Poll user_store until a new user appears or timeout. Returns True if paired."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if set(user_store.users.keys()) - initial_users:
            return True
        await asyncio.sleep(0.5)
    return False


def mac_from_bridge_id(bridge_id: str) -> str:
    """Derive a colon-separated MAC from a 16-char Hue bridge ID."""
    hex12 = bridge_id.replace("FFFE", "")[:12].lower()
    return ":".join(hex12[i : i + 2] for i in range(0, 12, 2))


class HueEntertainmentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Hue Entertainment Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        self._lights: list[str] = []
        self._backend = DEFAULT_OUTPUT_BACKEND
        self._hue_host = ""
        self._hue_credentials: dict[str, str] = {}
        self._hue_areas: list = []
        self._hue_area_id = ""
        self._bridge_id = ""
        self._paired = False
        self._pairing_task: asyncio.Task | None = None
        self._temp_api: HueAPIServer | None = None
        self._temp_discovery: HueBridgeDiscovery | None = None
        self._temp_user_store: UserStore | None = None

    async def _cleanup_temp_servers(self) -> None:
        """Stop temporary API/discovery servers started during pairing."""
        if self._temp_api:
            await self._temp_api.async_stop()
            self._temp_api = None
        if self._temp_discovery:
            await self._temp_discovery.async_stop()
            self._temp_discovery = None
        if self._pairing_task and not self._pairing_task.done():
            self._pairing_task.cancel()
            self._pairing_task = None

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Choose the output destination before TV pairing."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            self._backend = user_input.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
            self._lights = user_input.get(CONF_LIGHTS, [])
            raw = uuid.uuid4().hex[:12].upper()
            self._bridge_id = raw[:6] + "FFFE" + raw[6:]
            if self._backend == BACKEND_HUE:
                return await self.async_step_hue_host()
            return await self.async_step_pre_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OUTPUT_BACKEND, default=DEFAULT_OUTPUT_BACKEND): SelectSelector(
                        SelectSelectorConfig(
                            options=[BACKEND_HOME_ASSISTANT, BACKEND_HUE],
                            translation_key=CONF_OUTPUT_BACKEND,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_LIGHTS, default=[]): EntitySelector(
                        EntitySelectorConfig(domain="light", multiple=True)
                    ),
                }
            ),
        )

    async def async_step_hue_host(self, user_input=None):
        """Collect the physical bridge host; pairing remains explicitly user initiated."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HUE_HOST]).strip()
            if not host:
                errors[CONF_HUE_HOST] = "invalid_host"
            else:
                self._hue_host = host
                return await self.async_step_hue_pair()
        return self.async_show_form(
            step_id="hue_host",
            data_schema=vol.Schema({vol.Required(CONF_HUE_HOST, default=self._hue_host): TextSelector()}),
            errors=errors,
        )

    async def async_step_hue_pair(self, user_input=None):
        """Pair with the physical Hue Bridge after the link button has been pressed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                from hue_entertainment import HueEntertainmentAPI
                api = HueEntertainmentAPI(self._hue_host)
                try:
                    self._hue_credentials = await api.pair("ha_hue_entertainment#home_assistant")
                    self._hue_areas = await api.get_entertainment_areas()
                finally:
                    await api.close()
            except TimeoutError:
                errors["base"] = "hue_pairing_failed"
            except Exception:  # noqa: BLE001 - errors are intentionally credential-free
                _LOGGER.debug("Physical Hue Bridge pairing failed", exc_info=True)
                errors["base"] = "hue_unreachable"
            else:
                if not self._hue_areas:
                    errors["base"] = "no_entertainment_areas"
                else:
                    return await self.async_step_hue_area()
        return self.async_show_form(step_id="hue_pair", data_schema=vol.Schema({}), errors=errors)

    async def async_step_hue_area(self, user_input=None):
        """Select the real area's native channel layout for the virtual bridge."""
        if user_input is not None:
            self._hue_area_id = user_input[CONF_HUE_AREA_ID]
            return await self.async_step_pre_pairing()
        return self.async_show_form(
            step_id="hue_area",
            data_schema=vol.Schema({
                vol.Required(CONF_HUE_AREA_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": area.id, "label": area.name} for area in self._hue_areas],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }),
        )

    async def async_step_pre_pairing(self, user_input=None):
        """Step 2: inform the user about the pairing step before starting it."""
        if user_input is not None:
            return await self.async_step_pairing()
        return self.async_show_form(
            step_id="pre_pairing",
            data_schema=vol.Schema({}),
        )

    async def async_step_pairing(self, user_input=None):
        """Step 3: start a temporary bridge and wait for the TV to pair."""
        if self._pairing_task is None:
            host_ip = await async_get_source_ip(self.hass)
            use_ha_http = resolve_use_ha_http(self.hass, DEFAULT_HTTP_MODE)
            http_port = self.hass.http.server_port if use_ha_http else DEFAULT_API_PORT
            self._temp_user_store = UserStore()
            self._temp_api = HueAPIServer(
                bridge_id=self._bridge_id,
                mac=mac_from_bridge_id(self._bridge_id),
                host_ip=host_ip,
                http_port=http_port,
                channel_count=len(self._lights),
                light_entities=self._lights,
                user_store=self._temp_user_store,
                http_host=async_get_http_host(self.hass) if use_ha_http else None,
            )
            try:
                await self._temp_api.async_start()
            except OSError:
                _LOGGER.warning(
                    "Port %d is in use; cannot open the pairing window", DEFAULT_API_PORT
                )
                await self._cleanup_temp_servers()
                return self.async_abort(reason="port_in_use")
            self._temp_api.set_link_button(True)

            async_zc = await async_get_async_instance(self.hass)
            self._temp_discovery = HueBridgeDiscovery(
                bridge_id=self._bridge_id,
                host_ip=host_ip,
                port=http_port,
                async_zeroconf=async_zc,
            )
            await self._temp_discovery.async_start()

            self._pairing_task = self.hass.async_create_task(
                _wait_for_new_user(self._temp_user_store, set(), PAIRING_TIMEOUT)
            )

        if not self._pairing_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="waiting_for_tv",
                progress_task=self._pairing_task,
            )

        try:
            self._paired = self._pairing_task.result()
        except Exception:  # noqa: BLE001
            self._paired = False

        await self._cleanup_temp_servers()

        return self.async_show_progress_done(
            next_step_id="paired" if self._paired else "not_paired"
        )

    async def async_step_paired(self, user_input=None):
        """TV paired — create the entry immediately."""
        return self._create_entry()

    async def async_step_not_paired(self, user_input=None):
        """Pairing timed out — acknowledge then create the entry."""
        if user_input is not None:
            return self._create_entry()
        return self.async_show_form(step_id="not_paired", data_schema=vol.Schema({}))

    def _create_entry(self):
        initial_users = dict(self._temp_user_store.users) if self._temp_user_store else {}
        data = {
            CONF_OUTPUT_BACKEND: self._backend,
            CONF_LIGHTS: self._lights,
            CONF_BRIDGE_ID: self._bridge_id,
            "initial_users": initial_users,
        }
        if self._backend == BACKEND_HUE:
            area = next(area for area in self._hue_areas if area.id == self._hue_area_id)
            data.update({
                CONF_HUE_HOST: self._hue_host,
                CONF_HUE_APP_KEY: self._hue_credentials["username"],
                CONF_HUE_CLIENT_KEY: self._hue_credentials["clientkey"],
                CONF_HUE_AREA_ID: area.id,
                CONF_HUE_AREA_CHANNELS: [
                    {"channel_id": channel.channel_id, "name": channel.name, "position": list(channel.position)}
                    for channel in area.channels
                ],
            })
        return self.async_create_entry(
            title="Hue Entertainment Bridge",
            data=data,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Hue Entertainment Bridge."""

    def __init__(self) -> None:
        super().__init__()
        self._lights: list[str] = []
        self._bind_ip: str | None = None
        self._http_mode: str = DEFAULT_HTTP_MODE
        self._backend: str = DEFAULT_OUTPUT_BACKEND
        self._hue_host: str = ""
        self._hue_area_id: str = ""
        self._hue_channels: list[dict] = []
        self._paired = False
        self._pairing_task: asyncio.Task | None = None

    async def async_step_init(self, user_input=None):
        """Show lights selection and optional re-pair toggle."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._lights = user_input[CONF_LIGHTS]
            self._backend = user_input.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
            self._http_mode = user_input.get(CONF_HTTP_MODE, DEFAULT_HTTP_MODE)
            raw_ip = (user_input.get(CONF_BIND_IP) or "").strip()
            if raw_ip and not _is_ipv4(raw_ip):
                errors[CONF_BIND_IP] = "invalid_ip"
            else:
                self._bind_ip = raw_ip or None
                if user_input.get(CONF_PAIR_NOW, False):
                    return await self.async_step_pairing()
                if self._backend == BACKEND_HUE:
                    return await self.async_step_hue_options()
                return self.async_create_entry(title="", data=self._options())

        current_lights = self.config_entry.options.get(
            CONF_LIGHTS, self.config_entry.data.get(CONF_LIGHTS, [])
        )
        current_bind_ip = (
            self.config_entry.options.get(
                CONF_BIND_IP, self.config_entry.data.get(CONF_BIND_IP, "")
            )
            or ""
        )
        current_mode = self.config_entry.options.get(
            CONF_HTTP_MODE, self.config_entry.data.get(CONF_HTTP_MODE, DEFAULT_HTTP_MODE)
        )
        current_backend = self.config_entry.options.get(
            CONF_OUTPUT_BACKEND, self.config_entry.data.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
        )
        if user_input is not None:
            # Re-show what the user typed
            current_lights = user_input[CONF_LIGHTS]
            current_bind_ip = user_input.get(CONF_BIND_IP, "")
            current_mode = user_input.get(CONF_HTTP_MODE, current_mode)
            current_backend = user_input.get(CONF_OUTPUT_BACKEND, current_backend)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OUTPUT_BACKEND, default=current_backend): SelectSelector(
                        SelectSelectorConfig(
                            options=[BACKEND_HOME_ASSISTANT, BACKEND_HUE],
                            translation_key=CONF_OUTPUT_BACKEND,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_LIGHTS, default=current_lights): EntitySelector(
                        EntitySelectorConfig(domain="light", multiple=True)
                    ),
                    vol.Optional(CONF_PAIR_NOW, default=False): BooleanSelector(),
                    # Plain selector: validators inside the schema can't be
                    # serialised for the frontend (voluptuous_serialize)
                    vol.Optional(CONF_BIND_IP, default=current_bind_ip): TextSelector(),
                    vol.Optional(CONF_HTTP_MODE, default=current_mode): SelectSelector(
                        SelectSelectorConfig(
                            options=[HTTP_MODE_AUTO, HTTP_MODE_STANDALONE, HTTP_MODE_HOMEASSISTANT],
                            translation_key=CONF_HTTP_MODE,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_hue_options(self, user_input=None):
        """Select another physical area without recreating the virtual bridge."""
        errors: dict[str, str] = {}
        app_key = self.config_entry.data.get(CONF_HUE_APP_KEY, "")
        if user_input is not None:
            self._hue_host = str(user_input[CONF_HUE_HOST]).strip()
            self._hue_area_id = user_input[CONF_HUE_AREA_ID]
            if not self._hue_host:
                errors[CONF_HUE_HOST] = "invalid_host"
            else:
                try:
                    from hue_entertainment import HueEntertainmentAPI
                    api = HueEntertainmentAPI(self._hue_host, app_key)
                    try:
                        areas = await api.get_entertainment_areas()
                    finally:
                        await api.close()
                    area = next(area for area in areas if area.id == self._hue_area_id)
                    self._hue_channels = [
                        {"channel_id": channel.channel_id, "name": channel.name,
                         "position": list(channel.position)}
                        for channel in area.channels
                    ]
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Could not load physical Hue Entertainment Areas", exc_info=True)
                    errors["base"] = "hue_unreachable"
                else:
                    return self.async_create_entry(title="", data=self._options())

        if not self._hue_host:
            self._hue_host = self.config_entry.options.get(
                CONF_HUE_HOST, self.config_entry.data.get(CONF_HUE_HOST, "")
            )
        try:
            from hue_entertainment import HueEntertainmentAPI
            api = HueEntertainmentAPI(self._hue_host, app_key)
            try:
                areas = await api.get_entertainment_areas()
            finally:
                await api.close()
        except Exception:  # noqa: BLE001
            areas = []
            errors["base"] = "hue_unreachable"
        if not areas:
            errors.setdefault("base", "no_entertainment_areas")
        current_area = self.config_entry.options.get(
            CONF_HUE_AREA_ID, self.config_entry.data.get(CONF_HUE_AREA_ID, "")
        )
        return self.async_show_form(
            step_id="hue_options",
            data_schema=vol.Schema({
                vol.Required(CONF_HUE_HOST, default=self._hue_host): TextSelector(),
                vol.Required(CONF_HUE_AREA_ID, default=current_area): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": area.id, "label": area.name} for area in areas],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    def _options(self) -> dict:
        options = {
            CONF_LIGHTS: self._lights,
            CONF_BIND_IP: self._bind_ip,
            CONF_HTTP_MODE: self._http_mode,
        }
        # Old config entries deliberately remain indistinguishable from their
        # historic shape; absence means Home Assistant/ZHA output.
        if self._backend != DEFAULT_OUTPUT_BACKEND:
            options[CONF_OUTPUT_BACKEND] = self._backend
            options[CONF_HUE_HOST] = self._hue_host
            options[CONF_HUE_AREA_ID] = self._hue_area_id
            options[CONF_HUE_AREA_CHANNELS] = self._hue_channels
        return options

    async def async_step_pairing(self, user_input=None):
        """Open the link button and wait for the TV to pair."""
        if self._pairing_task is None:
            runtime = self.config_entry.runtime_data
            api_server: HueAPIServer = runtime.api_server
            user_store: UserStore = runtime.user_store
            api_server.set_link_button(True)
            initial_users = set(user_store.users.keys())
            self._pairing_task = self.hass.async_create_task(
                _wait_for_new_user(user_store, initial_users, PAIRING_TIMEOUT)
            )

        if not self._pairing_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="waiting_for_tv",
                progress_task=self._pairing_task,
            )

        try:
            self._paired = self._pairing_task.result()
        except Exception:  # noqa: BLE001
            self._paired = False

        return self.async_show_progress_done(
            next_step_id="paired" if self._paired else "not_paired"
        )

    async def async_step_paired(self, user_input=None):
        """TV paired — save options immediately."""
        return self.async_create_entry(title="", data=self._options())

    async def async_step_not_paired(self, user_input=None):
        """Pairing timed out — acknowledge then save options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._options())
        return self.async_show_form(step_id="not_paired", data_schema=vol.Schema({}))
