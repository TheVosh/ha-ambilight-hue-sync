"""Time-based colour smoothing between input frames and an output backend.

Ambilight inputs arrive at their own cadence (JointSpace is polled at ~10 Hz)
and each frame is a hard step. Forwarded verbatim, every poll reads as an
instant colour jump on the lamps. Instead, the latest input frame becomes a
per-channel *target*, and a steady output loop eases the streamed colour
towards it with an exponential (first-order low-pass) filter.

The per-tick weight is derived from a time constant ``tau`` and the actual
elapsed time, ``alpha = 1 - exp(-dt / tau)``, so the fade looks the same at any
output rate or under timer jitter. Each component is eased in the frame's own
colour space (16-bit RGB, or CIE x/y + brightness), mirroring how the Hue
Entertainment stream carries it. A channel within ``SNAP_DELTA`` of its target
snaps to it, so a fade terminates and the loop can go idle.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from typing import Any

from .backends import EntertainmentOutputBackend
from .entertainment import ChannelColor

_LOGGER = logging.getLogger(__name__)

# Per-component distance (of 65535, i.e. below one 8-bit step) within which the
# eased colour snaps to its target: ends the geometric tail of the fade.
SNAP_DELTA = 256.0

Components = tuple[float, float, float]
SmoothState = dict[int, Components]


def smoothing_alpha(dt: float, tau: float) -> float:
    """Return the exponential-smoothing weight for ``dt`` seconds at time constant ``tau``.

    ``tau <= 0`` disables smoothing (weight 1, the target is taken verbatim);
    a non-positive ``dt`` moves nothing.
    """
    if tau <= 0:
        return 1.0
    if dt <= 0:
        return 0.0
    return 1.0 - math.exp(-dt / tau)


def ease_component(current: float, target: float, alpha: float) -> float:
    """Move one component ``alpha`` of the way towards ``target``, snapping when close."""
    delta = target - current
    if abs(delta) <= SNAP_DELTA:
        return target
    return current + alpha * delta


def ease_state(
    current: Mapping[int, Components], targets: Mapping[int, ChannelColor], alpha: float
) -> SmoothState:
    """Ease every target channel from its current colour; unseen channels snap.

    Channels absent from ``targets`` are dropped, so the state always mirrors the
    latest input frame's channel set.
    """
    eased: SmoothState = {}
    for channel_id, target in targets.items():
        goal = (float(target.r), float(target.g), float(target.b))
        previous = current.get(channel_id)
        if previous is None:
            eased[channel_id] = goal
            continue
        eased[channel_id] = (
            ease_component(previous[0], goal[0], alpha),
            ease_component(previous[1], goal[1], alpha),
            ease_component(previous[2], goal[2], alpha),
        )
    return eased


def is_settled(current: Mapping[int, Components], targets: Mapping[int, ChannelColor]) -> bool:
    """Whether every target channel has exactly reached its target colour."""
    return current.keys() == targets.keys() and all(
        current[channel_id] == (float(target.r), float(target.g), float(target.b))
        for channel_id, target in targets.items()
    )


def state_to_channels(state: Mapping[int, Components]) -> list[ChannelColor]:
    """Round the float smoothing state into 16-bit channel colours, ordered by channel."""
    return [
        ChannelColor(channel_id, *(max(0, min(65535, round(value))) for value in components))
        for channel_id, components in sorted(state.items())
    ]


def smoothed_output(
    backend: EntertainmentOutputBackend, transition_ms: float, fps: int
) -> EntertainmentOutputBackend:
    """Wrap ``backend`` with colour smoothing; a transition of 0 ms returns it unchanged."""
    if transition_ms <= 0:
        return backend
    return SmoothedOutputBackend(backend, transition=transition_ms / 1000, fps=fps)


class SmoothedOutputBackend(EntertainmentOutputBackend):
    """Decouple output from input: stream eased frames to ``inner`` at a steady rate.

    ``send_frame`` records the newest target. A background loop runs at ``fps``
    while a fade is in progress and goes idle once every channel has settled, so
    a paused or stopped input stops traffic (the Hue library keeps the stream
    alive by itself).
    """

    def __init__(
        self,
        inner: EntertainmentOutputBackend,
        *,
        transition: float,
        fps: int,
    ) -> None:
        self.inner = inner
        self._tau = max(0.0, transition)
        self._interval = 1 / max(1, fps)
        self._targets: dict[int, ChannelColor] = {}
        self._state: SmoothState = {}
        self._color_space: int | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return self.inner.available

    @property
    def connected(self) -> bool:
        return self.inner.connected

    @property
    def stats(self) -> dict[str, Any]:
        inner_stats: dict[str, Any] = getattr(
            self.inner, "stats", {"type": type(self.inner).__name__}
        )
        return {**inner_stats, "smoothing_ms": round(self._tau * 1000)}

    async def async_start(self) -> None:
        await self.inner.async_start()

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        if color_space != self._color_space:
            self._state = {}  # components are not comparable across colour spaces
            self._color_space = color_space
        self._targets = {channel.channel_id: channel for channel in channels}
        if self._task is not None and not self._task.done():
            return  # the running loop picks the new target up on its next tick
        # Idle: answer this input at once with one nominal step, then keep fading.
        self._emit(smoothing_alpha(self._interval, self._tau))
        if is_settled(self._state, self._targets):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # synchronous caller without a loop: no fade possible
            self._emit(1.0)
            return
        self._task = loop.create_task(self._run())

    def handle_light_command(self, light_id: int, body: dict[str, Any]) -> None:
        self.inner.handle_light_command(light_id, body)

    async def async_stop(self) -> None:
        await self._cancel()
        await self.inner.async_stop()

    async def async_close(self) -> None:
        await self._cancel()
        await self.inner.async_close()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._targets, self._state = {}, {}
        self._color_space = None

    async def _run(self) -> None:
        """Emit eased frames at the output rate until every channel has settled."""
        last = time.monotonic()
        try:
            while not is_settled(self._state, self._targets):
                await asyncio.sleep(self._interval)
                now = time.monotonic()
                # Clamp dt so a stalled event loop cannot collapse a fade into a jump.
                dt = min(now - last, 2 * self._interval)
                last = now
                self._emit(smoothing_alpha(dt, self._tau))
        except Exception:  # a faulty output must not kill the event loop task silently
            _LOGGER.exception("Colour smoothing loop stopped unexpectedly")

    def _emit(self, alpha: float) -> None:
        if not self._targets or self._color_space is None:
            return
        self._state = ease_state(self._state, self._targets, alpha)
        self.inner.send_frame(state_to_channels(self._state), self._color_space)
