"""Config flow for Hue Entertainment Bridge."""

from __future__ import annotations

import asyncio
import socket
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
)

from .const import CONF_BRIDGE_ID, CONF_LIGHTS, CONF_PAIR_NOW, DEFAULT_API_PORT, DOMAIN
from .discovery import HueBridgeDiscovery
from .hue_api import HueAPIServer
from .user_store import UserStore

PAIRING_TIMEOUT = 60  # seconds


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


def _probe_host_ip() -> str:
    """Get local IP via UDP routing probe (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def mac_from_bridge_id(bridge_id: str) -> str:
    """Derive a colon-separated MAC from a 16-char Hue bridge ID."""
    hex12 = bridge_id.replace("FFFE", "")[:12].lower()
    return ":".join(hex12[i : i + 2] for i in range(0, 12, 2))


class HueEntertainmentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Hue Entertainment Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        self._lights: list[str] = []
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
        """Step 1: select lights."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            self._lights = user_input[CONF_LIGHTS]
            raw = uuid.uuid4().hex[:12].upper()
            self._bridge_id = raw[:6] + "FFFE" + raw[6:]
            return await self.async_step_pre_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHTS): EntitySelector(
                        EntitySelectorConfig(domain="light", multiple=True)
                    ),
                }
            ),
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
            host_ip = await self.hass.async_add_executor_job(_probe_host_ip)
            self._temp_user_store = UserStore()
            self._temp_api = HueAPIServer(
                bridge_id=self._bridge_id,
                mac=mac_from_bridge_id(self._bridge_id),
                host_ip=host_ip,
                http_port=DEFAULT_API_PORT,
                channel_count=len(self._lights),
                light_entities=self._lights,
                user_store=self._temp_user_store,
            )
            await self._temp_api.async_start()
            self._temp_api.set_link_button(True)

            async_zc = await async_get_async_instance(self.hass)
            self._temp_discovery = HueBridgeDiscovery(
                bridge_id=self._bridge_id,
                host_ip=host_ip,
                port=DEFAULT_API_PORT,
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
        return self.async_create_entry(
            title="Hue Entertainment Bridge",
            data={
                CONF_LIGHTS: self._lights,
                CONF_BRIDGE_ID: self._bridge_id,
                "initial_users": initial_users,
            },
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Hue Entertainment Bridge."""

    def __init__(self) -> None:
        super().__init__()
        self._lights: list[str] = []
        self._paired = False
        self._pairing_task: asyncio.Task | None = None

    async def async_step_init(self, user_input=None):
        """Show lights selection and optional re-pair toggle."""
        if user_input is not None:
            self._lights = user_input[CONF_LIGHTS]
            if user_input.get(CONF_PAIR_NOW, False):
                return await self.async_step_pairing()
            return self.async_create_entry(title="", data={CONF_LIGHTS: self._lights})

        current_lights = self.config_entry.options.get(
            CONF_LIGHTS, self.config_entry.data.get(CONF_LIGHTS, [])
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHTS, default=current_lights): EntitySelector(
                        EntitySelectorConfig(domain="light", multiple=True)
                    ),
                    vol.Optional(CONF_PAIR_NOW, default=False): BooleanSelector(),
                }
            ),
        )

    async def async_step_pairing(self, user_input=None):
        """Open the link button and wait for the TV to pair."""
        if self._pairing_task is None:
            entry_data = self.hass.data[DOMAIN][self.config_entry.entry_id]
            api_server: HueAPIServer = entry_data["api_server"]
            user_store: UserStore = entry_data["user_store"]
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
        return self.async_create_entry(title="", data={CONF_LIGHTS: self._lights})

    async def async_step_not_paired(self, user_input=None):
        """Pairing timed out — acknowledge then save options."""
        if user_input is not None:
            return self.async_create_entry(title="", data={CONF_LIGHTS: self._lights})
        return self.async_show_form(step_id="not_paired", data_schema=vol.Schema({}))
