"""Tests for the persistent synchronization runtime control."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.hue_entertainment import backends as backends_module
from custom_components.hue_entertainment.backends import (
    EntertainmentOutputBackend,
    HueEntertainmentBackend,
)
from custom_components.hue_entertainment.const import COLOR_SPACE_RGB, COLOR_SPACE_XY
from custom_components.hue_entertainment.control import SyncController
from custom_components.hue_entertainment.entertainment import ChannelColor


class RecordingBackend(EntertainmentOutputBackend):
    """Minimal output that records normalized frames."""

    def __init__(self) -> None:
        self.started = False
        self.start_count = 0
        self.frames: list[tuple[list[ChannelColor], int]] = []

    async def async_start(self) -> None:
        self.started = True
        self.start_count += 1

    def send_frame(self, channels: list[ChannelColor], color_space: int) -> None:
        self.frames.append((channels, color_space))

    async def async_stop(self) -> None:
        self.started = False

    @property
    def connected(self) -> bool:
        return self.started


def _controller() -> tuple[SyncController, RecordingBackend, SimpleNamespace]:
    backend = RecordingBackend()
    engine = SimpleNamespace(
        output_suppressed=False,
        status="idle",
        async_restore_lights=AsyncMock(),
    )
    store = SimpleNamespace(
        async_delay_save=Mock(),
        async_save=AsyncMock(),
    )
    control = SyncController(
        backend,
        engine,
        store,
        Mock(),
        enabled=True,
        intensity=1.0,
        track_engine_session=False,
    )
    return control, backend, engine


@pytest.mark.asyncio
async def test_intensity_scales_rgb_and_xy_luminance_without_mutating_input() -> None:
    control, backend, _engine = _controller()
    assert await control.async_start()
    await control.async_set_intensity(0.5)

    rgb = ChannelColor(1, 60000, 30000, 10000)
    control.send_frame([rgb], COLOR_SPACE_RGB)
    assert backend.frames[-1] == ([ChannelColor(1, 30000, 15000, 5000)], COLOR_SPACE_RGB)
    assert rgb == ChannelColor(1, 60000, 30000, 10000)

    xy = ChannelColor(1, 20000, 30000, 40000)
    control.send_frame([xy], COLOR_SPACE_XY)
    assert backend.frames[-1] == ([ChannelColor(1, 20000, 30000, 20000)], COLOR_SPACE_XY)


@pytest.mark.asyncio
async def test_intensity_boundaries_and_power_gate_actual_output() -> None:
    control, backend, engine = _controller()
    assert await control.async_start()

    await control.async_set_intensity(-1)
    assert control.intensity == 0
    control.send_frame([ChannelColor(1, 65535, 32768, 1)], COLOR_SPACE_RGB)
    assert backend.frames[-1][0] == [ChannelColor(1, 0, 0, 0)]

    await control.async_set_intensity(2)
    assert control.intensity == 1

    await control.async_set_enabled(False)
    assert not backend.started
    assert not control.connected
    before = len(backend.frames)
    control.send_frame([ChannelColor(1, 1, 2, 3)], COLOR_SPACE_RGB)
    assert len(backend.frames) == before
    engine.async_restore_lights.assert_awaited_once()

    await control.async_set_enabled(True)
    assert await control.async_start()
    assert control.connected

    # A backend that reports a lost connection is started again on the next frame cycle.
    backend.started = False
    assert await control.async_start()
    assert backend.start_count == 3


@pytest.mark.asyncio
async def test_physical_hue_runtime_start_and_recreation_do_not_import_dependency(
    monkeypatch,
) -> None:
    """The installed Hue dependency is resolved before the async runtime path."""
    sessions = []
    import_attempts = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs) -> None:
            self.is_streaming = False
            self.closed = False
            self.fail_send = False
            sessions.append(self)

        async def start(self, _area_id: str) -> None:
            self.is_streaming = True

        def send(self, _commands) -> None:
            if self.fail_send:
                raise OSError("synthetic disconnect")

        async def stop(self) -> None:
            self.is_streaming = False

        async def aclose(self) -> None:
            self.closed = True
            self.is_streaming = False

    class FakeCommand:
        def __init__(self, **kwargs) -> None:
            self.values = kwargs

    def reject_runtime_import(name: str, *_args, **_kwargs):
        if name == "hue_entertainment":
            import_attempts.append(name)
            raise AssertionError("Hue dependency imported from async runtime")
        return original_import_module(name, *_args, **_kwargs)

    original_import_module = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", reject_runtime_import)
    if hasattr(backends_module, "import_module"):
        monkeypatch.setattr(backends_module, "import_module", reject_runtime_import)
    monkeypatch.setattr(backends_module, "EntertainmentSession", FakeSession)
    monkeypatch.setattr(backends_module, "LightColorCommand", FakeCommand)

    backend = HueEntertainmentBackend(
        "synthetic-bridge",
        "synthetic-app-key",
        "synthetic-client-key",
        "synthetic-area",
        {1: 0},
    )
    await backend.async_start()
    assert backend.connected
    assert len(sessions) == 1

    sessions[0].fail_send = True
    backend.send_frame([ChannelColor(1, 1000, 2000, 3000)], COLOR_SPACE_RGB)
    assert not backend.connected

    await backend.async_start()
    assert sessions[0].closed
    assert len(sessions) == 2
    assert backend.connected
    assert import_attempts == []
    await backend.async_close()
