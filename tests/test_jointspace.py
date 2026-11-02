"""Tests for Philips JointSpace Ambilight normalization and mapping."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.hue_entertainment.backends import EntertainmentOutputBackend
from custom_components.hue_entertainment.control import SyncController
from custom_components.hue_entertainment.entertainment import ChannelColor
from custom_components.hue_entertainment.jointspace import (
    DARK_LEVEL,
    DARK_RELEASE_TIMEOUT,
    GateAction,
    PhilipsJointSpaceSource,
    Sample,
    SignalGate,
    advance_gate,
    classify_points,
    map_points_to_channels,
    measured_points,
    parse_topology,
)


def test_dynamic_topology_and_edge_positions() -> None:
    topology = parse_topology({"left": 2, "top": 3, "right": 1, "bottom": 0})
    points = measured_points(
        {
            "layer1": {
                "left": {"0": {"r": 1, "g": 2, "b": 3}, "1": {"r": 4, "g": 5, "b": 6}},
                "top": {"0": {"r": 7, "g": 8, "b": 9}},
            }
        },
        topology,
    )
    assert topology == {"left": 2, "top": 3, "right": 1, "bottom": 0}
    assert points[0].position == (-1.0, -1.0, 0.0)
    assert points[1].position == (-1.0, 1.0, 0.0)


def test_reverse_edge_and_nearest_channel_mapping() -> None:
    topology = parse_topology({"left": 0, "top": 2, "right": 0, "bottom": 0})
    points = measured_points(
        {"layer1": {"top": {"0": {"r": 10, "g": 20, "b": 30}, "1": {"r": 40, "g": 50, "b": 60}}}},
        topology,
        {"top"},
    )
    colors = map_points_to_channels(points, {1: (-1, 1, 0), 2: (1, 1, 0)})
    assert [(color.channel_id, color.r, color.g, color.b) for color in colors] == [
        (1, 40 * 257, 50 * 257, 60 * 257),
        (2, 10 * 257, 20 * 257, 30 * 257),
    ]


def test_invalid_or_missing_measured_layer_is_ignored() -> None:
    topology = parse_topology({"left": 1})
    assert measured_points({}, topology) == []
    assert measured_points({"layer1": {"left": {"0": {"r": "bad"}}}}, topology) == []


@pytest.mark.asyncio
async def test_initial_topology_failure_retries_before_starting_poll_loop(monkeypatch) -> None:
    """A transient startup request must not leave JointSpace permanently idle."""
    source = PhilipsJointSpaceSource(
        None,
        "synthetic-tv",
        "synthetic-user",
        "synthetic-password",
        {1: (0.0, 0.0, 0.0)},
        lambda _colors: None,
    )
    attempts = 0
    polling = asyncio.Event()

    async def async_topology() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.TimeoutError

    async def poll_loop() -> None:
        polling.set()
        await asyncio.Future()

    monkeypatch.setattr(source, "_async_topology", async_topology)
    monkeypatch.setattr(source, "_poll_loop", poll_loop)

    await source.async_start()
    await asyncio.wait_for(polling.wait(), timeout=2)

    assert attempts == 2
    assert source.stats["failed_requests"] == 1
    assert source.stats["reconnects"] == 1
    await source.async_close()


# --- TV-off regression: no repeated Entertainment Area start/stop cycles -------------

LIT = {"layer1": {"left": {"0": {"r": 200, "g": 80, "b": 10}}}}
BLACK = {"layer1": {"left": {"0": {"r": 0, "g": 0, "b": 0}}}}
REQUEST_TIMEOUT = 3.0  # aiohttp ClientTimeout(total=3) in PhilipsJointSpaceSource._get

TvBehaviour = Callable[[float, int], "dict[str, Any] | None"]


class FakeClock:
    """Virtual monotonic clock; sleeping advances it and yields to the loop."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class CountingBackend(EntertainmentOutputBackend):
    """Records every activation/release of the (physical) Entertainment Area."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.streaming = False
        self.events: list[tuple[float, str]] = []
        self.frames: list[list[ChannelColor]] = []

    async def async_start(self) -> None:
        self.streaming = True
        self.events.append((self._clock.now, "start"))

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        self.frames.append(channels)

    async def async_stop(self) -> None:
        if self.streaming:
            self.events.append((self._clock.now, "stop"))
        self.streaming = False

    @property
    def connected(self) -> bool:
        return self.streaming

    def kinds(self) -> list[str]:
        return [kind for _time, kind in self.events]


async def _simulate(tv: TvBehaviour, until: float) -> CountingBackend:
    """Run the real poll loop + SyncController wired like ``__init__`` on a fake clock."""
    clock = FakeClock()
    backend = CountingBackend(clock)
    engine = SimpleNamespace(
        output_suppressed=False, status="idle", async_restore_lights=AsyncMock()
    )
    control = SyncController(
        backend,
        engine,  # type: ignore[arg-type]
        SimpleNamespace(async_delay_save=Mock()),  # type: ignore[arg-type]
        Mock(),
        enabled=True,
        intensity=1.0,
        track_engine_session=False,
    )
    tasks: set[asyncio.Task[bool]] = set()

    def frame(colors: list[ChannelColor]) -> None:
        # Mirrors __init__._jointspace_frame: every forwarded frame (re)starts output.
        task = asyncio.create_task(control.async_start())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        control.send_frame(colors, 0)

    source = PhilipsJointSpaceSource(
        None,  # type: ignore[arg-type]
        "synthetic-tv",
        "synthetic-user",
        "synthetic-password",
        {1: (-1.0, 0.0, 0.0)},
        frame,
        clock=clock,
        sleep=clock.sleep,
    )
    source.set_inactivity_callback(control.async_stop, 5.0)
    attempts = 0

    async def fake_get(_resource: str) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        payload = tv(clock.now, attempts)
        if payload is None:
            await clock.sleep(REQUEST_TIMEOUT)
            raise asyncio.TimeoutError
        await clock.sleep(0.02)
        return payload

    async def topology() -> None:
        source._topology = {"left": 1, "top": 0, "right": 0, "bottom": 0}

    source._get = fake_get  # type: ignore[method-assign]
    source._async_topology = topology  # type: ignore[method-assign]
    await source.async_start()
    while clock.now < until:
        await asyncio.sleep(0)
    await source.async_close()
    return backend


def _on_until(off_at: float, off: TvBehaviour, on_again: float = float("inf")) -> TvBehaviour:
    def tv(now: float, attempt: int) -> dict[str, Any] | None:
        return LIT if now < off_at or now >= on_again else off(now, attempt)

    return tv


def _sporadic_black(_now: float, attempt: int) -> dict[str, Any] | None:
    """Network standby: mostly timeouts, every 7th request answered with black."""
    return BLACK if attempt % 7 == 0 else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "off"),
    [
        ("unreachable", lambda _now, _attempt: None),
        ("standby_black", lambda _now, _attempt: BLACK),
        ("sporadic_black", _sporadic_black),
        # Stray stale lit answers amid timeouts (never two in a row).
        ("sporadic_stale_lit", lambda _now, attempt: LIT if attempt % 5 == 0 else None),
        ("empty_measurement", lambda _now, _attempt: {"layer1": {}}),
    ],
)
async def test_tv_off_releases_area_once_and_never_restarts(name: str, off: TvBehaviour) -> None:
    backend = await _simulate(_on_until(20.0, off), until=900.0)

    assert backend.kinds() == ["start", "stop"], (name, backend.events)
    start_at, stop_at = (time for time, _kind in backend.events)
    assert start_at < 1.0
    assert 20.0 < stop_at <= 20.0 + DARK_RELEASE_TIMEOUT + REQUEST_TIMEOUT + 1.0


@pytest.mark.asyncio
async def test_output_resumes_when_tv_turns_back_on() -> None:
    backend = await _simulate(_on_until(20.0, _sporadic_black, on_again=400.0), until=600.0)

    assert backend.kinds() == ["start", "stop", "start"], backend.events
    restart_at = backend.events[-1][0]
    # Poll backoff is capped at 30 s, so the TV is noticed again within ~one cap.
    assert 400.0 <= restart_at <= 400.0 + 30.0 + REQUEST_TIMEOUT + 1.0
    assert backend.streaming


@pytest.mark.asyncio
async def test_dark_scene_while_streaming_is_forwarded_without_release() -> None:
    def tv(now: float, _attempt: int) -> dict[str, Any] | None:
        return BLACK if 10.0 <= now < 10.0 + DARK_RELEASE_TIMEOUT - 2.0 else LIT

    backend = await _simulate(tv, until=60.0)

    assert backend.kinds() == ["start"]
    assert any(all(max(c.r, c.g, c.b) == 0 for c in frame) for frame in backend.frames)


def test_gate_requires_consecutive_lit_frames_and_releases_once() -> None:
    gate = SignalGate()
    gate, action = advance_gate(gate, Sample.LIT, 0.0, timeout=5.0, arm_frames=2)
    assert action is GateAction.NONE
    gate, action = advance_gate(gate, Sample.MISSING, 0.1, timeout=5.0, arm_frames=2)
    assert (action, gate.lit_streak) == (GateAction.NONE, 0)
    gate, _ = advance_gate(gate, Sample.LIT, 0.2, timeout=5.0, arm_frames=2)
    gate, action = advance_gate(gate, Sample.LIT, 0.3, timeout=5.0, arm_frames=2)
    assert action is GateAction.FORWARD and gate.active
    gate, action = advance_gate(gate, Sample.MISSING, 5.0, timeout=5.0)
    assert action is GateAction.NONE
    gate, action = advance_gate(gate, Sample.MISSING, 5.4, timeout=5.0)
    assert action is GateAction.RELEASE and not gate.active
    gate, action = advance_gate(gate, Sample.DARK, 6.0, timeout=5.0)
    assert action is GateAction.NONE and not gate.active


def test_classify_points() -> None:
    topology = parse_topology({"left": 2})
    dark = measured_points({"layer1": {"left": {"0": {"r": 0, "g": 1, "b": DARK_LEVEL}}}}, topology)
    assert classify_points(dark) is Sample.DARK
    lit = measured_points(
        {"layer1": {"left": {"1": {"r": DARK_LEVEL + 1, "g": 0, "b": 0}}}}, topology
    )
    assert classify_points(lit) is Sample.LIT
    assert classify_points([]) is Sample.MISSING
