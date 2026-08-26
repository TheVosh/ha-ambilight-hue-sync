"""Hue Entertainment Bridge — entertainment mode for HA lights."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from urllib.parse import urlparse

from homeassistant.components.network import async_get_source_ip
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import get_url
from homeassistant.helpers.storage import Store
from homeassistant.util.network import is_loopback

from .config_flow import mac_from_bridge_id
from .const import (
    CONF_API_PORT,
    CONF_BIND_IP,
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
from .entertainment import EntertainmentEngine, FrameMailbox, LightMapping
from .hue_api import HueAPIServer
from .user_store import UserStore

PLATFORMS = [Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hue Entertainment Bridge from a config entry."""
    light_entities: list[str] = entry.options.get(CONF_LIGHTS, entry.data.get(CONF_LIGHTS, []))

    bridge_id: str = entry.data[CONF_BRIDGE_ID]
    mac = mac_from_bridge_id(bridge_id)

    # If a bind IP is explicitly configured, use it directly — no need to probe.
    bind_ip: str | None = entry.options.get(CONF_BIND_IP) or entry.data.get(CONF_BIND_IP)
    if bind_ip:
        host_ip = bind_ip
    else:
        host_ip = await _async_get_host_ip(hass)

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
        bind_ip=bind_ip,
    )

    # DTLS server — always listening; TV may probe before the REST "start" action
    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        if hex_key is None:
            return None
        return bytes.fromhex(hex_key)

    # DTLS library logs under "dtls_psk.server" (separate from this integration).
    # Enable with: logger: logs: dtls_psk.server: debug
    # Frames are handed over through a single-slot mailbox (freshest wins) so a
    # stalled loop never accumulates a backlog of stale frames.
    mailbox = FrameMailbox(hass.loop, engine.handle_frame)
    dtls_server = DTLSPSKServer(
        host=bind_ip or "0.0.0.0",
        port=ent_port,
        psk_callback=psk_lookup,
        frame_callback=mailbox.put,
        loop=None,  # mailbox.put is thread-safe; it schedules onto hass.loop itself
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

    def _cancel_watchdog() -> None:
        nonlocal watchdog_task
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
        watchdog_task = None

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
            # Owned by the entry: cancelled automatically on unload
            watchdog_task = entry.async_create_background_task(
                hass, _frame_watchdog(), name=f"{DOMAIN}_frame_watchdog"
            )

    async def _on_entertainment_stop() -> None:
        _cancel_watchdog()
        await engine.async_restore_lights()
        async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)

    api_server.set_entertainment_callbacks(_on_entertainment_start, _on_entertainment_stop)
    # TV "classic" mode (no DTLS stream): per-light REST commands follow the
    # same Zigbee-paced drain loop.
    api_server.set_light_command_callback(engine.handle_light_command)

    # Store references for teardown and OptionsFlow access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api_server": api_server,
        "dtls_server": dtls_server,
        "discovery": discovery,
        "engine": engine,
        "user_store": user_store,
        "cancel_watchdog": _cancel_watchdog,
    }

    async def _async_start(_event: Event | None = None) -> None:
        """Start servers once HA is fully running."""
        try:
            await api_server.async_start()
            await dtls_server.async_start()
        except OSError as err:
            # Port in use (typically :80) or bind IP not on this host.
            await dtls_server.async_stop()
            await api_server.async_stop()
            raise ConfigEntryNotReady(
                f"Cannot bind Hue bridge ports (http={http_port}, dtls={ent_port}): {err}"
            ) from err
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
        _cancel_watchdog()
        # Stop the servers first: HA's shutdown budget is short, and if the
        # light restore (transition) gets cut off the DTLS thread must not
        # outlive the event loop.
        await dtls_server.async_stop()
        await api_server.async_stop()
        await discovery.async_stop()
        await engine.async_restore_lights()
        _LOGGER.info("Hue Entertainment Bridge stopped")

    if hass.state is CoreState.running:
        await _async_start()
    else:

        async def _async_deferred_start(_event: Event) -> None:
            try:
                await _async_start()
            except ConfigEntryNotReady as err:
                # Too late to fail setup — surface it and let HA retry via reload.
                _LOGGER.error("%s — reloading to retry", err)
                hass.config_entries.async_schedule_reload(entry.entry_id)

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _async_deferred_start
        )

        def _cancel_deferred_start() -> None:
            # A fired one-shot listener has already removed itself; removing it
            # again makes HA log "Unable to remove unknown job listener".
            if hass.state is not CoreState.running:
                unsub_started()

        entry.async_on_unload(_cancel_deferred_start)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Options (lights, bind IP) are resolved once above — reload to apply changes
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data[DOMAIN].pop(entry.entry_id, {})
    if data:
        data["cancel_watchdog"]()
        await data["engine"].async_restore_lights()
        await data["dtls_server"].async_stop()
        await data["api_server"].async_stop()
        await data["discovery"].async_stop()
    return unload_ok


async def _async_get_host_ip(hass: HomeAssistant) -> str:
    """IP to advertise to the TV: HA's internal URL host if it is an IP, else the source IP."""
    try:
        host = urlparse(get_url(hass, prefer_external=False)).hostname
        if host and not is_loopback(host) and _is_ip(host):
            return host
    except Exception:  # noqa: BLE001
        _LOGGER.debug("No usable internal URL; using HA's source IP")
    return await async_get_source_ip(hass)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
