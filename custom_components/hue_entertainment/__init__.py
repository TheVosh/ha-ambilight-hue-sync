"""Hue Entertainment Bridge — entertainment mode for HA lights."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.network import is_loopback

from .config_flow import mac_from_bridge_id
from .const import (
    CONF_API_PORT,
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_HTTP_MODE,
    CONF_LIGHTS,
    CONF_OUTPUT_BACKEND,
    CONF_HUE_HOST,
    CONF_HUE_APP_KEY,
    CONF_HUE_CLIENT_KEY,
    CONF_HUE_AREA_ID,
    CONF_HUE_AREA_CHANNELS,
    CONF_STREAM_FPS,
    CONF_BRIGHTNESS_MULTIPLIER,
    CONF_SATURATION_MULTIPLIER,
    CONF_INPUT_MODE, CONF_TV_HOST, CONF_TV_USERNAME, CONF_TV_PASSWORD, CONF_TV_API_VERSION,
    CONF_TV_PORT, CONF_TV_VERIFY_SSL, CONF_TV_POLL_FPS, CONF_REVERSE_LEFT, CONF_REVERSE_RIGHT,
    CONF_REVERSE_TOP, CONF_REVERSE_BOTTOM, INPUT_PHILIPS_JOINTSPACE, DEFAULT_INPUT_MODE,
    DEFAULT_TV_API_VERSION, DEFAULT_TV_PORT, DEFAULT_TV_POLL_FPS, CONF_TV_CHANNEL_MAPPINGS,
    CONF_TV_INACTIVITY_TIMEOUT, DEFAULT_TV_INACTIVITY_TIMEOUT,
    CONF_OUTPUT_CONFIGURED,
    BACKEND_HUE,
    DEFAULT_OUTPUT_BACKEND,
    DEFAULT_STREAM_FPS,
    DEFAULT_API_PORT,
    DEFAULT_ENTERTAINMENT_PORT,
    DEFAULT_HTTP_MODE,
    DOMAIN,
    FRAME_TIMEOUT,
    FRAME_WATCHDOG_INTERVAL,
    SIGNAL_ENTERTAINMENT_CHANGED,
)
from .discovery import HueBridgeDiscovery
from .dtls_psk import DTLSPSKServer
from .entertainment import EntertainmentEngine, FrameMailbox, LightMapping
from .entertainment import parse_huestream_frame
from .backends import DisabledOutputBackend, EntertainmentOutputBackend, HomeAssistantLightBackend, HueEntertainmentBackend
from .jointspace import PhilipsJointSpaceSource
from .ha_http import async_get_http_host, resolve_use_ha_http
from .hue_api import HueAPIServer
from .user_store import UserStore

PLATFORMS = [Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


@dataclass
class HueEntertainmentData:
    """Runtime objects for one bridge (entry.runtime_data)."""

    bridge_id: str
    api_server: HueAPIServer
    dtls_server: DTLSPSKServer
    discovery: HueBridgeDiscovery
    engine: EntertainmentEngine
    backend: EntertainmentOutputBackend
    user_store: UserStore
    mailbox: FrameMailbox
    cancel_watchdog: Callable[[], None]
    jointspace_source: PhilipsJointSpaceSource | None = None


type HueEntertainmentConfigEntry = ConfigEntry[HueEntertainmentData]


async def async_setup_entry(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> bool:
    """Set up Hue Entertainment Bridge from a config entry."""
    backend_name = entry.options.get(
        CONF_OUTPUT_BACKEND, entry.data.get(CONF_OUTPUT_BACKEND, DEFAULT_OUTPUT_BACKEND)
    )
    input_mode = entry.options.get(CONF_INPUT_MODE, entry.data.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE))
    output_configured = entry.options.get(
        CONF_OUTPUT_CONFIGURED, entry.data.get(CONF_OUTPUT_CONFIGURED, True)
    )
    light_entities: list[str] = entry.options.get(CONF_LIGHTS, entry.data.get(CONF_LIGHTS, []))
    area_channels: list[dict] = entry.options.get(
        CONF_HUE_AREA_CHANNELS, entry.data.get(CONF_HUE_AREA_CHANNELS, [])
    )
    if backend_name == BACKEND_HUE and not output_configured:
        backend: EntertainmentOutputBackend = DisabledOutputBackend()
    elif backend_name == BACKEND_HUE:
        # The physical area's channel layout is persisted at configuration time.
        # It becomes the virtual bridge layout exposed to the TV, so virtual IDs
        # deterministically map by index to native physical channel IDs.
        light_entities = [str(channel.get("name", f"Channel {index}")) for index, channel in enumerate(area_channels, 1)]

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
    http_mode = entry.options.get(CONF_HTTP_MODE, entry.data.get(CONF_HTTP_MODE, DEFAULT_HTTP_MODE))
    use_ha_http = resolve_use_ha_http(hass, http_mode)
    if use_ha_http:
        http_port = hass.http.server_port
        http_host = async_get_http_host(hass)
        if hass.http.ssl_certificate is not None or http_port != 80:
            # Explicit "homeassistant" mode on a setup the TV can't reach: Hue
            # clients only ever try plain HTTP on port 80.
            _LOGGER.warning(
                "Hue API is served by Home Assistant on %s:%d, but Hue clients expect plain HTTP "
                "on port 80 — set Home Assistant's HTTP server port to 80 without a certificate, "
                "or switch the 'Hue API server' option back to Automatic",
                "https" if hass.http.ssl_certificate else "http",
                http_port,
            )
    else:
        http_port = entry.options.get(
            CONF_API_PORT, entry.data.get(CONF_API_PORT, DEFAULT_API_PORT)
        )
        http_host = None

    # Build light channel mappings — v1 uses 1-indexed light IDs, v2 uses 0-indexed
    # channel IDs.  Map both so either protocol version works.
    mappings = [
        LightMapping(channel_id=i + 1, entity_id=entity_id)
        for i, entity_id in enumerate(light_entities)
    ]

    engine = EntertainmentEngine(hass, mappings)
    if backend_name == BACKEND_HUE:
        channel_map = {
            index: int(channel["channel_id"])
            for index, channel in enumerate(area_channels, 1)
            if "channel_id" in channel
        }
        backend: EntertainmentOutputBackend = HueEntertainmentBackend(
            entry.options.get(CONF_HUE_HOST, entry.data.get(CONF_HUE_HOST, "")),
            entry.options.get(CONF_HUE_APP_KEY, entry.data.get(CONF_HUE_APP_KEY, "")),
            entry.options.get(CONF_HUE_CLIENT_KEY, entry.data.get(CONF_HUE_CLIENT_KEY, "")),
            entry.options.get(CONF_HUE_AREA_ID, entry.data.get(CONF_HUE_AREA_ID, "")),
            channel_map,
            fps_cap=int(entry.options.get(CONF_STREAM_FPS, DEFAULT_STREAM_FPS)),
            brightness_multiplier=float(entry.options.get(CONF_BRIGHTNESS_MULTIPLIER, 1.0)),
            saturation_multiplier=float(entry.options.get(CONF_SATURATION_MULTIPLIER, 1.0)),
        )
    else:
        backend = HomeAssistantLightBackend(engine)

    # HA-idiomatic persistent user store
    ha_store: Store[dict[str, dict]] = Store(hass, version=1, key=f"{DOMAIN}.users")
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
    positions = {
        index: tuple(channel.get("position", (0.0, 0.0, 0.0)))
        for index, channel in enumerate(area_channels, 1)
    }

    jointspace_source: PhilipsJointSpaceSource | None = None
    jointspace_backend_started = False
    async def _start_jointspace_backend() -> None:
        nonlocal jointspace_backend_started
        if jointspace_backend_started:
            return
        jointspace_backend_started = True
        try:
            await backend.async_start()
        except Exception:
            jointspace_backend_started = False
            _LOGGER.debug("JointSpace output backend is temporarily unavailable", exc_info=True)

    def _jointspace_frame(colors) -> None:
        hass.async_create_task(_start_jointspace_backend())
        backend.send_frame(colors, 0)

    if input_mode == INPUT_PHILIPS_JOINTSPACE:
        reversed_edges = {
            edge for edge, option in (("left", CONF_REVERSE_LEFT), ("right", CONF_REVERSE_RIGHT),
                                      ("top", CONF_REVERSE_TOP), ("bottom", CONF_REVERSE_BOTTOM))
            if entry.options.get(option, entry.data.get(option, False))
        }
        jointspace_source = PhilipsJointSpaceSource(
            async_get_clientsession(hass),
            entry.options.get(CONF_TV_HOST, entry.data.get(CONF_TV_HOST, "")),
            entry.options.get(CONF_TV_USERNAME, entry.data.get(CONF_TV_USERNAME, "")),
            entry.options.get(CONF_TV_PASSWORD, entry.data.get(CONF_TV_PASSWORD, "")),
            positions, _jointspace_frame,
            api_version=int(entry.options.get(CONF_TV_API_VERSION, DEFAULT_TV_API_VERSION)),
            port=int(entry.options.get(CONF_TV_PORT, DEFAULT_TV_PORT)),
            fps=int(entry.options.get(CONF_TV_POLL_FPS, DEFAULT_TV_POLL_FPS)),
            verify_ssl=bool(entry.options.get(CONF_TV_VERIFY_SSL, False)),
            reversed_edges=reversed_edges,
            manual_mappings={
                index: entry.options.get(CONF_TV_CHANNEL_MAPPINGS, entry.data.get(CONF_TV_CHANNEL_MAPPINGS, {})).get(str(index), channel.get("tv_mapping", "auto"))
                for index, channel in enumerate(area_channels, 1)
            },
        )
        jointspace_source.set_inactivity_callback(
            backend.async_stop,
            float(entry.options.get(CONF_TV_INACTIVITY_TIMEOUT, entry.data.get(CONF_TV_INACTIVITY_TIMEOUT, DEFAULT_TV_INACTIVITY_TIMEOUT))),
        )
    api_server = HueAPIServer(
        bridge_id=bridge_id,
        mac=mac,
        host_ip=host_ip,
        http_port=http_port,
        channel_count=len(light_entities),
        light_entities=light_entities,
        channel_positions=positions,
        user_store=user_store,
        bind_ip=bind_ip,
        http_host=http_host,
    )

    # DTLS server — always listening; TV may probe before the REST "start" action
    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        if hex_key is None:
            return None
        return bytes.fromhex(hex_key)

    # DTLS logs under custom_components.hue_entertainment.dtls_psk.server
    # (enable separately for handshake-level debug).
    # Frames are handed over through a single-slot mailbox (freshest wins) so a
    # stalled loop never accumulates a backlog of stale frames.
    last_frame_time = 0.0
    def _handle_frame(frame: bytes) -> None:
        nonlocal last_frame_time
        parsed = parse_huestream_frame(frame)
        if parsed is None:
            return
        last_frame_time = time.monotonic()
        _version, color_space, channels = parsed
        backend.send_frame(channels, color_space)

    mailbox = FrameMailbox(hass.loop, _handle_frame)
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
            while api_server.entertainment_active:
                await asyncio.sleep(FRAME_WATCHDOG_INTERVAL)
                if not api_server.entertainment_active:
                    break
                elapsed = time.monotonic() - last_frame_time
                if elapsed > FRAME_TIMEOUT:
                    _LOGGER.warning(
                        "No entertainment frames for %.1f seconds, auto-stopping", elapsed
                    )
                    api_server.clear_entertainment()
                    await backend.async_stop()
                    async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
                    break
        except asyncio.CancelledError:
            pass

    async def _on_entertainment_start(username: str) -> None:
        nonlocal watchdog_task
        _LOGGER.info("Entertainment started by %s", username)
        try:
            await backend.async_start()
        except Exception:
            # TV side remains available; a later stream activation retries the physical bridge.
            _LOGGER.warning("Output backend is not ready; retrying on the next TV stream")
        async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
        if watchdog_task is None or watchdog_task.done():
            # Owned by the entry: cancelled automatically on unload
            watchdog_task = entry.async_create_background_task(
                hass, _frame_watchdog(), name=f"{DOMAIN}_frame_watchdog"
            )

    async def _on_entertainment_stop() -> None:
        _cancel_watchdog()
        await backend.async_stop()
        async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)

    api_server.set_entertainment_callbacks(_on_entertainment_start, _on_entertainment_stop)
    # TV "classic" mode (no DTLS stream): per-light REST commands follow the
    # same Zigbee-paced drain loop.
    if isinstance(backend, HomeAssistantLightBackend):
        api_server.set_light_command_callback(backend.handle_light_command)

    # Runtime objects for the platforms, the options flow and diagnostics
    entry.runtime_data = HueEntertainmentData(
        bridge_id=bridge_id,
        api_server=api_server,
        dtls_server=dtls_server,
        discovery=discovery,
        engine=engine,
        backend=backend,
        user_store=user_store,
        mailbox=mailbox,
        cancel_watchdog=_cancel_watchdog,
        jointspace_source=jointspace_source,
    )

    async def _async_start(_event: Event | None = None) -> None:
        """Start only the resources required by the configured TV input."""
        if input_mode != INPUT_PHILIPS_JOINTSPACE:
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
        if jointspace_source is not None:
            try:
                await jointspace_source.async_start()
                _LOGGER.info("JointSpace Ambilight source started")
            except Exception:
                _LOGGER.warning("JointSpace Ambilight source is unavailable; it will retry on reload", exc_info=True)
        _LOGGER.info(
            "Hue Entertainment Bridge started: input=%s, bridge_id=%s, http=%s:%d, dtls=:%d, lights=%d, users=%d",
            input_mode,
            bridge_id,
            "hass" if use_ha_http else "standalone",
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
        if input_mode != INPUT_PHILIPS_JOINTSPACE:
            await dtls_server.async_stop()
            await api_server.async_stop()
            await discovery.async_stop()
        if jointspace_source is not None:
            await jointspace_source.async_close()
        await backend.async_close()
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


async def _async_update_listener(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = entry.runtime_data
    data.cancel_watchdog()
    # Same order as HA shutdown: stop the inputs first so no frame re-dirties a
    # slot while the lights are being restored.
    input_mode = entry.options.get(CONF_INPUT_MODE, entry.data.get(CONF_INPUT_MODE, DEFAULT_INPUT_MODE))
    if input_mode != INPUT_PHILIPS_JOINTSPACE:
        await data.dtls_server.async_stop()
        await data.api_server.async_stop()
        await data.discovery.async_stop()
    if data.jointspace_source is not None:
        await data.jointspace_source.async_close()
    await data.backend.async_close()
    return unload_ok


async def _async_get_host_ip(hass: HomeAssistant) -> str:
    """IP to advertise to the TV: HA's internal URL host if it is an IP, else the source IP."""
    try:
        host = urlparse(get_url(hass, prefer_external=False)).hostname
        if host and _is_ip(host) and not is_loopback(ipaddress.ip_address(host)):
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
