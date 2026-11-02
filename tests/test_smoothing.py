"""Tests for time-based colour smoothing on the output path."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.hue_entertainment.backends import EntertainmentOutputBackend
from custom_components.hue_entertainment.const import COLOR_SPACE_RGB, COLOR_SPACE_XY
from custom_components.hue_entertainment.control import SyncController
from custom_components.hue_entertainment.entertainment import ChannelColor
from custom_components.hue_entertainment.smoothing import (
    SNAP_DELTA,
    SmoothedOutputBackend,
    ease_component,
    ease_state,
    is_settled,
    smoothed_output,
    smoothing_alpha,
    state_to_channels,
)

BLACK = ChannelColor(1, 0, 0, 0)
WHITE = ChannelColor(1, 65535, 65535, 65535)


class RecordingBackend(EntertainmentOutputBackend):
    """Output that records every frame it is asked to send."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = 0
        self.closed = 0
        self.frames: list[tuple[list[ChannelColor], int]] = []

    async def async_start(self) -> None:
        self.started = True

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        self.frames.append((channels, color_space))

    async def async_stop(self) -> None:
        self.started = False
        self.stopped += 1

    async def async_close(self) -> None:
        self.closed += 1

    @property
    def connected(self) -> bool:
        return self.started

    @property
    def stats(self) -> dict[str, object]:
        return {"type": "recording"}


def _reds(frames: list[tuple[list[ChannelColor], int]]) -> list[int]:
    return [channels[0].r for channels, _space in frames]


async def _wait_idle(backend: SmoothedOutputBackend, timeout: float = 2.0) -> None:
    task = backend._task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)


# --- pure math -----------------------------------------------------------------


def test_alpha_disabled_and_degenerate_inputs() -> None:
    assert smoothing_alpha(0.02, 0.0) == 1.0
    assert smoothing_alpha(0.02, -1.0) == 1.0
    assert smoothing_alpha(0.0, 0.15) == 0.0
    assert smoothing_alpha(-0.01, 0.15) == 0.0


def test_alpha_matches_exponential_time_constant() -> None:
    # After exactly one time constant, 1 - 1/e of the gap has been covered.
    assert smoothing_alpha(0.15, 0.15) == pytest.approx(1 - 1 / math.e)
    assert 0 < smoothing_alpha(0.02, 0.15) < smoothing_alpha(0.04, 0.15) < 1


def test_alpha_is_frame_rate_independent() -> None:
    tau = 0.15
    # Remaining fraction after 200 ms is identical whether stepped at 25, 50 or 100 Hz.
    remaining = [(1 - smoothing_alpha(1 / fps, tau)) ** round(fps * 0.2) for fps in (25, 50, 100)]
    assert remaining == pytest.approx([math.exp(-0.2 / tau)] * 3)


def test_ease_component_moves_partially_without_overshoot_and_snaps() -> None:
    assert ease_component(0.0, 10000.0, 0.25) == pytest.approx(2500.0)
    assert ease_component(10000.0, 0.0, 0.25) == pytest.approx(7500.0)
    assert ease_component(0.0, 10000.0, 1.0) == 10000.0
    assert ease_component(1000.0, 1000.0 + SNAP_DELTA, 0.01) == 1000.0 + SNAP_DELTA
    assert ease_component(1000.0, 1000.0 - SNAP_DELTA, 0.01) == 1000.0 - SNAP_DELTA


def test_ease_state_snaps_new_channels_and_drops_missing_ones() -> None:
    current = {1: (0.0, 0.0, 0.0), 9: (5.0, 5.0, 5.0)}
    targets = {1: ChannelColor(1, 40000, 20000, 0), 2: ChannelColor(2, 100, 200, 300)}
    eased = ease_state(current, targets, 0.5)
    assert eased == {1: (20000.0, 10000.0, 0.0), 2: (100.0, 200.0, 300.0)}
    assert current == {1: (0.0, 0.0, 0.0), 9: (5.0, 5.0, 5.0)}  # input not mutated


def test_repeated_easing_converges_exactly_and_settles() -> None:
    targets = {1: WHITE}
    state = {1: (0.0, 0.0, 0.0)}
    previous = -1.0
    for _ in range(200):
        state = ease_state(state, targets, smoothing_alpha(0.02, 0.15))
        assert state[1][0] >= previous  # monotonic approach, no overshoot
        previous = state[1][0]
        if is_settled(state, targets):
            break
    assert is_settled(state, targets)
    assert state_to_channels(state) == [WHITE]


def test_is_settled_requires_identical_channel_set() -> None:
    assert is_settled({}, {})
    assert not is_settled({}, {1: WHITE})
    assert not is_settled({1: (65535.0,) * 3, 2: (0.0,) * 3}, {1: WHITE})


def test_state_to_channels_rounds_clamps_and_orders() -> None:
    state = {2: (1.6, -3.0, 70000.0), 1: (0.4, 0.5, 1.5)}
    assert state_to_channels(state) == [ChannelColor(1, 0, 0, 2), ChannelColor(2, 2, 0, 65535)]


def test_smoothed_output_zero_returns_backend_unchanged() -> None:
    inner = RecordingBackend()
    assert smoothed_output(inner, 0, 50) is inner
    wrapped = smoothed_output(inner, 150, 50)
    assert isinstance(wrapped, SmoothedOutputBackend)
    assert wrapped.inner is inner
    assert wrapped.stats == {"type": "recording", "smoothing_ms": 150}


# --- output loop ---------------------------------------------------------------


async def test_jump_is_streamed_as_a_steady_fade_that_settles() -> None:
    inner = RecordingBackend()
    backend = SmoothedOutputBackend(inner, transition=0.02, fps=200)

    backend.send_frame([BLACK], COLOR_SPACE_RGB)  # first sight snaps
    assert _reds(inner.frames) == [0]
    assert backend._task is None  # already settled: no loop

    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    # Answered immediately with a partial step, not the hard jump.
    assert 0 < _reds(inner.frames)[-1] < 65535
    await _wait_idle(backend)

    reds = _reds(inner.frames)
    assert len(reds) > 5  # many interpolated frames from a single input frame
    assert reds == sorted(reds)
    assert reds[-1] == 65535
    assert all(space == COLOR_SPACE_RGB for _channels, space in inner.frames)
    assert backend._task is not None and backend._task.done()  # idle once settled


async def test_new_target_mid_fade_is_followed_without_restarting() -> None:
    inner = RecordingBackend()
    backend = SmoothedOutputBackend(inner, transition=0.05, fps=200)
    backend.send_frame([BLACK], COLOR_SPACE_RGB)
    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    task = backend._task
    await asyncio.sleep(0.03)
    backend.send_frame([BLACK], COLOR_SPACE_RGB)
    assert backend._task is task  # same loop picks up the new target
    await _wait_idle(backend)
    reds = _reds(inner.frames)
    peak = max(reds)
    assert 0 < peak < 65535  # turned around before reaching white
    assert reds[-1] == 0


async def test_colour_space_change_snaps_instead_of_mixing_components() -> None:
    inner = RecordingBackend()
    backend = SmoothedOutputBackend(inner, transition=0.5, fps=200)
    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    xy = ChannelColor(1, 20000, 21000, 30000)
    backend.send_frame([xy], COLOR_SPACE_XY)
    assert inner.frames[-1] == ([xy], COLOR_SPACE_XY)
    await backend.async_stop()


async def test_stop_cancels_fade_and_resets_state() -> None:
    inner = RecordingBackend()
    backend = SmoothedOutputBackend(inner, transition=1.0, fps=200)
    await backend.async_start()
    backend.send_frame([BLACK], COLOR_SPACE_RGB)
    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    task = backend._task
    assert task is not None and not task.done()

    await backend.async_stop()
    assert task.done()
    assert inner.stopped == 1
    count = len(inner.frames)
    await asyncio.sleep(0.02)
    assert len(inner.frames) == count  # no trailing frames after stop

    # A fresh session snaps to its first colour instead of fading from stale state.
    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    assert inner.frames[-1] == ([WHITE], COLOR_SPACE_RGB)
    await backend.async_close()
    assert inner.closed == 1


def test_without_running_loop_frames_pass_through() -> None:
    inner = RecordingBackend()
    backend = SmoothedOutputBackend(inner, transition=0.15, fps=50)
    backend.send_frame([BLACK], COLOR_SPACE_RGB)
    backend.send_frame([WHITE], COLOR_SPACE_RGB)
    assert inner.frames[-1] == ([WHITE], COLOR_SPACE_RGB)


async def test_delegates_availability_connection_and_light_commands() -> None:
    inner = RecordingBackend()
    inner.handle_light_command = Mock()  # type: ignore[method-assign]
    backend = SmoothedOutputBackend(inner, transition=0.15, fps=50)
    assert backend.available
    assert not backend.connected
    await backend.async_start()
    assert backend.connected
    backend.handle_light_command(3, {"on": True})
    inner.handle_light_command.assert_called_once_with(3, {"on": True})


# --- pipeline integration ------------------------------------------------------


def _pipeline(transition: float) -> tuple[SyncController, RecordingBackend]:
    inner = RecordingBackend()
    output = smoothed_output(inner, transition * 1000, 200)
    engine = SimpleNamespace(
        output_suppressed=False, status="idle", async_restore_lights=AsyncMock()
    )
    store = SimpleNamespace(async_delay_save=Mock(), async_save=AsyncMock())
    control = SyncController(
        output,
        engine,
        store,
        Mock(),
        enabled=True,
        intensity=1.0,
        track_engine_session=False,
    )
    return control, inner


async def test_stepped_jointspace_input_reaches_lamps_as_continuous_ramp() -> None:
    control, inner = _pipeline(0.03)
    assert await control.async_start()

    # Simulate ~10 Hz JointSpace polls stepping the colour up.
    for level in (0, 20000, 40000, 65535):
        control.send_frame([ChannelColor(1, level, level, level)], COLOR_SPACE_RGB)
        await asyncio.sleep(0.1 / 4)
    await _wait_idle(control._backend)  # type: ignore[arg-type]

    reds = _reds(inner.frames)
    assert reds == sorted(reds)
    assert reds[-1] == 65535
    # Far more output frames than input frames, and no single jump of a full step.
    assert len(reds) > 8
    assert max(b - a for a, b in zip(reds, reds[1:], strict=False)) < 20000
    await control.async_stop()


async def test_intensity_is_applied_before_smoothing_and_power_gate_holds() -> None:
    control, inner = _pipeline(0.02)
    await control.async_set_intensity(0.5)
    assert await control.async_start()
    control.send_frame([WHITE], COLOR_SPACE_RGB)
    await _wait_idle(control._backend)  # type: ignore[arg-type]
    assert inner.frames[-1][0] == [ChannelColor(1, 32768, 32768, 32768)]

    await control.async_stop()
    count = len(inner.frames)
    control.send_frame([BLACK], COLOR_SPACE_RGB)
    assert len(inner.frames) == count


async def test_zero_smoothing_keeps_verbatim_pipeline() -> None:
    control, inner = _pipeline(0.0)
    assert control._backend is inner
    assert await control.async_start()
    control.send_frame([BLACK], COLOR_SPACE_RGB)
    control.send_frame([WHITE], COLOR_SPACE_RGB)
    assert _reds(inner.frames) == [0, 65535]
