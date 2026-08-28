"""Config flow for Hue Entertainment Bridge."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import uuid

import voluptuous as vol
import aiohttp
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
    CONF_INPUT_MODE, CONF_TV_HOST, CONF_TV_USERNAME, CONF_TV_PASSWORD, CONF_TV_API_VERSION,
    CONF_TV_PORT, CONF_TV_VERIFY_SSL, INPUT_LEGACY_HUESTREAM, INPUT_PHILIPS_JOINTSPACE,
    DEFAULT_INPUT_MODE, DEFAULT_TV_API_VERSION, DEFAULT_TV_PORT,
    CONF_TV_CHANNEL_MAPPINGS, TV_RELATIVE_POSITIONS,
    CONF_OUTPUT_CONFIGURED,
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
from .ha_hue import async_known_hue_bridges
from .jointspace import async_validate_jointspace
from .user_store import UserStore

PAIRING_TIMEOUT = LINK_BUTTON_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class HueSetupError(Exception):
    """A credential-free, user-facing physical Hue setup failure."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _pairing_error_from_timeout(error: TimeoutError) -> str:
    """Classify the library's registration result without exposing credentials."""
    message = str(error).lower()
    if "link button" in message:
        return "link_button_not_pressed"
    if "unexpected pairing response" in message:
        return "malformed_registration_response"
    if "connect" in message or "timeout" in message:
        return "bridge_unreachable"
    return "registration_rejected"


async def async_pair_hue_entertainment(host: str) -> tuple[dict[str, str], list]:
    """Register then validate a physical Hue Entertainment application.

    The library intentionally does not mutate its app-key after V1 registration,
    so discovery must use a new authenticated client with the generated username.
    """
    from hue_entertainment import HueEntertainmentAPI

    _LOGGER.debug("Physical Hue setup stage=registration")
    registration_api = HueEntertainmentAPI(host)
    try:
        credentials = await registration_api.pair("ha_hue_entertainment#home_assistant")
    except TimeoutError as err:
        _LOGGER.debug("Physical Hue setup stage=registration result=%s", _pairing_error_from_timeout(err))
        raise HueSetupError(_pairing_error_from_timeout(err)) from err
    except (aiohttp.ClientConnectionError, OSError) as err:
        _LOGGER.debug("Physical Hue setup stage=registration result=bridge_unreachable: %s", type(err).__name__)
        raise HueSetupError("bridge_unreachable") from err
    except Exception as err:  # noqa: BLE001 - library error details can include sensitive data
        _LOGGER.debug("Physical Hue setup stage=registration result=registration_rejected: %s", type(err).__name__)
        raise HueSetupError("registration_rejected") from err
    finally:
        await registration_api.close()

    if not isinstance(credentials, dict) or not all(
        isinstance(credentials.get(key), str) and credentials[key] for key in ("username", "clientkey")
    ):
        _LOGGER.debug("Physical Hue setup stage=registration result=malformed_registration_response")
        raise HueSetupError("malformed_registration_response")

    _LOGGER.debug("Physical Hue setup stage=credential_validation result=success")
    discovery_api = HueEntertainmentAPI(host, credentials["username"])
    try:
        _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery")
        areas = await discovery_api.get_entertainment_areas()
    except aiohttp.ClientResponseError as err:
        result = "invalid_generated_credentials" if err.status in (401, 403) else "entertainment_api_initialization_failed"
        _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery http_status=%d result=%s", err.status, result)
        raise HueSetupError(result) from err
    except (aiohttp.ClientConnectionError, OSError) as err:
        _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery result=bridge_unreachable: %s", type(err).__name__)
        raise HueSetupError("bridge_unreachable") from err
    except Exception as err:  # noqa: BLE001 - never include credential data in UI/log messages
        _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery result=entertainment_api_initialization_failed: %s", type(err).__name__)
        raise HueSetupError("entertainment_api_initialization_failed") from err
    finally:
        await discovery_api.close()

    if not areas:
        _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery result=no_entertainment_areas")
        raise HueSetupError("no_entertainment_areas")
    _LOGGER.debug("Physical Hue setup stage=entertainment_area_discovery result=success area_count=%d", len(areas))
    return credentials, areas


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
        self._input_mode = DEFAULT_INPUT_MODE
        self._tv: dict = {}
        self._hue_host = ""
        self._hue_credentials: dict[str, str] = {}
        self._hue_areas: list = []
        self._hue_area_id = ""
        self._tv_channel_mappings: dict[str, str] = {}
        self._mapping_index = 0
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
            self._input_mode = user_input.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE)
            self._lights = user_input.get(CONF_LIGHTS, [])
            raw = uuid.uuid4().hex[:12].upper()
            self._bridge_id = raw[:6] + "FFFE" + raw[6:]
            if self._input_mode == INPUT_PHILIPS_JOINTSPACE:
                return await self.async_step_jointspace()
            if self._backend == BACKEND_HUE:
                return await self.async_step_hue_bridge()
            return await self.async_step_pre_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INPUT_MODE, default=DEFAULT_INPUT_MODE): SelectSelector(
                        SelectSelectorConfig(options=[INPUT_LEGACY_HUESTREAM, INPUT_PHILIPS_JOINTSPACE], translation_key=CONF_INPUT_MODE, mode=SelectSelectorMode.DROPDOWN)
                    ),
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

    async def async_step_hue_bridge(self, user_input=None):
        """Prefer an already configured official Home Assistant Hue bridge."""
        bridges = async_known_hue_bridges(self.hass)
        if len(bridges) == 1:
            self._hue_host = bridges[0].host
            _LOGGER.debug("Selected existing Home Assistant Hue bridge")
            return await self.async_step_hue_pair()
        if user_input is not None:
            selected = user_input["ha_hue_bridge"]
            if selected == "manual":
                return await self.async_step_hue_host()
            bridge = next(bridge for bridge in bridges if bridge.entry_id == selected)
            self._hue_host = bridge.host
            _LOGGER.debug("Selected existing Home Assistant Hue bridge")
            return await self.async_step_hue_pair()
        if not bridges:
            return await self.async_step_hue_host()
        return self.async_show_form(
            step_id="hue_bridge",
            data_schema=vol.Schema({vol.Required("ha_hue_bridge"): SelectSelector(
                SelectSelectorConfig(options=[
                    {"value": bridge.entry_id, "label": f"{bridge.name} ({bridge.host})"}
                    for bridge in bridges
                ] + [{"value": "manual", "label": "Enter a different Hue Bridge"}], mode=SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_jointspace(self, user_input=None):
        """Configure an HTTPS/Digest JointSpace Ambilight source."""
        if user_input is not None:
            self._tv = dict(user_input)
            try:
                await async_validate_jointspace(
                    async_get_clientsession(self.hass), self._tv[CONF_TV_HOST],
                    self._tv[CONF_TV_USERNAME], self._tv[CONF_TV_PASSWORD],
                    api_version=self._tv[CONF_TV_API_VERSION], port=self._tv[CONF_TV_PORT],
                    verify_ssl=self._tv[CONF_TV_VERIFY_SSL],
                )
            except aiohttp.ClientResponseError as err:
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "invalid_auth" if err.status in (401, 403) else "cannot_connect"})
            except aiohttp.ClientConnectorCertificateError:
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "tls_error"})
            except asyncio.TimeoutError:
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "timeout"})
            except (aiohttp.ClientConnectionError, OSError):
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "cannot_connect"})
            except ValueError:
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "invalid_topology"})
            except Exception:
                _LOGGER.debug("JointSpace validation failed", exc_info=True)
                return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema(), errors={"base": "unknown"})
            if self._backend == BACKEND_HUE:
                return await self.async_step_hue_bridge()
            return self._create_entry()
        return self.async_show_form(step_id="jointspace", data_schema=self._jointspace_schema())

    def _jointspace_schema(self):
        return vol.Schema({
                vol.Required(CONF_TV_HOST, default=self._tv.get(CONF_TV_HOST, "")): TextSelector(),
                vol.Required(CONF_TV_USERNAME, default=self._tv.get(CONF_TV_USERNAME, "")): TextSelector(),
                vol.Required(CONF_TV_PASSWORD, default=self._tv.get(CONF_TV_PASSWORD, "")): TextSelector(),
                vol.Optional(CONF_TV_API_VERSION, default=self._tv.get(CONF_TV_API_VERSION, DEFAULT_TV_API_VERSION)): vol.Coerce(int),
                vol.Optional(CONF_TV_PORT, default=self._tv.get(CONF_TV_PORT, DEFAULT_TV_PORT)): vol.Coerce(int),
                vol.Optional(CONF_TV_VERIFY_SSL, default=self._tv.get(CONF_TV_VERIFY_SSL, False)): BooleanSelector(),
            })

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
        if user_input is not None and user_input.get("skip_hue_pairing"):
            return self._create_entry()
        if user_input is not None:
            try:
                self._hue_credentials, self._hue_areas = await async_pair_hue_entertainment(self._hue_host)
            except HueSetupError as err:
                errors["base"] = err.error
            else:
                if not self._hue_areas:
                    errors["base"] = "no_entertainment_areas"
                else:
                    return await self.async_step_hue_area()
        return self.async_show_form(step_id="hue_pair", data_schema=vol.Schema({
            vol.Optional("skip_hue_pairing", default=False): BooleanSelector(),
        }), errors=errors)

    async def async_step_hue_area(self, user_input=None):
        """Select the real area's native channel layout for the virtual bridge."""
        if user_input is not None:
            self._hue_area_id = user_input[CONF_HUE_AREA_ID]
            if self._input_mode == INPUT_PHILIPS_JOINTSPACE:
                self._mapping_index = 0
                return await self.async_step_jointspace_mapping()
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

    async def async_step_jointspace_mapping(self, user_input=None):
        """Collect an explicit TV-relative mapping for each Hue area channel."""
        area = next(area for area in self._hue_areas if area.id == self._hue_area_id)
        if user_input is not None:
            channel = area.channels[self._mapping_index]
            self._tv_channel_mappings[str(self._mapping_index + 1)] = user_input["tv_mapping"]
            self._mapping_index += 1
        if self._mapping_index >= len(area.channels):
            return self._create_entry()
        channel = area.channels[self._mapping_index]
        return self.async_show_form(
            step_id="jointspace_mapping",
            description_placeholders={
                "name": channel.name, "channel_id": str(channel.channel_id),
                "position": "x={:.2f}, y={:.2f}, z={:.2f}".format(*channel.position),
            },
            data_schema=vol.Schema({
                vol.Required("tv_mapping", default="auto"): SelectSelector(
                    SelectSelectorConfig(options=list(TV_RELATIVE_POSITIONS), mode=SelectSelectorMode.DROPDOWN)
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
            CONF_INPUT_MODE: self._input_mode,
            CONF_OUTPUT_BACKEND: self._backend,
            CONF_LIGHTS: self._lights,
            CONF_BRIDGE_ID: self._bridge_id,
            "initial_users": initial_users,
            CONF_OUTPUT_CONFIGURED: bool(self._backend != BACKEND_HUE or self._hue_credentials),
        }
        if self._input_mode == INPUT_PHILIPS_JOINTSPACE:
            data.update(self._tv)
            data[CONF_TV_CHANNEL_MAPPINGS] = self._tv_channel_mappings
        if self._backend == BACKEND_HUE and self._hue_credentials:
            area = next(area for area in self._hue_areas if area.id == self._hue_area_id)
            data.update({
                CONF_HUE_HOST: self._hue_host,
                CONF_HUE_APP_KEY: self._hue_credentials["username"],
                CONF_HUE_CLIENT_KEY: self._hue_credentials["clientkey"],
                CONF_HUE_AREA_ID: area.id,
                CONF_HUE_AREA_CHANNELS: [
                    {"channel_id": channel.channel_id, "name": channel.name, "position": list(channel.position), "tv_mapping": self._tv_channel_mappings.get(str(index), "auto")}
                    for index, channel in enumerate(area.channels, 1)
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
        self._hue_credentials: dict[str, str] = {}
        self._hue_areas: list = []
        self._input_mode: str = DEFAULT_INPUT_MODE
        self._tv_channel_mappings: dict[str, str] = {}
        self._mapping_index = 0
        self._paired = False
        self._pairing_task: asyncio.Task | None = None

    async def async_step_init(self, user_input=None):
        """Show lights selection and optional re-pair toggle."""
        input_mode = self.config_entry.options.get(
            CONF_INPUT_MODE, self.config_entry.data.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE)
        )
        backend = self.config_entry.options.get(
            CONF_OUTPUT_BACKEND, self.config_entry.data.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
        )
        configured = self.config_entry.options.get(
            CONF_OUTPUT_CONFIGURED, self.config_entry.data.get(CONF_OUTPUT_CONFIGURED, True)
        )
        if input_mode == INPUT_PHILIPS_JOINTSPACE and backend == BACKEND_HUE:
            if user_input and user_input.get("setup_hue"):
                return await self.async_step_hue_setup_bridge()
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({vol.Optional("setup_hue", default=False): BooleanSelector()}),
                description_placeholders={"hue_status": "Connected" if configured else "Not configured"},
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            self._lights = user_input[CONF_LIGHTS]
            self._backend = user_input.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
            self._input_mode = self.config_entry.options.get(
                CONF_INPUT_MODE, self.config_entry.data.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE)
            )
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

    async def async_step_hue_setup_bridge(self, user_input=None):
        """Continue a deferred physical Hue setup without touching TV settings."""
        bridges = async_known_hue_bridges(self.hass)
        if len(bridges) == 1:
            self._hue_host = bridges[0].host
            return await self.async_step_hue_setup_pair()
        if user_input is not None:
            selected = user_input["ha_hue_bridge"]
            if selected == "manual":
                return await self.async_step_hue_setup_host()
            self._hue_host = next(b.host for b in bridges if b.entry_id == selected)
            return await self.async_step_hue_setup_pair()
        if not bridges:
            return await self.async_step_hue_setup_host()
        return self.async_show_form(step_id="hue_setup_bridge", data_schema=vol.Schema({
            vol.Required("ha_hue_bridge"): SelectSelector(SelectSelectorConfig(options=[
                {"value": b.entry_id, "label": f"{b.name} ({b.host})"} for b in bridges
            ] + [{"value": "manual", "label": "Enter a different Hue Bridge"}], mode=SelectSelectorMode.DROPDOWN))
        }))

    async def async_step_hue_setup_host(self, user_input=None):
        if user_input is not None:
            self._hue_host = str(user_input[CONF_HUE_HOST]).strip()
            if self._hue_host:
                return await self.async_step_hue_setup_pair()
        return self.async_show_form(step_id="hue_setup_host", data_schema=vol.Schema({vol.Required(CONF_HUE_HOST, default=self._hue_host): TextSelector()}))

    async def async_step_hue_setup_pair(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._hue_credentials, self._hue_areas = await async_pair_hue_entertainment(self._hue_host)
                return await self.async_step_hue_setup_area()
            except HueSetupError as err:
                errors["base"] = err.error
        return self.async_show_form(step_id="hue_setup_pair", data_schema=vol.Schema({}), errors=errors)

    async def async_step_hue_setup_area(self, user_input=None):
        if user_input is not None:
            area = next(area for area in self._hue_areas if area.id == user_input[CONF_HUE_AREA_ID])
            options = dict(self.config_entry.options)
            options.update({
                CONF_HUE_HOST: self._hue_host, CONF_HUE_APP_KEY: self._hue_credentials["username"],
                CONF_HUE_CLIENT_KEY: self._hue_credentials["clientkey"], CONF_HUE_AREA_ID: area.id,
                CONF_HUE_AREA_CHANNELS: [{"channel_id": c.channel_id, "name": c.name, "position": list(c.position), "tv_mapping": "auto"} for c in area.channels],
                CONF_OUTPUT_CONFIGURED: True,
            })
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(step_id="hue_setup_area", data_schema=vol.Schema({
            vol.Required(CONF_HUE_AREA_ID): SelectSelector(SelectSelectorConfig(options=[
                {"value": area.id, "label": area.name} for area in self._hue_areas
            ], mode=SelectSelectorMode.DROPDOWN))
        }))

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
                    if self._input_mode == INPUT_PHILIPS_JOINTSPACE:
                        self._mapping_index = 0
                        return await self.async_step_jointspace_mapping_options()
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

    async def async_step_jointspace_mapping_options(self, user_input=None):
        """Edit stored per-channel manual mappings without recreating the entry."""
        channels = self._hue_channels or self.config_entry.options.get(
            CONF_HUE_AREA_CHANNELS, self.config_entry.data.get(CONF_HUE_AREA_CHANNELS, [])
        )
        if user_input is not None:
            self._tv_channel_mappings[str(self._mapping_index + 1)] = user_input["tv_mapping"]
            self._mapping_index += 1
        if self._mapping_index >= len(channels):
            return self.async_create_entry(title="", data=self._options())
        channel = channels[self._mapping_index]
        defaults = self.config_entry.options.get(
            CONF_TV_CHANNEL_MAPPINGS, self.config_entry.data.get(CONF_TV_CHANNEL_MAPPINGS, {})
        )
        return self.async_show_form(
            step_id="jointspace_mapping_options",
            description_placeholders={"name": channel.get("name", "Channel"),
                "channel_id": str(channel.get("channel_id", "")),
                "position": "x={:.2f}, y={:.2f}, z={:.2f}".format(*channel.get("position", (0, 0, 0)))},
            data_schema=vol.Schema({vol.Required("tv_mapping", default=defaults.get(str(self._mapping_index + 1), channel.get("tv_mapping", "auto"))): SelectSelector(
                SelectSelectorConfig(options=list(TV_RELATIVE_POSITIONS), mode=SelectSelectorMode.DROPDOWN))}),
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
        if self._input_mode == INPUT_PHILIPS_JOINTSPACE:
            options[CONF_TV_CHANNEL_MAPPINGS] = self._tv_channel_mappings
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
