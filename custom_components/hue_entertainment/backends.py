"""Output backends for normalized Ambilight channel frames.

Legacy HueStream and JointSpace inputs share these destinations, keeping input
transport independent from the physical output transport.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging
import time
from abc import ABC, abstractmethod
from importlib import import_module
from typing import Any

from .const import COLOR_SPACE_XY, DEFAULT_STREAM_FPS
from .entertainment import ChannelColor, EntertainmentEngine

_LOGGER = logging.getLogger(__name__)


def _hue_symbol(name: str) -> Any:
    """Resolve optional PyPI symbols despite the integration package name collision."""
    return getattr(import_module("hue_entertainment"), name)


class EntertainmentOutputBackend(ABC):
    """A destination for normalized HueStream channel colours."""

    @abstractmethod
    async def async_start(self) -> None: ...

    @abstractmethod
    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None: ...

    @abstractmethod
    async def async_stop(self) -> None: ...

    async def async_close(self) -> None:
        """Release resources. Backends without resources need no override."""


class HomeAssistantLightBackend(EntertainmentOutputBackend):
    """The legacy HA/ZHA output path, including state restore and coalescing."""

    def __init__(self, engine: EntertainmentEngine) -> None:
        self.engine = engine

    async def async_start(self) -> None:
        await self.engine.async_snapshot_lights()

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        self.engine.handle_channels(channels, color_space)

    async def async_stop(self) -> None:
        await self.engine.async_restore_lights()

    def handle_light_command(self, light_id: int, body: dict[str, Any]) -> None:
        """Keep the non-streaming Hue v1 compatibility path on HA lights."""
        self.engine.handle_light_command(light_id, body)


class DisabledOutputBackend(EntertainmentOutputBackend):
    """Safe placeholder while a staged physical Hue setup is incomplete."""

    async def async_start(self) -> None:
        return

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        return

    async def async_stop(self) -> None:
        return

    @property
    def stats(self) -> dict[str, Any]:
        return {"configured": False, "type": "incomplete_hue_output"}


class HueEntertainmentBackend(EntertainmentOutputBackend):
    """Native DTLS Hue Entertainment output to a physical Hue Bridge.

    ``EntertainmentSession.send`` is intentionally non-blocking. It delegates
    the socket work to the library's sender thread, so this backend never turns
    input frames into Home Assistant ``light.turn_on`` service calls.
    """

    def __init__(
        self,
        host: str,
        app_key: str,
        client_key: str,
        area_id: str,
        channel_map: dict[int, int],
        *,
        fps_cap: int = DEFAULT_STREAM_FPS,
        brightness_multiplier: float = 1.0,
        saturation_multiplier: float = 1.0,
    ) -> None:
        self._host = host
        self._app_key = app_key
        self._client_key = client_key
        self._area_id = area_id
        self.channel_map = channel_map
        self._fps_cap = max(1, fps_cap)
        self._brightness_multiplier = max(0.0, brightness_multiplier)
        self._saturation_multiplier = max(0.0, saturation_multiplier)
        self._session: Any = None
        self._last_send = 0.0
        self._frames_sent = 0
        self._reconnects = 0
        self._start_lock = asyncio.Lock()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "host": self._host,
            "area_id": self._area_id,
            "channel_mapping": self.channel_map,
            "frames_sent": self._frames_sent,
            "reconnects": self._reconnects,
            "streaming": bool(self._session and self._session.is_streaming),
        }

    async def async_start(self) -> None:
        async with self._start_lock:
            if self._session is not None and self._session.is_streaming:
                return
            # Imported here so HA can install the manifest requirement before
            # importing the integration and unit tests can exercise old code alone.
            EntertainmentSession = _hue_symbol("EntertainmentSession")

            if self._session is None:
                self._session = EntertainmentSession(
                    self._host, self._app_key, self._client_key, idle_timeout=0
                )
            try:
                await self._session.start(self._area_id)
            except Exception as err:  # bridge may be offline; next TV start retries
                self._reconnects += 1
                _LOGGER.warning("Unable to start physical Hue Entertainment stream: %s", err)
                raise
            _LOGGER.debug("Physical Hue Entertainment session started for area %s", self._area_id)

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        if self._session is None or not self._session.is_streaming:
            return
        now = time.monotonic()
        if now - self._last_send < 1 / self._fps_cap:
            return
        self._last_send = now
        LightColorCommand = _hue_symbol("LightColorCommand")

        commands = []
        for channel in channels:
            physical_channel = self.channel_map.get(channel.channel_id)
            if physical_channel is None:
                continue
            red, green, blue = self._adjust(*_as_rgb(channel, color_space))
            commands.append(
                LightColorCommand(channel_id=physical_channel, red=red, green=green, blue=blue)
            )
        if not commands:
            return
        try:
            self._session.send(commands)
            self._frames_sent += 1
        except Exception:  # recover on the next explicit TV stream start; never block input
            _LOGGER.debug("Hue Entertainment send failed; session will be recreated", exc_info=True)

    async def async_stop(self) -> None:
        if self._session is not None:
            await self._session.stop()
            _LOGGER.debug("Physical Hue Entertainment session stopped")

    async def async_close(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _adjust(self, red: int, green: int, blue: int) -> tuple[int, int, int]:
        hue, saturation, value = colorsys.rgb_to_hsv(red / 65535, green / 65535, blue / 65535)
        adjusted_red, adjusted_green, adjusted_blue = colorsys.hsv_to_rgb(
            hue,
            min(1.0, saturation * self._saturation_multiplier),
            min(1.0, value * self._brightness_multiplier),
        )
        return (
            round(adjusted_red * 65535),
            round(adjusted_green * 65535),
            round(adjusted_blue * 65535),
        )


def _as_rgb(channel: ChannelColor, color_space: int) -> tuple[int, int, int]:
    """Convert source RGB or CIE xy + brightness to 16-bit sRGB."""
    if color_space != COLOR_SPACE_XY:
        red, green, blue = channel.r, channel.g, channel.b
    else:
        x, y, bri = channel.r / 65535, channel.g / 65535, channel.b / 65535
        if y <= 0:
            return (0, 0, 0)
        # CIE xyY -> linear RGB (D65), then gamma encode.
        y_lum = bri
        x_val, z_val = x * y_lum / y, (1 - x - y) * y_lum / y
        linear = (
            3.2406 * x_val - 1.5372 * y_lum - 0.4986 * z_val,
            -0.9689 * x_val + 1.8758 * y_lum + 0.0415 * z_val,
            0.0557 * x_val - 0.2040 * y_lum + 1.0570 * z_val,
        )
        rgb = [max(0.0, value) for value in linear]
        peak = max(rgb, default=0.0)
        if peak > 1:
            rgb = [value / peak for value in rgb]
        red, green, blue = (round(_gamma(value) * 65535) for value in rgb)
    return (red, green, blue)


def _gamma(value: float) -> float:
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
