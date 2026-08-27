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
from dataclasses import dataclass
from typing import Any

import aiohttp

from .entertainment import ChannelColor

_LOGGER = logging.getLogger(__name__)
EDGES = ("left", "top", "right", "bottom")


async def async_validate_jointspace(
    session: aiohttp.ClientSession, host: str, username: str, password: str,
    *, api_version: int, port: int, verify_ssl: bool,
) -> dict[str, int]:
    """Validate production JointSpace connectivity and return its topology."""
    source = PhilipsJointSpaceSource(
        session, host, username, password, {}, lambda _colors: None,
        api_version=api_version, port=port, verify_ssl=verify_ssl,
    )
    await source._async_topology()  # validation deliberately shares production request code
    return source.stats["topology"]


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
    payload: dict[str, Any], topology: dict[str, int], reversed_edges: set[str] = frozenset()
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
                rgb = tuple(max(0, min(255, int(color[key]))) for key in ("r", "g", "b"))
            except (KeyError, TypeError, ValueError):
                continue
            if index < 0 or index >= count:
                continue
            position = _zone_position(edge, index, count, edge in reversed_edges)
            points.append(AmbilightPoint(edge, index, position, rgb))
    return points


def map_points_to_channels(
    points: list[AmbilightPoint], channel_positions: dict[int, tuple[float, float, float]],
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
        red, green, blue = (round(sum(point.rgb[index] for point in selected) / len(selected)) for index in range(3))
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
    candidates = sorted((point for point in points if point.edge == edge), key=lambda point: point.position[1])
    if not candidates or segment not in {"top", "middle", "bottom"}:
        return []
    # Positions already reflect the configured edge orientation. Select the
    # nearest third centre, so 2, 3, or longer dynamic edge counts all work.
    target = {"bottom": -1.0, "middle": 0.0, "top": 1.0}[segment]
    return [min(candidates, key=lambda point: abs(point.position[1] - target))]


class PhilipsJointSpaceSource(AmbilightSource):
    """Poll authenticated HTTPS JointSpace measured Ambilight data without backlog."""
    def __init__(self, session: aiohttp.ClientSession, host: str, username: str, password: str,
                 channel_positions: dict[int, tuple[float, float, float]], frame_callback,
                 *, api_version: int = 6, port: int = 1926, fps: int = 10,
                 verify_ssl: bool = False, reversed_edges: set[str] = frozenset(), manual_mappings: dict[int, str] | None = None) -> None:
        self._session, self._host, self._username, self._password = session, host, username, password
        self._channel_positions, self._frame_callback = channel_positions, frame_callback
        self._api_version, self._port, self._fps, self._verify_ssl = api_version, port, max(1, fps), verify_ssl
        self._reversed_edges = reversed_edges
        self._auth = aiohttp.DigestAuth(username, password)
        self._manual_mappings = manual_mappings or {}
        self._mapping_logged = False
        self._inactivity_callback = None
        self._inactivity_timeout = 5.0
        self._output_active = False
        self._topology: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_frame = 0.0
        self._failed = self._frames = self._skipped = self._reconnects = 0
        self._backoff = 0.0
        self._latency_total = 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {"topology": self._topology, "target_fps": self._fps, "frames": self._frames,
                "failed_requests": self._failed, "skipped_polls": self._skipped,
                "reconnects": self._reconnects, "average_latency_ms": round(1000 * self._latency_total / self._frames, 1) if self._frames else 0}

    @property
    def last_frame_time(self) -> float: return self._last_frame

    async def async_start(self) -> None:
        if self._running: return
        await self._async_topology()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def async_stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None

    async def async_close(self) -> None: await self.async_stop()

    def set_inactivity_callback(self, callback, timeout: float) -> None:
        """Stop a direct output session when the TV stops yielding valid frames."""
        self._inactivity_callback, self._inactivity_timeout = callback, max(0.1, timeout)

    async def _async_topology(self) -> None:
        self._topology = parse_topology(await self._get("ambilight/topology"))

    async def _get(self, resource: str) -> dict[str, Any]:
        url = f"https://{self._host}:{self._port}/{self._api_version}/{resource}"
        async with self._session.get(url, auth=self._auth, ssl=self._verify_ssl, timeout=aiohttp.ClientTimeout(total=3)) as response:
            response.raise_for_status()
            data = await response.json()
            if not isinstance(data, dict): raise ValueError("JointSpace response is not an object")
            return data

    async def _poll_loop(self) -> None:
        interval, next_poll = 1 / self._fps, time.monotonic()
        while self._running:
            now = time.monotonic()
            if now < next_poll:
                await asyncio.sleep(next_poll - now)
            started = time.monotonic()
            next_poll = started + interval
            try:
                payload = await self._get("ambilight/measured")
                points = measured_points(payload, self._topology, self._reversed_edges)
                colors = map_points_to_channels(points, self._channel_positions, self._manual_mappings)
                if not colors: continue
                if not self._mapping_logged:
                    for channel_id, position in sorted(self._channel_positions.items()):
                        mapping = self._manual_mappings.get(channel_id, "auto")
                        labels = resolved_zone_labels(points, mapping)
                        _LOGGER.debug("Hue channel %d at %s -> %s -> %s", channel_id, position, mapping, labels or ["auto nearest"])
                    self._mapping_logged = True
                self._last_frame = time.monotonic(); self._frames += 1; self._latency_total += self._last_frame - started
                self._backoff = 0.0
                self._output_active = True
                self._frame_callback(colors)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                self._failed += 1
                self._reconnects += 1
                self._backoff = min(max(self._backoff * 2, 1.0), 30.0)
                if self._failed == 1 or self._failed % 30 == 0: _LOGGER.debug("JointSpace Ambilight poll failed", exc_info=True)
                await asyncio.sleep(self._backoff)
            if self._output_active and time.monotonic() - self._last_frame > self._inactivity_timeout:
                self._output_active = False
                if self._inactivity_callback is not None:
                    asyncio.create_task(self._inactivity_callback())
            if time.monotonic() > next_poll:
                self._skipped += 1
                next_poll = time.monotonic() + interval


def _zone_position(edge: str, index: int, count: int, reverse: bool) -> tuple[float, float, float]:
    fraction = (count - 1 - index if reverse else index) / max(count - 1, 1)
    coordinate = -1 + 2 * fraction
    if edge == "left": return (-1.0, coordinate, 0.0)
    if edge == "right": return (1.0, coordinate, 0.0)
    if edge == "top": return (coordinate, 1.0, 0.0)
    return (coordinate, -1.0, 0.0)


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
