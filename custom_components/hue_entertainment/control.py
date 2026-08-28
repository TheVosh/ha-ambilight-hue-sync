"""Persistent runtime control for Ambilight synchronization."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.helpers.storage import Store

from .backends import EntertainmentOutputBackend
from .const import COLOR_SPACE_XY
from .entertainment import ChannelColor, EntertainmentEngine

DEFAULT_INTENSITY = 1.0


class SyncController:
    """Single source of truth for sync power, intensity, and output state."""

    def __init__(
        self,
        backend: EntertainmentOutputBackend,
        engine: EntertainmentEngine,
        store: Store[dict[str, Any]],
        notify: Callable[[], None],
        *,
        enabled: bool,
        intensity: float,
        track_engine_session: bool,
    ) -> None:
        self._backend = backend
        self._engine = engine
        self._store = store
        self._notify = notify
        self._enabled = enabled
        self._intensity = _clamp_intensity(intensity)
        self._track_engine_session = track_engine_session
        self._active = False
        self._starting = False
        self._last_error: str | None = None
        self._start_lock = asyncio.Lock()
        self._stop_session: Callable[[], Awaitable[None]] | None = None

    @classmethod
    async def async_load(
        cls,
        backend: EntertainmentOutputBackend,
        engine: EntertainmentEngine,
        store: Store[dict[str, Any]],
        notify: Callable[[], None],
        *,
        track_engine_session: bool,
    ) -> SyncController:
        """Restore control state before input transports can produce frames."""
        saved = await store.async_load() or {}
        return cls(
            backend,
            engine,
            store,
            notify,
            enabled=bool(saved.get("enabled", True)),
            intensity=float(saved.get("intensity", DEFAULT_INTENSITY)),
            track_engine_session=track_engine_session,
        )

    @property
    def available(self) -> bool:
        """Whether the configured output is ready for user control."""
        return self._backend.available

    @property
    def enabled(self) -> bool:
        """Whether automatic synchronization is allowed to start."""
        return self._enabled

    @property
    def intensity(self) -> float:
        """Global output intensity as a normalized 0..1 value."""
        return self._intensity

    @property
    def connected(self) -> bool:
        """Whether the selected output currently has an active session."""
        return self._active and self._backend.connected

    @property
    def status(self) -> str:
        """Return a compact status derived from actual runtime state."""
        if not self._enabled:
            return "disabled"
        if self._starting:
            return "connecting"
        if self._last_error is not None or (self._active and not self._backend.connected):
            return "error"
        if self._engine.status in {"paused", "releasing"}:
            return self._engine.status
        if self.connected:
            return "streaming"
        if self._engine.status == "classic":
            return "classic"
        return "idle"

    @property
    def stats(self) -> dict[str, Any]:
        """Non-sensitive diagnostics for the control surface."""
        return {
            "enabled": self._enabled,
            "intensity": self._intensity,
            "connected": self.connected,
            "status": self.status,
            "last_error": self._last_error,
        }

    def set_stop_session_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Attach the integration-level teardown after all transports exist."""
        self._stop_session = callback

    async def async_set_enabled(self, enabled: bool) -> None:
        """Enable automatic starts or disable and tear down the current session."""
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self._last_error = None
        self._schedule_save()
        self._notify()
        if not enabled:
            if self._stop_session is not None:
                await self._stop_session()
            else:
                await self.async_stop()

    async def async_set_intensity(self, intensity: float) -> None:
        """Set global intensity without restarting an active session."""
        resolved = _clamp_intensity(intensity)
        if self._intensity == resolved:
            return
        self._intensity = resolved
        self._schedule_save()
        self._notify()

    async def async_start(self) -> bool:
        """Start the output once, unless synchronization is disabled."""
        if not self._enabled or not self.available:
            return False
        if self._active and self._backend.connected:
            return True
        async with self._start_lock:
            if not self._enabled:
                return False
            if self._active and self._backend.connected:
                return True
            self._active = False
            self._starting = True
            self._last_error = None
            self._notify()
            try:
                await self._backend.async_start()
                if not self._enabled:
                    await self._backend.async_stop()
                    return False
                if self._track_engine_session:
                    await self._engine.async_snapshot_lights()
                self._active = True
                return True
            except Exception as err:
                self._last_error = type(err).__name__
                raise
            finally:
                self._starting = False
                self._notify()

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        """Gate and scale a normalized frame before it reaches the backend."""
        if not self._enabled or not self._active or self._engine.output_suppressed:
            return
        self._backend.send_frame(
            _scale_channels(channels, color_space, self._intensity), color_space
        )

    def handle_light_command(self, light_id: int, body: dict[str, Any]) -> None:
        """Gate and scale a classic Hue REST light command."""
        if not self._enabled or self._engine.output_suppressed:
            return
        adjusted = dict(body)
        if "bri" in adjusted:
            try:
                adjusted["bri"] = round(float(adjusted["bri"]) * self._intensity)
            except (TypeError, ValueError):
                pass
        self._backend.handle_light_command(light_id, adjusted)

    async def async_stop(self) -> None:
        """Stop the output and resolve any engine-side session state."""
        async with self._start_lock:
            was_active = self._active or self._starting
            self._active = False
            self._starting = False
            if was_active:
                self._notify()
            await self._backend.async_stop()
            await self._engine.async_restore_lights()

    async def async_close(self) -> None:
        """Persist current state and release output resources on unload."""
        await self.async_stop()
        await self._store.async_save(self._saved_state())
        await self._backend.async_close()

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._saved_state, 1.0)

    def _saved_state(self) -> dict[str, Any]:
        return {"enabled": self._enabled, "intensity": self._intensity}


def _clamp_intensity(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scale_channels(
    channels: list[ChannelColor], color_space: int, intensity: float
) -> list[ChannelColor]:
    """Scale luminance without mutating input channels or shifting hue."""
    if intensity >= 1.0:
        return channels
    if color_space == COLOR_SPACE_XY:
        return [
            ChannelColor(channel.channel_id, channel.r, channel.g, round(channel.b * intensity))
            for channel in channels
        ]
    return [
        ChannelColor(
            channel.channel_id,
            round(channel.r * intensity),
            round(channel.g * intensity),
            round(channel.b * intensity),
        )
        for channel in channels
    ]
