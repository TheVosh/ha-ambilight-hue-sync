"""Hue Entertainment Bridge — entertainment mode for HA lights."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from urllib.parse import urlparse

from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import get_url
from homeassistant.helpers.storage import Store
from homeassistant.util.network import is_loopback

from .config_flow import mac_from_bridge_id
from .const import (
    CONF_API_PORT,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_LIGHTS,
    DEFAULT_API_PORT,
    DEFAULT_ENTERTAINMENT_PORT,
    DOMAIN,
    FRAME_TIMEOUT,
    FRAME_WATCHDOG_INTERVAL,
    SIGNAL_ENTERTAINMENT_CHANGED,
)
from .discovery import HueBridgeDiscovery
from .dtls_psk import DTLSPSKServer
from .entertainment import EntertainmentEngine, LightMapping
from .hue_api import HueAPIServer
from .user_store import UserStore

PLATFORMS = [Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hue Entertainment Bridge from a config entry."""
    light_entities: list[str] = entry.options.get(CONF_LIGHTS, entry.data.get(CONF_LIGHTS, []))

    bridge_id: str = entry.data[CONF_BRIDGE_ID]
    mac = mac_from_bridge_id(bridge_id)

    host_ip = await hass.async_add_executor_job(_get_host_ip, hass)

    # Resolve port config (options take precedence over data, data over defaults)
    ent_port = entry.options.get(
        CONF_ENTERTAINMENT_PORT,
        entry.data.get(CONF_ENTERTAINMENT_PORT, DEFAULT_ENTERTAINMENT_PORT),
    )
    http_port = entry.options.get(
        CONF_API_PORT,
        entry.data.get(CONF_API_PORT, DEFAULT_API_PORT),
    )

    # Build light channel mappings — v1 uses 1-indexed light IDs, v2 uses 0-indexed
    # channel IDs.  Map both so either protocol version works.
    mappings = [
        LightMapping(channel_id=i + 1, entity_id=entity_id)
        for i, entity_id in enumerate(light_entities)
    ]

    engine = EntertainmentEngine(hass, mappings)

    # HA-idiomatic persistent user store
    ha_store = Store(hass, version=1, key=f"{DOMAIN}.users")
    user_store = UserStore(ha_store=ha_store)
    await user_store.async_load()

    # Import users paired during the config flow's pairing step (one-time on first start)
    initial_users: dict = entry.data.get("initial_users", {})
    for username, info in initial_users.items():
        if user_store.get_psk(username) is None:
            user_store.add(username, info["clientkey"], info.get("devicetype", "unknown"))
    if initial_users:
        await user_store.async_save()

    # API server (HTTP only — TV never uses HTTPS)
    api_server = HueAPIServer(
        bridge_id=bridge_id,
        mac=mac,
        host_ip=host_ip,
        http_port=http_port,
        channel_count=len(light_entities),
        light_entities=light_entities,
        user_store=user_store,
    )

    # DTLS server — always listening; TV may probe before the REST "start" action
    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        if hex_key is None:
            return None
        return bytes.fromhex(hex_key)

    # DTLS library logs under "dtls_psk.server" (separate from this integration).
    # Enable with: logger: logs: dtls_psk.server: debug
    dtls_server = DTLSPSKServer(
        host="0.0.0.0",
        port=ent_port,
        psk_callback=psk_lookup,
        frame_callback=engine.handle_frame,
        loop=hass.loop,
    )

    # mDNS discovery — use HA's shared zeroconf instance
    async_zc = await async_get_async_instance(hass)
    discovery = HueBridgeDiscovery(
        bridge_id=bridge_id,
        host_ip=host_ip,
        port=http_port,
        async_zeroconf=async_zc,
    )

    watchdog_task: asyncio.Task | None = None

    async def _frame_watchdog() -> None:
        try:
            while engine.is_active:
                await asyncio.sleep(FRAME_WATCHDOG_INTERVAL)
                if not engine.is_active:
                    break
                elapsed = time.monotonic() - engine.last_frame_time
                if elapsed > FRAME_TIMEOUT:
                    _LOGGER.warning(
                        "No entertainment frames for %.1f seconds, auto-stopping", elapsed
                    )
                    api_server.clear_entertainment()
                    await engine.async_restore_lights()
                    async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
                    break
        except asyncio.CancelledError:
            pass

    async def _on_entertainment_start(username: str) -> None:
        nonlocal watchdog_task
        _LOGGER.info("Entertainment started by %s", username)
        await engine.async_snapshot_lights()
        async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
        if watchdog_task is None or watchdog_task.done():
            watchdog_task = hass.async_create_task(_frame_watchdog())

    async def _on_entertainment_stop() -> None:
        nonlocal watchdog_task
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            watchdog_task = None
        await engine.async_restore_lights()
        async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)

    api_server.set_entertainment_callbacks(_on_entertainment_start, _on_entertainment_stop)

    # Store references for teardown and OptionsFlow access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api_server": api_server,
        "dtls_server": dtls_server,
        "discovery": discovery,
        "engine": engine,
        "user_store": user_store,
    }

    async def _async_start(_event: Event | None = None) -> None:
        """Start servers once HA is fully running."""
        await api_server.async_start()
        await dtls_server.async_start()
        await discovery.async_start()
        _LOGGER.info(
            "Hue Entertainment Bridge started: bridge_id=%s, http=:%d, dtls=:%d, lights=%d, users=%d",
            bridge_id,
            http_port,
            ent_port,
            len(light_entities),
            len(user_store.users),
        )

    async def _async_stop(event: Event) -> None:
        """Clean up on HA shutdown."""
        nonlocal watchdog_task
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            watchdog_task = None
        await engine.async_restore_lights()
        await dtls_server.async_stop()
        await api_server.async_stop()
        await discovery.async_stop()
        _LOGGER.info("Hue Entertainment Bridge stopped")

    if hass.state is CoreState.running:
        await _async_start()
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_start))

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data[DOMAIN].pop(entry.entry_id, {})
    if data:
        await data["engine"].async_restore_lights()
        await data["dtls_server"].async_stop()
        await data["api_server"].async_stop()
        await data["discovery"].async_stop()
    return unload_ok


def _get_host_ip(hass: HomeAssistant) -> str:
    """Get the primary IP address of the HA host."""
    try:
        url = get_url(hass, prefer_external=False)
        parsed = urlparse(url)
        host = parsed.hostname
        if host and not is_loopback(host):
            return host
    except Exception:
        _LOGGER.debug("Could not resolve host IP from HA config, falling back to UDP probe")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()
