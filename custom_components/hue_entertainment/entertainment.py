"""Parse HueStream frames and dispatch colour updates to HA lights."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

from .const import (
    BRIGHTNESS_TOLERANCE,
    CIE_TOLERANCE,
    COLOR_SPACE_XY,
    HUESTREAM_CHANNEL_SIZE,
    HUESTREAM_HEADER,
    HUESTREAM_HEADER_SIZE,
    RESTORE_TRANSITION,
)

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
    ) -> None:
        self._hass = hass
        self._mappings = {m.channel_id: m for m in light_mappings}
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

        # Update last_frame_time on every valid frame so the watchdog can detect silence
        now = time.monotonic()
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

        # Write freshest colour into per-light slots (drain loop sends them)
        for channel in channels:
            self._schedule_update(channel, color_space)

    @property
    def is_active(self) -> bool:
        """True while entertainment mode is in progress."""
        return self._active

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
        # Clear dirty flags
        for m in self._mappings.values():
            m.dirty = False
            m.pending_data = None

    async def async_snapshot_lights(self) -> None:
        """Snapshot current light states so they can be restored after entertainment."""
        states: list[State] = []
        for mapping in self._mappings.values():
            state = self._hass.states.get(mapping.entity_id)
            if state is not None:
                states.append(state)
        self._saved_states = states
        self._active = True
        self.last_frame_time = time.monotonic()
        # Start the adaptive drain loop
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = self._hass.async_create_task(self._drain_loop())
        _LOGGER.info("Snapshotted %d light states for restore", len(states))

    async def async_restore_lights(self) -> None:
        """Restore lights to their pre-entertainment state (idempotent)."""
        if not self._active:
            return
        self._active = False
        # Stop the drain loop
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        saved = self._saved_states
        self._saved_states = None
        self.reset_stats()
        if saved:
            from homeassistant.helpers.state import async_reproduce_state  # noqa: PLC0415

            await async_reproduce_state(
                self._hass,
                saved,
                reproduce_options={"transition": RESTORE_TRANSITION},
            )
            _LOGGER.info("Restored %d lights to pre-entertainment state", len(saved))

    async def _drain_loop(self) -> None:
        """Adaptive round-robin: send the freshest colour per light, one at a time.

        Sends one blocking service call, waits for ZHA to complete, then moves
        to the next dirty light.  This naturally adapts to the Zigbee radio's
        throughput — no timer to tune.  Lights always get the most recent colour.
        """
        mappings = list(self._mappings.values())
        try:
            while self._active:
                sent_any = False
                for mapping in mappings:
                    if not self._active:
                        return
                    if not mapping.dirty:
                        continue
                    # Grab and clear the slot atomically (single-threaded event loop)
                    data = mapping.pending_data
                    mapping.dirty = False
                    mapping.pending_data = None
                    if data is None:
                        continue
                    sent_any = True
                    self._total_commands_sent += 1
                    self._window_commands += 1
                    # Dynamic transition: fade over the time since this light's
                    # last update so the colour ramps smoothly instead of stepping.
                    now = time.monotonic()
                    if mapping.last_sent > 0:
                        interval = now - mapping.last_sent
                        # Clamp to [0.1, 2.0]s — avoid 0 (snappy) or huge (first cmd)
                        data["transition"] = min(max(round(interval, 1), 0.1), 2.0)
                    else:
                        data["transition"] = 0  # first command: snap immediately
                    mapping.last_sent = now
                    try:
                        await self._hass.services.async_call(
                            "light", "turn_on", data, blocking=True
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("Failed to update %s", mapping.entity_id, exc_info=True)
                if not sent_any:
                    # Nothing dirty — yield briefly to let new frames arrive
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            return

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
