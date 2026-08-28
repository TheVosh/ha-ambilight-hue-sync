"""Parse HueStream frames and dispatch colour updates to HA lights."""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.state import async_reproduce_state

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant, State

from .const import (
    BRIGHTNESS_TOLERANCE,
    CIE_TOLERANCE,
    CLASSIC_DRAIN_IDLE,
    COLOR_SPACE_XY,
    DEFAULT_YIELD_SECONDS,
    HUESTREAM_CHANNEL_SIZE,
    HUESTREAM_HEADER,
    HUESTREAM_HEADER_SIZE,
    RESTORE_TIMEOUT,
    RESTORE_TRANSITION,
)

STATE_UNAVAILABLE = "unavailable"  # homeassistant.const, inlined so the module imports without HA

# V1 protocol sizes (not in const.py — only used here)
_V1_HEADER_SIZE = 16
_V1_CHANNEL_SIZE = 9

_LOGGER = logging.getLogger(__name__)


@dataclass
class ChannelColor:
    """Colour state for a single channel.

    Values are raw 16-bit unsigned integers (0-65535) for both RGB and XY modes.
    The interpretation depends on the frame's colorspace byte:
    - RGB: r/g/b are red/green/blue intensities.
    - XY:  r/g are CIE x/y (scaled: x = r / 65535), b is brightness.
    """

    channel_id: int
    r: int
    g: int
    b: int


@dataclass
class LightMapping:
    """Map a channel ID to an HA light entity."""

    channel_id: int
    entity_id: str
    # Tolerance tracking (last dispatched values)
    last_r: int = -1
    last_g: int = -1
    last_b: int = -1
    # Coalesce slot: freshest service_data waiting to be sent
    pending_data: dict[str, Any] | None = field(default=None, repr=False)
    dirty: bool = False
    # Timestamp of last successful send — used to derive dynamic transition
    last_sent: float = 0.0
    # Classic mode (v1 REST): pending_data carries its own transition
    explicit_transition: bool = False
    # Logged once per unavailable spell so the drain loop doesn't spam
    unavailable_logged: bool = False


def _parse_v2_channels(data: bytes) -> list[ChannelColor]:
    """Parse v2 channel data (7 bytes per channel after 52-byte header)."""
    channels = []
    offset = HUESTREAM_HEADER_SIZE
    while offset + HUESTREAM_CHANNEL_SIZE <= len(data):
        channel_id = data[offset]
        val1 = struct.unpack(">H", data[offset + 1 : offset + 3])[0]
        val2 = struct.unpack(">H", data[offset + 3 : offset + 5])[0]
        val3 = struct.unpack(">H", data[offset + 5 : offset + 7])[0]
        channels.append(ChannelColor(channel_id, val1, val2, val3))
        offset += HUESTREAM_CHANNEL_SIZE
    return channels


def _parse_v1_channels(data: bytes) -> list[ChannelColor]:
    """Parse v1 channel data (9 bytes per channel after 16-byte header)."""
    channels = []
    offset = _V1_HEADER_SIZE
    while offset + _V1_CHANNEL_SIZE <= len(data):
        # v1: 1 byte type, 2 bytes light ID, 2+2+2 bytes colour
        light_id = struct.unpack(">H", data[offset + 1 : offset + 3])[0]
        val1 = struct.unpack(">H", data[offset + 3 : offset + 5])[0]
        val2 = struct.unpack(">H", data[offset + 5 : offset + 7])[0]
        val3 = struct.unpack(">H", data[offset + 7 : offset + 9])[0]
        channels.append(ChannelColor(light_id, val1, val2, val3))
        offset += _V1_CHANNEL_SIZE
    return channels


def parse_huestream_frame(
    data: bytes,
) -> tuple[int, int, list[ChannelColor]] | None:
    """Parse a HueStream frame into (version, colorspace, channels).

    Returns None if the frame is invalid (bad magic, too short, unknown version).
    Pure function — no HA dependency.
    """
    if not data.startswith(HUESTREAM_HEADER):
        return None

    # Need at least 15 bytes to read version (byte 9) and colorspace (byte 14)
    if len(data) < 15:
        return None

    api_version = data[9]
    color_space = data[14]

    if api_version == 0x02:
        if len(data) < HUESTREAM_HEADER_SIZE:
            return None
        channels = _parse_v2_channels(data)
    elif api_version == 0x01:
        if len(data) < _V1_HEADER_SIZE + _V1_CHANNEL_SIZE:
            return None
        channels = _parse_v1_channels(data)
    else:
        _LOGGER.warning("Unknown HueStream API version: %d", api_version)
        return None

    return (api_version, color_space, channels)


def _v1_state_to_service_data(body: dict[str, Any]) -> dict[str, Any]:
    """Translate a Hue v1 light state body into light.turn_on/turn_off data."""
    data: dict[str, Any] = {}
    if "transitiontime" in body:
        try:
            data["transition"] = max(int(body["transitiontime"]), 0) / 10
        except (TypeError, ValueError):
            pass
    if body.get("on") is False:
        data["_service"] = "turn_off"
        return data
    if "bri" in body:
        try:
            bri = int(body["bri"])
        except (TypeError, ValueError):
            bri = 254
        data["brightness"] = max(1, min(255, round(bri * 255 / 254)))
    xy = body.get("xy")
    if isinstance(xy, list) and len(xy) == 2:
        data["xy_color"] = [float(xy[0]), float(xy[1])]
    elif "hue" in body or "sat" in body:
        hue = float(body.get("hue", 0)) / 65535 * 360
        sat = float(body.get("sat", 254)) / 254 * 100
        data["hs_color"] = [round(hue, 2), round(sat, 2)]
    elif "ct" in body:
        try:
            mired = int(body["ct"])
            if mired > 0:
                data["color_temp_kelvin"] = round(1_000_000 / mired)
        except (TypeError, ValueError):
            pass
    if not data and body.get("on") is not True:
        return {}
    data["_service"] = "turn_on"
    return data


class FrameMailbox:
    """Single-slot hand-off from the DTLS thread to the event loop.

    The TV sends ~25 frames/s; if the loop ever stalls, queuing every frame
    with ``call_soon_threadsafe`` would let a backlog build.  Keeping only the
    freshest frame and scheduling at most one loop callback at a time bounds
    the work to one parse per loop iteration.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, handler: Callable[[bytes], None]) -> None:
        self._loop = loop
        self._handler = handler
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._scheduled = False
        self.coalesced = 0  # frames superseded before the loop got to them

    def put(self, frame: bytes) -> None:
        """Thread-safe: store the freshest frame and wake the loop once."""
        with self._lock:
            if self._latest is not None:
                self.coalesced += 1
            self._latest = frame
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._deliver)
        except RuntimeError:  # event loop closed (HA shutting down)
            with self._lock:
                self._scheduled = False

    def _deliver(self) -> None:
        with self._lock:
            frame = self._latest
            self._latest = None
            self._scheduled = False
        if frame is not None:
            self._handler(frame)


class EntertainmentEngine:
    """Process HueStream frames and update HA lights.

    Frames arrive from the DTLS thread at ~25fps.  The engine throttles to
    TARGET_FPS, applies tolerance-based dedup, and writes the freshest colour
    into a per-light slot.  A background drain loop sends one Zigbee command at
    a time (round-robin, adaptive rate) so the radio is never overloaded.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        light_mappings: list[LightMapping],
        notify: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._mappings = {m.channel_id: m for m in light_mappings}
        # Fired on every state transition (session start/stop, drain
        # start/stop, pause/resume/release) — the single source of truth
        # the binary_sensor/status sensor listen to. Never delayed behind
        # anything that can hang (e.g. the light restore) — see
        # async_restore_lights.
        self._notify = notify or (lambda: None)
        self._total_frames_received = 0
        self._total_commands_sent = 0
        self._window_received = 0
        self._window_commands = 0
        self._fps_time = time.monotonic()
        self._first_frame_logged = False
        self.last_frame_time: float = 0.0
        self._active: bool = False
        self._saved_states: list[State] | None = None
        self._drain_task: asyncio.Task | None = None
        # True for as long as the drain loop is doing work — set synchronously
        # when the task is created, cleared in the loop's own `finally` (which
        # runs on every exit path, not just the clean one). Task.done() is
        # NOT used for this: it only flips after the coroutine has fully
        # unwound, which is too late — a command arriving in that window
        # would find done()==False and be silently dropped with no new task
        # started. See _ensure_drain_task / _drain_loop.
        self._drain_running: bool = False
        self._wake = asyncio.Event()  # set whenever a slot becomes dirty

        # --- pause() / release() state — see README "Pause, resume, release"
        # for the full caller-facing contract these fields implement.
        self._paused_until: float | None = None  # monotonic deadline; None = not paused
        self._pause_cancel: Callable[[], None] | None = None
        self._releasing: bool = False
        # True once the release grace period has elapsed without the TV
        # voluntarily disconnecting — from this point handle_frame stops
        # feeding last_frame_time, so the *existing* FRAME_TIMEOUT watchdog
        # detects "silence" and tears the session down through the normal
        # path. No separate forced-teardown mechanism needed.
        self._release_forcing: bool = False
        self._release_cancel: Callable[[], None] | None = None

    def _log_fps(self, now: float) -> None:
        """Log FPS stats if the 5-second window has elapsed, then reset counters."""
        if now - self._fps_time < 5.0:
            return
        elapsed = now - self._fps_time
        rx_fps = self._window_received / elapsed
        cmd_fps = self._window_commands / elapsed
        dirty = sum(1 for m in self._mappings.values() if m.dirty)
        _LOGGER.debug(
            "Entertainment: %.1f fps in, %.1f cmd/s, dirty=%d",
            rx_fps,
            cmd_fps,
            dirty,
        )
        self._window_received = 0
        self._window_commands = 0
        self._fps_time = now

    def handle_frame(self, data: bytes) -> None:
        """Parse a HueStream frame and update per-light colour slots.

        Every valid frame overwrites the per-light slots with the freshest
        colour.  There is no throttle here — the adaptive drain loop controls
        how fast commands actually reach the Zigbee radio.
        """
        parsed = parse_huestream_frame(data)
        if parsed is None:
            return

        now = time.monotonic()
        if not self._release_forcing:
            # Update last_frame_time on every valid frame so the watchdog can
            # detect silence. Once forcing a release, this deliberately stops:
            # pretending the frames went silent is what makes the *existing*
            # watchdog tear the session down for us — see async_release().
            self.last_frame_time = now

        api_version, color_space, channels = parsed

        self._total_frames_received += 1
        self._window_received += 1

        if not self._first_frame_logged:
            self._first_frame_logged = True
            cs = "XY" if color_space == COLOR_SPACE_XY else "RGB"
            ch = ", ".join(f"ch{c.channel_id}=({c.r},{c.g},{c.b})" for c in channels)
            _LOGGER.info(
                "First HueStream frame: v%d %s [%s] (%d bytes)",
                api_version,
                cs,
                ch,
                len(data),
            )

        self._log_fps(now)

        self.handle_channels(channels, color_space)

    def handle_channels(self, channels: list[ChannelColor], color_space: int) -> None:
        """Accept already-normalized channels from the shared frame router."""
        if self._suppressed:
            # Paused or releasing: the frame still counts toward the session
            # totals in handle_frame (an honest count of what the TV sent),
            # but its effect on the lights is dropped.
            return

        # Write freshest colour into per-light slots (drain loop sends them)
        for channel in channels:
            self._schedule_update(channel, color_space)

    @property
    def stats(self) -> dict[str, Any]:
        """Counters for diagnostics."""
        return {
            "active": self._active,
            "status": self.status,
            "status_attributes": self.status_attributes,
            "lights": [m.entity_id for m in self._mappings.values()],
            "session_frames_received": self._total_frames_received,
            "session_commands_sent": self._total_commands_sent,
            "seconds_since_last_frame": (
                round(time.monotonic() - self.last_frame_time, 1) if self.last_frame_time else None
            ),
            "unavailable_lights": [
                m.entity_id for m in self._mappings.values() if m.unavailable_logged
            ],
        }

    @property
    def is_active(self) -> bool:
        """True while a DTLS entertainment stream is in progress."""
        return self._active

    @property
    def _is_paused(self) -> bool:
        """Wall-clock derived, not a raw flag flipped by a timer callback.

        A pause can never outlive its own deadline even if the scheduled
        expiry callback is somehow lost — the exact "stuck forever, no
        signal" shape this integration's other bugs had. The callback exists
        only to fire `_notify()` promptly; correctness doesn't depend on it.
        """
        return self._paused_until is not None and time.monotonic() < self._paused_until

    @property
    def _suppressed(self) -> bool:
        """True while paused or releasing — frame/command effects are dropped."""
        return self._is_paused or self._releasing

    @property
    def is_driving_lights(self) -> bool:
        """True while the bridge is actually writing to these lights right now.

        Broader than `is_active`: also true during classic-mode (plain REST,
        no DTLS stream) sessions. False while paused or releasing, even if
        the underlying session/traffic technically continues — the whole
        point of pausing or releasing is "don't count on the bridge right
        now," so callers deciding whether to treat these lights as claimed
        should see False. See `status` for *why* it's false.
        """
        return not self._suppressed and (self._active or self._drain_running)

    @property
    def status(self) -> str:
        """One of: idle, streaming, classic, paused, releasing.

        Single source of truth for the status sensor, derived from the more
        primitive fields below rather than tracked as its own variable — so
        it can never drift out of sync with what those fields actually say.
        Precedence: paused/releasing (a suppression request) always displays
        over whatever activity happens to be live underneath it; the
        underlying activity is still visible via `status_attributes`.
        """
        if self._releasing:
            return "releasing"
        if self._is_paused:
            return "paused"
        if self._active:
            return "streaming"
        if self._drain_running:
            return "classic"
        return "idle"

    @property
    def status_attributes(self) -> dict[str, Any]:
        """Diagnostic detail behind `status` — how long, and underneath what."""
        attrs: dict[str, Any] = {}
        if self._is_paused and self._paused_until is not None:
            attrs["paused_remaining_seconds"] = round(self._paused_until - time.monotonic(), 1)
        if self._releasing:
            attrs["release_forcing"] = self._release_forcing
        if self._releasing or self._is_paused:
            attrs["underlying_activity"] = (
                "streaming" if self._active else ("classic" if self._drain_running else "idle")
            )
        return attrs

    def reset_stats(self) -> None:
        """Log session totals and reset counters (call when streaming stops)."""
        if self._total_frames_received > 0:
            _LOGGER.info(
                "Entertainment session: %d frames received, %d commands sent to lights",
                self._total_frames_received,
                self._total_commands_sent,
            )
        self._total_frames_received = 0
        self._total_commands_sent = 0
        self._window_received = 0
        self._window_commands = 0
        self._fps_time = time.monotonic()
        self._first_frame_logged = False
        self._reset_mappings()

    def _reset_mappings(self) -> None:
        """Forget per-light send state so the next session starts from scratch.

        Without this, the first frames of a new session are suppressed by the
        tolerance check whenever they resemble the last frames of the previous
        one (e.g. the TV's home screen), and the lights stay in their restored
        state until the picture changes enough.
        """
        for m in self._mappings.values():
            m.dirty = False
            m.pending_data = None
            m.last_r = m.last_g = m.last_b = -1
            m.last_sent = 0.0

    def _ensure_drain_task(self) -> None:
        if not self._drain_running:
            self._drain_task = self._hass.async_create_task(self._drain_loop())
            self._drain_running = True
            self._notify()

    async def async_snapshot_lights(self) -> None:
        """Snapshot current light states so they can be restored after entertainment."""
        if self._releasing:
            # A new session starting while we were waiting for the old one to
            # end (or force it) IS that resolution — the TV showing up again
            # is exactly what release() was waiting for. Resolve it and fall
            # through to a normal, fresh snapshot rather than treating this
            # as "already active" below (there is nothing stale left to keep:
            # release() already discarded the old restore target).
            self._cancel_release()
        elif self._active:
            # A second stream.active=true mid-session (TV re-toggle) must not
            # overwrite the pre-entertainment snapshot with streaming colours.
            _LOGGER.debug("Entertainment already active; keeping existing snapshot")
            return
        states: list[State] = []
        for mapping in self._mappings.values():
            state = self._hass.states.get(mapping.entity_id)
            if state is not None:
                states.append(state)
        self._saved_states = states
        self._reset_mappings()
        self._active = True
        self.last_frame_time = time.monotonic()
        self._notify()
        self._ensure_drain_task()
        _LOGGER.info("Snapshotted %d light states for restore", len(states))

    async def async_restore_lights(self) -> None:
        """Restore lights to their pre-entertainment state (idempotent).

        Flips `_active` and notifies listeners *before* attempting the
        restore itself — a light that never answers the restore call must
        not leave the sensor reporting stale "active" state. The restore is
        bounded by RESTORE_TIMEOUT for the same reason.

        Also the one place every teardown path converges: the TV's own clean
        disconnect, the frame watchdog (silence — genuine, or manufactured by
        an in-progress release), and unload/shutdown all end up here. Any
        pending release is resolved here too, whichever end-trigger fired.
        """
        if not self._active:
            return
        self._active = False
        self._cancel_release()
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        # The drain loop's own `finally` resets this on every exit — except
        # when the task was cancelled before its first step ever ran (session
        # ended in the same event-loop turn it started), in which case the
        # coroutine body never executes at all. Reset it here too, or
        # _ensure_drain_task would refuse to start a fresh loop for the next
        # classic command and the sensor would report "classic" forever.
        self._drain_running = False
        self._notify()
        saved = self._saved_states
        self._saved_states = None
        self.reset_stats()
        if saved:
            try:
                await asyncio.wait_for(
                    async_reproduce_state(
                        self._hass,
                        saved,
                        reproduce_options={"transition": RESTORE_TRANSITION},
                    ),
                    timeout=RESTORE_TIMEOUT,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "Restoring %d lights timed out after %.1fs", len(saved), RESTORE_TIMEOUT
                )
                return
            except Exception as err:  # noqa: BLE001 — best effort (e.g. ZHA already down at shutdown)
                _LOGGER.warning("Could not restore %d lights: %s", len(saved), err)
                return
            _LOGGER.info("Restored %d lights to pre-entertainment state", len(saved))

    async def async_pause(self, seconds: float) -> None:
        """Suppress frame/command effects for `seconds`, then resume automatically.

        A courtesy gap for radio contention — unlike release(), no intent
        about these lights has changed, so nothing about the session (if any)
        is touched: last_frame_time keeps advancing normally, and no restore
        target is discarded. Never blocks a session from starting — a DTLS
        handshake or classic command still completes normally while paused;
        only the resulting light effect is dropped (see `handle_frame` /
        `handle_light_command`).

        Contract: a `release()` already in progress wins — pausing on top of
        it would let its later auto-expiry silently cancel the release, so
        this is a no-op while releasing. Calling pause() again while already
        paused resets the timer to a fresh `seconds` from now (last call
        wins), regardless of how much time was left on the previous one.

        Flushes any command already queued (dirty/pending on a mapping) but
        not yet sent. The gate in handle_frame/handle_light_command only
        blocks *new* commands from being queued while suppressed — it never
        touched what was already sitting in the pipe. Against a live ~25fps
        stream something is queued almost continuously, so without this a
        pause/release would routinely be undone within a couple hundred
        milliseconds by a command that was queued a moment before suppression
        began: found live 2026-08-28, a released light relit ~770ms after
        being swept off while the TV kept streaming. One narrower gap
        remains and is NOT fixed by this: a command already past this point
        and being sent by the drain loop (mid-await on the Zigbee service
        call) cannot be recalled — a byte already on the wire stays sent.
        _reset_mappings() also clears tolerance tracking, which is correct
        here too: the first command after suppression ends should snap to
        the live colour immediately, not fade from a timestamp that's now
        seconds stale.
        """
        if self._releasing:
            _LOGGER.debug("pause() ignored: a release is already in progress")
            return
        self._cancel_pause()
        self._reset_mappings()
        resolved = seconds if seconds > 0 else DEFAULT_YIELD_SECONDS
        self._paused_until = time.monotonic() + resolved
        self._notify()
        self._pause_cancel = async_call_later(self._hass, resolved, self._on_pause_expired)

    async def async_resume(self) -> None:
        """Cancel an in-progress pause early. No-op if not paused, or if releasing.

        Releasing has no caller-triggered counterpart — see async_release().
        """
        if self._releasing or self._paused_until is None:
            return
        self._cancel_pause()
        self._notify()

    async def async_release(self, seconds: float) -> None:
        """Stop driving these lights until a new session genuinely begins.

        Unlike pause, this discards the pending restore target immediately:
        whatever the caller's sweep left the lights doing is now correct,
        and there is nothing to restore back to. Frame/command effects are
        dropped immediately too.

        `seconds` is a polite grace period, paired with the API-visible
        `stream.active` flag the caller flips separately (see the `release`
        service in __init__.py) — a compliant TV notices and disconnects on
        its own within it, producing a clean, ordinary teardown through
        async_restore_lights. If it doesn't, this stops feeding
        last_frame_time once the grace period elapses, so the *existing*
        FRAME_TIMEOUT watchdog detects "silence" and forces the same
        teardown — no separate forced-disconnect mechanism needed. Either
        way, this integration never leaves you waiting on the TV forever:
        worst case is `seconds` + FRAME_TIMEOUT.

        Without a DTLS session (classic mode, or nothing at all) there is no
        watchdog to lean on, so the grace timer resolves the release itself
        when it expires: classic commands are dropped for `seconds`, then
        the next one drives the lights again.

        Contract: supersedes an in-progress pause (an intent change always
        wins over a courtesy gap). Calling release() again while already
        releasing restarts the grace period — safe for a caller unsure
        whether an earlier call landed.

        Flushes any command already queued but not yet sent — see
        async_pause's docstring for why this is needed (a live stream keeps
        something queued almost continuously) and the one narrower gap it
        doesn't close (a command already mid-send when release() is called).
        For release specifically this is what makes the whole point of the
        service actually hold: without it, whatever the caller's sweep just
        set could be undone within a fraction of a second by a stale queued
        command, even though the intent (see above) already discarded the
        restore target and is not coming back.
        """
        self._cancel_pause()
        self._reset_mappings()
        resolved = seconds if seconds > 0 else DEFAULT_YIELD_SECONDS
        self._saved_states = None  # nothing to restore — the caller's new state IS correct
        self._releasing = True
        self._release_forcing = False
        self._notify()
        if self._release_cancel is not None:
            self._release_cancel()
        self._release_cancel = async_call_later(
            self._hass, resolved, self._on_release_grace_expired
        )

    def _cancel_pause(self) -> None:
        self._paused_until = None
        if self._pause_cancel is not None:
            self._pause_cancel()
            self._pause_cancel = None

    def _cancel_release(self) -> None:
        was_releasing = self._releasing
        self._releasing = False
        self._release_forcing = False
        if self._release_cancel is not None:
            self._release_cancel()
            self._release_cancel = None
        if was_releasing:
            self._notify()

    @callback
    def _on_pause_expired(self, _now: datetime) -> None:
        self._paused_until = None
        self._pause_cancel = None
        self._notify()

    @callback
    def _on_release_grace_expired(self, _now: datetime) -> None:
        self._release_cancel = None
        if not self._releasing:
            return  # already resolved (TV reconnected, or restore already ran)
        if not self._active:
            # Classic mode or idle: there is no DTLS session for the watchdog
            # to tear down, so nothing else would ever resolve this release.
            # The grace period is the whole guarantee here — commands were
            # dropped for its duration; the next one drives the lights again.
            self._cancel_release()
            return
        self._release_forcing = True
        self._notify()

    async def _drain_loop(self) -> None:
        """Adaptive round-robin: send the freshest colour per light, one at a time.

        Sends one blocking service call, waits for ZHA to complete, then moves
        to the next dirty light.  This naturally adapts to the Zigbee radio's
        throughput — no timer to tune.  Lights always get the most recent colour.
        """
        mappings = list(self._mappings.values())
        idle_since: float | None = None
        try:
            await self._drain_until_idle_or_cancelled(mappings, idle_since)
        finally:
            # Runs on every exit — the clean idle-timeout return, a
            # cancellation, or any other exception escaping the loop below.
            # Without this, an exception other than CancelledError would
            # leave _drain_running stuck True forever: no task, nothing to
            # cancel it, and _ensure_drain_task's guard would then refuse to
            # ever start a fresh one for a later classic command.
            self._drain_running = False
            self._notify()

    async def _drain_until_idle_or_cancelled(
        self, mappings: list[LightMapping], idle_since: float | None
    ) -> None:
        try:
            while True:
                self._wake.clear()
                sent_any = False
                for mapping in mappings:
                    if not mapping.dirty:
                        continue
                    # Grab and clear the slot atomically (single-threaded event loop)
                    data = mapping.pending_data
                    explicit = mapping.explicit_transition
                    mapping.dirty = False
                    mapping.pending_data = None
                    mapping.explicit_transition = False
                    if data is None:
                        continue
                    if not self._light_available(mapping):
                        continue
                    sent_any = True
                    self._total_commands_sent += 1
                    self._window_commands += 1
                    now = time.monotonic()
                    service = data.pop("_service", "turn_on")
                    if not explicit:
                        # Dynamic transition: fade over the time since this
                        # light's last update so colour ramps instead of stepping.
                        if mapping.last_sent > 0:
                            interval = now - mapping.last_sent
                            # Clamp to [0.1, 2.0]s — avoid 0 (snappy) or huge (first cmd)
                            data["transition"] = min(max(round(interval, 1), 0.1), 2.0)
                        else:
                            data["transition"] = 0  # first command: snap immediately
                    mapping.last_sent = now
                    try:
                        await self._hass.services.async_call("light", service, data, blocking=True)
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("Failed to update %s", mapping.entity_id, exc_info=True)
                if sent_any:
                    idle_since = None
                    continue
                if not self._active:
                    # Classic mode (no stream): exit once nothing has been
                    # queued for a while; the next command restarts the loop.
                    now = time.monotonic()
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since > CLASSIC_DRAIN_IDLE:
                        return
                # Nothing dirty — sleep until new data is slotted (or the idle check is due)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=CLASSIC_DRAIN_IDLE)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    def _light_available(self, mapping: LightMapping) -> bool:
        """Skip lights HA reports as unavailable (a blocking call would stall the loop)."""
        state = self._hass.states.get(mapping.entity_id)
        if state is not None and state.state == STATE_UNAVAILABLE:
            if not mapping.unavailable_logged:
                _LOGGER.warning("%s is unavailable; skipping until it returns", mapping.entity_id)
                mapping.unavailable_logged = True
            return False
        if mapping.unavailable_logged:
            _LOGGER.info("%s is available again", mapping.entity_id)
            mapping.unavailable_logged = False
        return True

    def handle_light_command(self, light_id: int, body: dict[str, Any]) -> None:
        """Apply a v1 ``PUT /lights/{id}/state`` body (TV classic mode).

        Successive PUTs for one light are merged into its slot (the TV sends
        ``xy`` and ``bri`` as separate requests) and drained at Zigbee pace.
        No snapshot/restore: like a real bridge, the lights just follow.
        """
        if self._suppressed:
            # Paused or releasing: drop it, don't queue it. Classic mode is a
            # continuous stream of updates like DTLS frames are — the TV will
            # send another one on its next paint tick once suppression ends,
            # so there's nothing worth buffering.
            return
        mapping = self._mappings.get(light_id)
        if mapping is None:
            return
        data = _v1_state_to_service_data(body)
        if not data:
            return
        pending = (
            mapping.pending_data
            if mapping.dirty and mapping.pending_data and mapping.explicit_transition
            else {}  # nothing queued, or a stream-mode slot: start fresh
        )
        if data.get("_service") == "turn_off":
            pending = {}  # anything queued before an "off" is moot
        elif pending.get("_service") == "turn_off":
            if body.get("on") is not True:
                return  # colour tweaks while the light is being turned off are moot
            pending = {}  # explicit on: supersedes the queued off
        pending.update(data)
        pending["entity_id"] = mapping.entity_id
        mapping.pending_data = pending
        mapping.explicit_transition = True
        mapping.dirty = True
        self._wake.set()
        self._ensure_drain_task()

    def _schedule_update(self, channel: ChannelColor, color_space: int) -> None:
        """Write the freshest colour into a light's slot if it changed enough.

        Does not set ``transition`` — the drain loop sets it dynamically based
        on the measured interval between commands to each light.
        """
        mapping = self._mappings.get(channel.channel_id)
        if not mapping:
            return

        if color_space == COLOR_SPACE_XY:
            # Convert 16-bit values to CIE xy + brightness
            x = channel.r / 65535.0
            y = channel.g / 65535.0
            bri = channel.b

            # Check tolerance
            last_x = mapping.last_r / 65535.0 if mapping.last_r >= 0 else -1
            last_y = mapping.last_g / 65535.0 if mapping.last_g >= 0 else -1
            if (
                abs(x - last_x) < CIE_TOLERANCE
                and abs(y - last_y) < CIE_TOLERANCE
                and abs(bri - mapping.last_b) < BRIGHTNESS_TOLERANCE
            ):
                return

            mapping.last_r = channel.r
            mapping.last_g = channel.g
            mapping.last_b = channel.b

            # Scale brightness to 0-255
            brightness = round(bri / 65535 * 255)
            service_data = {
                "entity_id": mapping.entity_id,
                "xy_color": [x, y],
                "brightness": brightness,
            }
        else:
            # RGB mode
            r = channel.r
            g = channel.g
            b = channel.b

            if (
                abs(r - mapping.last_r) < BRIGHTNESS_TOLERANCE
                and abs(g - mapping.last_g) < BRIGHTNESS_TOLERANCE
                and abs(b - mapping.last_b) < BRIGHTNESS_TOLERANCE
            ):
                return

            mapping.last_r = r
            mapping.last_g = g
            mapping.last_b = b

            # Scale 16-bit to 8-bit; derive brightness from peak channel
            brightness = max(r, g, b) >> 8 or 1  # at least 1 to keep the light on
            service_data = {
                "entity_id": mapping.entity_id,
                "rgb_color": [r >> 8, g >> 8, b >> 8],
                "brightness": brightness,
            }

        # Write into the slot — drain loop picks up the freshest value
        mapping.pending_data = service_data
        mapping.dirty = True
        self._wake.set()
