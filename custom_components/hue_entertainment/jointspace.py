"""Async Philips JointSpace Ambilight input source.

JointSpace responses are normalized into virtual Hue channel colours.  The
source has no dependency on the emulated bridge or on an output transport.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

import aiohttp

from .entertainment import ChannelColor

_LOGGER = logging.getLogger(__name__)
EDGES = ("left", "top", "right", "bottom")

# A zone whose 8-bit channels are all at or below this level carries no light.
# A switched-off / standby TV reports such frames, so they never (re)start output.
DARK_LEVEL = 4
# Consecutive lit polls required before the output is (re)started. A single
# stray answer from a TV in network standby must not activate the Hue area.
ARM_FRAMES = 3
# While streaming, a fully dark picture is still forwarded (dark scenes), but if
# nothing lit arrives for this long the TV is treated as off and output released.
DARK_RELEASE_TIMEOUT = 15.0


class Sample(Enum):
    """Classification of one JointSpace poll."""

    LIT = "lit"  # usable colours with at least one zone carrying light
    DARK = "dark"  # usable colours, but every zone is black
    MISSING = "missing"  # failed request, invalid or empty measurement


class GateAction(Enum):
    """What the poll loop must do after a gate transition."""

    NONE = "none"
    FORWARD = "forward"  # hand the frame to the output (starts it if idle)
    RELEASE = "release"  # stop the output once; stay idle until re-armed


@dataclass(frozen=True)
class SignalGate:
    """Immutable output gate state: idle until armed by lit frames, then active."""

    active: bool = False
    lit_streak: int = 0
    last_frame: float = 0.0
    last_lit: float = 0.0


def classify_points(points: list[AmbilightPoint]) -> Sample:
    """Classify parsed measured zones as lit, dark, or missing."""
    if not points:
        return Sample.MISSING
    if any(max(point.rgb) > DARK_LEVEL for point in points):
        return Sample.LIT
    return Sample.DARK


def advance_gate(
    gate: SignalGate,
    sample: Sample,
    now: float,
    *,
    timeout: float,
    dark_timeout: float = DARK_RELEASE_TIMEOUT,
    arm_frames: int = ARM_FRAMES,
) -> tuple[SignalGate, GateAction]:
    """Pure transition function of the TV-signal output gate.

    Idle: only ``arm_frames`` consecutive lit polls activate output; dark or
    missing polls reset the streak, so a TV that is off/unreachable never
    restarts the stream. Active: every usable frame (lit or dark) is forwarded;
    output is released exactly once when no usable frame arrived for
    ``timeout`` or no lit frame for ``max(timeout, dark_timeout)``.
    """
    if not gate.active:
        if sample is not Sample.LIT:
            return (replace(gate, lit_streak=0) if gate.lit_streak else gate), GateAction.NONE
        streak = gate.lit_streak + 1
        if streak < max(1, arm_frames):
            return replace(gate, lit_streak=streak), GateAction.NONE
        return SignalGate(True, 0, now, now), GateAction.FORWARD
    if sample is Sample.LIT:
        return replace(gate, last_frame=now, last_lit=now), GateAction.FORWARD
    if sample is Sample.DARK:
        if now - gate.last_lit > max(timeout, dark_timeout):
            return SignalGate(), GateAction.RELEASE
        return replace(gate, last_frame=now), GateAction.FORWARD
    if now - gate.last_frame > timeout:
        return SignalGate(), GateAction.RELEASE
    return gate, GateAction.NONE


async def async_validate_jointspace(
    session: aiohttp.ClientSession,
    host: str,
    username: str,
    password: str,
    *,
    api_version: int,
    port: int,
    verify_ssl: bool,
) -> dict[str, int]:
    """Validate production JointSpace connectivity and return its topology."""
    source = PhilipsJointSpaceSource(
        session,
        host,
        username,
        password,
        {},
        lambda _colors: None,
        api_version=api_version,
        port=port,
        verify_ssl=verify_ssl,
    )
    try:
        await source._async_topology()  # validation deliberately shares production request code
        return source.stats["topology"]
    finally:
        await source.async_close()


@dataclass(frozen=True)
class AmbilightPoint:
    """One measured TV-edge zone, positioned on the TV plane."""

    edge: str
    index: int
    position: tuple[float, float, float]
    rgb: tuple[int, int, int]


class AmbilightSource(ABC):
    """An asynchronous source of normalized Ambilight frames."""

    @abstractmethod
    async def async_start(self) -> None: ...
    @abstractmethod
    async def async_stop(self) -> None: ...
    @abstractmethod
    async def async_close(self) -> None: ...


def parse_topology(payload: dict[str, Any]) -> dict[str, int]:
    """Validate the dynamic edge counts returned by JointSpace topology."""
    result: dict[str, int] = {}
    for edge in EDGES:
        value = payload.get(edge, 0)
        try:
            result[edge] = max(0, int(value))
        except (TypeError, ValueError):
            result[edge] = 0
    if not any(result.values()):
        raise ValueError("JointSpace topology contains no Ambilight zones")
    return result


def measured_points(
    payload: dict[str, Any], topology: dict[str, int], reversed_edges: frozenset[str] = frozenset()
) -> list[AmbilightPoint]:
    """Parse `layer1` measured RGB, ignoring missing/invalid/all-black zones."""
    layer = payload.get("layer1")
    if not isinstance(layer, dict):
        return []
    points: list[AmbilightPoint] = []
    for edge in EDGES:
        zones = layer.get(edge)
        count = topology.get(edge, 0)
        if not isinstance(zones, dict) or count <= 0:
            continue
        for index_text, color in zones.items():
            try:
                index = int(index_text)
                red, green, blue = (max(0, min(255, int(color[key]))) for key in ("r", "g", "b"))
                rgb: tuple[int, int, int] = (red, green, blue)
            except (KeyError, TypeError, ValueError):
                continue
            if index < 0 or index >= count:
                continue
            position = _zone_position(edge, index, count, edge in reversed_edges)
            points.append(AmbilightPoint(edge, index, position, rgb))
    return points


def map_points_to_channels(
    points: list[AmbilightPoint],
    channel_positions: dict[int, tuple[float, float, float]],
    manual_mappings: dict[int, str] | None = None,
) -> list[ChannelColor]:
    """Map every virtual Hue channel to its nearest measured TV zone deterministically."""
    if not points:
        return []
    colors: list[ChannelColor] = []
    for channel_id, position in sorted(channel_positions.items()):
        mapping = (manual_mappings or {}).get(channel_id, "auto")
        selected = _points_for_location(points, mapping)
        if not selected:
            selected = [min(points, key=lambda point: _distance(point.position, position))]
        red, green, blue = (
            round(sum(point.rgb[index] for point in selected) / len(selected)) for index in range(3)
        )
        colors.append(ChannelColor(channel_id, red * 257, green * 257, blue * 257))
    return colors


def resolved_zone_labels(points: list[AmbilightPoint], mapping: str) -> list[str]:
    """Labels suitable for a single non-sensitive DEBUG mapping log entry."""
    return [f"{point.edge}[{point.index}]" for point in _points_for_location(points, mapping)]


def _points_for_location(points: list[AmbilightPoint], mapping: str) -> list[AmbilightPoint]:
    if mapping in {"auto", ""}:
        return []
    if mapping in {"top", "bottom"}:
        return [point for point in points if point.edge == mapping]
    try:
        edge, segment = mapping.split("_", 1)
    except ValueError:
        return []
    candidates = sorted(
        (point for point in points if point.edge == edge), key=lambda point: point.position[1]
    )
    if not candidates or segment not in {"top", "middle", "bottom"}:
        return []
    # Positions already reflect the configured edge orientation. Select the
    # nearest third centre, so 2, 3, or longer dynamic edge counts all work.
    target = {"bottom": -1.0, "middle": 0.0, "top": 1.0}[segment]
    return [min(candidates, key=lambda point: abs(point.position[1] - target))]


class PhilipsJointSpaceSource(AmbilightSource):
    """Poll authenticated HTTPS JointSpace measured Ambilight data without backlog."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        channel_positions: dict[int, tuple[float, float, float]],
        frame_callback,
        *,
        api_version: int = 6,
        port: int = 1926,
        fps: int = 10,
        verify_ssl: bool = False,
        reversed_edges: frozenset[str] = frozenset(),
        manual_mappings: dict[int, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Digest middleware must be attached when a ClientSession is created.
        # Keep this private session scoped to this TV; the HA shared session is
        # deliberately not modified and cannot leak Digest credentials elsewhere.
        self._session: aiohttp.ClientSession | None = None
        self._hass_session, self._host, self._username, self._password = (
            session,
            host,
            username,
            password,
        )
        self._channel_positions, self._frame_callback = channel_positions, frame_callback
        self._api_version, self._port, self._fps, self._verify_ssl = (
            api_version,
            port,
            max(1, fps),
            verify_ssl,
        )
        self._reversed_edges = reversed_edges
        self._manual_mappings = manual_mappings or {}
        self._mapping_logged = False
        self._inactivity_callback: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._inactivity_timeout = 5.0
        self._clock, self._sleep = clock, sleep
        self._gate = SignalGate()
        self._callback_tasks: set[asyncio.Task[None]] = set()
        self._topology: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_frame = 0.0
        self._failed = self._frames = self._skipped = self._reconnects = 0
        self._backoff = 0.0
        self._latency_total = 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "topology": self._topology,
            "target_fps": self._fps,
            "frames": self._frames,
            "failed_requests": self._failed,
            "skipped_polls": self._skipped,
            "reconnects": self._reconnects,
            "average_latency_ms": round(1000 * self._latency_total / self._frames, 1)
            if self._frames
            else 0,
        }

    @property
    def last_frame_time(self) -> float:
        return self._last_frame

    async def async_start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Initialize topology with backoff, then keep the single polling loop alive."""
        while self._running:
            try:
                await self._async_topology()
                self._backoff = 0.0
                await self._poll_loop()
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                self._failed += 1
                self._reconnects += 1
                self._backoff = min(max(self._backoff * 2, 1.0), 30.0)
                if self._failed == 1 or self._failed % 30 == 0:
                    _LOGGER.debug("JointSpace topology initialization failed", exc_info=True)
                await self._sleep(self._backoff)
            except Exception:
                self._running = False
                _LOGGER.exception("JointSpace topology initialization stopped unexpectedly")
                return

    async def async_stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def async_close(self) -> None:
        await self.async_stop()
        if self._session is not None:
            await self._session.close()
            self._session = None

    def set_inactivity_callback(
        self, callback: Callable[[], Coroutine[Any, Any, None]], timeout: float
    ) -> None:
        """Stop a direct output session when the TV stops yielding valid frames."""
        self._inactivity_callback, self._inactivity_timeout = callback, max(0.1, timeout)

    async def _async_topology(self) -> None:
        self._topology = parse_topology(await self._get("ambilight/topology"))

    async def _get(self, resource: str) -> dict[str, Any]:
        if self._session is None:
            middleware = aiohttp.DigestAuthMiddleware(self._username, self._password)
            self._session = aiohttp.ClientSession(middlewares=(middleware,))
        url = f"https://{self._host}:{self._port}/{self._api_version}/{resource}"
        async with self._session.get(
            url, ssl=self._verify_ssl, timeout=aiohttp.ClientTimeout(total=3)
        ) as response:
            response.raise_for_status()
            data = await response.json()
            if not isinstance(data, dict):
                raise ValueError("JointSpace response is not an object")
            return data

    async def _poll_loop(self) -> None:
        interval, next_poll = 1 / self._fps, self._clock()
        while self._running:
            now = self._clock()
            if now < next_poll:
                await self._sleep(next_poll - now)
            started = self._clock()
            next_poll = started + interval
            sample, colors, failed = Sample.MISSING, [], False
            try:
                payload = await self._get("ambilight/measured")
                points = measured_points(payload, self._topology, self._reversed_edges)
                colors = map_points_to_channels(
                    points, self._channel_positions, self._manual_mappings
                )
                sample = classify_points(points) if colors else Sample.MISSING
                if colors and not self._mapping_logged:
                    self._log_mapping(points)
                self._backoff = 0.0
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                failed = True
                self._failed += 1
                self._reconnects += 1
                self._backoff = min(max(self._backoff * 2, 1.0), 30.0)
                if self._failed == 1 or self._failed % 30 == 0:
                    _LOGGER.debug("JointSpace Ambilight poll failed", exc_info=True)
            self._apply_sample(sample, colors, started)
            if failed:
                await self._sleep(self._backoff)
            if self._clock() > next_poll:
                self._skipped += 1
                next_poll = self._clock() + interval

    def _apply_sample(self, sample: Sample, colors: list[ChannelColor], started: float) -> None:
        """Feed one poll result through the output gate and act on its decision."""
        now = self._clock()
        self._gate, action = advance_gate(self._gate, sample, now, timeout=self._inactivity_timeout)
        if action is GateAction.FORWARD:
            self._last_frame = now
            self._frames += 1
            self._latency_total += now - started
            self._frame_callback(colors)
        elif action is GateAction.RELEASE:
            _LOGGER.debug("Ambilight signal lost (%s); releasing output", sample.value)
            if self._inactivity_callback is not None:
                task = asyncio.create_task(self._inactivity_callback())
                self._callback_tasks.add(task)
                task.add_done_callback(self._callback_tasks.discard)

    def _log_mapping(self, points: list[AmbilightPoint]) -> None:
        for channel_id, position in sorted(self._channel_positions.items()):
            mapping = self._manual_mappings.get(channel_id, "auto")
            labels = resolved_zone_labels(points, mapping)
            _LOGGER.debug(
                "Hue channel %d at %s -> %s -> %s",
                channel_id,
                position,
                mapping,
                labels or ["auto nearest"],
            )
        self._mapping_logged = True


def _zone_position(edge: str, index: int, count: int, reverse: bool) -> tuple[float, float, float]:
    fraction = (count - 1 - index if reverse else index) / max(count - 1, 1)
    coordinate = -1 + 2 * fraction
    if edge == "left":
        return (-1.0, coordinate, 0.0)
    if edge == "right":
        return (1.0, coordinate, 0.0)
    if edge == "top":
        return (coordinate, 1.0, 0.0)
    return (coordinate, -1.0, 0.0)


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
