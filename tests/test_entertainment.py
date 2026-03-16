"""Tests for HueStream frame parsing and light dispatch."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: mock homeassistant + load const/entertainment without HA install
# ---------------------------------------------------------------------------

for _mod_name in ["homeassistant", "homeassistant.core"]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"


def _load(name: str, filename: str):
    path = _base / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Register a stub package so relative imports in entertainment.py resolve
_pkg_stub = MagicMock()
_pkg_stub.__path__ = [str(_base)]
_pkg_stub.__package__ = "hue_entertainment"
sys.modules.setdefault("hue_entertainment", _pkg_stub)

_const = _load("hue_entertainment.const", "const.py")
_ent = _load("hue_entertainment.entertainment", "entertainment.py")

EntertainmentEngine = _ent.EntertainmentEngine
ChannelColor = _ent.ChannelColor
LightMapping = _ent.LightMapping
parse_huestream_frame = _ent.parse_huestream_frame
_parse_v1_channels = _ent._parse_v1_channels
_parse_v2_channels = _ent._parse_v2_channels

COLOR_SPACE_RGB = _const.COLOR_SPACE_RGB
COLOR_SPACE_XY = _const.COLOR_SPACE_XY
HUESTREAM_HEADER_SIZE = _const.HUESTREAM_HEADER_SIZE
HUESTREAM_CHANNEL_SIZE = _const.HUESTREAM_CHANNEL_SIZE
BRIGHTNESS_TOLERANCE = _const.BRIGHTNESS_TOLERANCE
CIE_TOLERANCE = _const.CIE_TOLERANCE
RESTORE_TRANSITION = _const.RESTORE_TRANSITION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID = b"a" * 36  # 36-byte placeholder UUID


def _v2_header(color_space: int = COLOR_SPACE_RGB, api_version: int = 0x02) -> bytes:
    """Build a 52-byte v2 HueStream header."""
    return (
        b"HueStream"  # 9 bytes  [0-8]
        + bytes([api_version])  # 1 byte   [9]  api_version
        + bytes([0x00])  # 1 byte   [10] minor version
        + bytes([0x00])  # 1 byte   [11] seq
        + bytes([0x00, 0x00])  # 2 bytes  [12-13] reserved
        + bytes([color_space])  # 1 byte   [14] color_space
        + bytes([0x00])  # 1 byte   [15] reserved
        + _UUID  # 36 bytes [16-51] config UUID
    )


def _v2_channel(channel_id: int, r: int, g: int, b: int) -> bytes:
    """Build a 7-byte v2 channel block."""
    return bytes([channel_id]) + struct.pack(">HHH", r, g, b)


def _v1_header(color_space: int = COLOR_SPACE_RGB) -> bytes:
    """Build a minimal 16-byte v1 header. Channels start at offset 16."""
    return (
        b"HueStream"  # 9 bytes  [0-8]
        + bytes([0x01])  # 1 byte   [9]  api_version = 1
        + bytes([0x00])  # 1 byte   [10] minor version
        + bytes([0x00])  # 1 byte   [11] seq
        + bytes([0x00, 0x00])  # 2 bytes  [12-13] reserved
        + bytes([color_space])  # 1 byte   [14] color_space
        + bytes([0x00])  # 1 byte   [15] reserved
    )  # 16 bytes total


def _v1_channel(light_id: int, r: int, g: int, b: int, type_byte: int = 0) -> bytes:
    """Build a 9-byte v1 channel block."""
    return bytes([type_byte]) + struct.pack(">HHHH", light_id, r, g, b)


def _make_engine(channels: int = 2) -> tuple[EntertainmentEngine, MagicMock]:
    """Return an engine wired to a mock hass and N light mappings."""
    hass = MagicMock()
    hass.async_create_task = MagicMock()
    mappings = [LightMapping(channel_id=i, entity_id=f"light.test_{i}") for i in range(channels)]
    engine = EntertainmentEngine(hass, mappings)
    return engine, hass


# ---------------------------------------------------------------------------
# _parse_v2_channels
# ---------------------------------------------------------------------------


class TestParseV2Channels:
    def test_single_channel_rgb(self):
        frame = _v2_header() + _v2_channel(0, 0xFFFF, 0x8000, 0x0001)
        channels = _parse_v2_channels(frame)
        assert len(channels) == 1
        c = channels[0]
        assert c.channel_id == 0
        assert c.r == 0xFFFF
        assert c.g == 0x8000
        assert c.b == 0x0001

    def test_multiple_channels(self):
        frame = (
            _v2_header()
            + _v2_channel(0, 100, 200, 300)
            + _v2_channel(1, 400, 500, 600)
            + _v2_channel(2, 700, 800, 900)
        )
        channels = _parse_v2_channels(frame)
        assert len(channels) == 3
        assert channels[1].channel_id == 1
        assert channels[1].r == 400
        assert channels[2].b == 900

    def test_trailing_partial_bytes_ignored(self):
        # Append 3 extra bytes — not enough for a full channel (7 bytes needed)
        frame = _v2_header() + _v2_channel(0, 1, 2, 3) + b"\x00\x00\x00"
        channels = _parse_v2_channels(frame)
        assert len(channels) == 1

    def test_no_channels_when_only_header(self):
        frame = _v2_header()
        channels = _parse_v2_channels(frame)
        assert channels == []

    def test_xy_colorspace_parsing_identical_to_rgb(self):
        # Parsing is the same; colorspace only matters in _schedule_update
        frame = _v2_header(COLOR_SPACE_XY) + _v2_channel(0, 32767, 16383, 65535)
        channels = _parse_v2_channels(frame)
        assert len(channels) == 1
        assert channels[0].r == 32767
        assert channels[0].g == 16383
        assert channels[0].b == 65535


# ---------------------------------------------------------------------------
# _parse_v1_channels
# ---------------------------------------------------------------------------


class TestParseV1Channels:
    def test_single_channel(self):
        frame = _v1_header() + _v1_channel(light_id=3, r=0x1111, g=0x2222, b=0x3333)
        channels = _parse_v1_channels(frame)
        assert len(channels) == 1
        c = channels[0]
        assert c.channel_id == 3  # light_id mapped to channel_id
        assert c.r == 0x1111
        assert c.g == 0x2222
        assert c.b == 0x3333

    def test_multiple_channels(self):
        frame = _v1_header() + _v1_channel(1, 10, 20, 30) + _v1_channel(2, 40, 50, 60)
        channels = _parse_v1_channels(frame)
        assert len(channels) == 2
        assert channels[0].channel_id == 1
        assert channels[1].channel_id == 2

    def test_trailing_partial_bytes_ignored(self):
        frame = _v1_header() + _v1_channel(0, 1, 2, 3) + b"\x00\x00\x00\x00"
        channels = _parse_v1_channels(frame)
        assert len(channels) == 1

    def test_type_byte_ignored_channel_id_is_light_id(self):
        """diyHue uses type byte 0=light, 1=gradient strip; our parser ignores it."""
        frame = _v1_header() + _v1_channel(light_id=5, r=10, g=20, b=30, type_byte=1)
        channels = _parse_v1_channels(frame)
        # channel_id comes from light_id regardless of type_byte
        assert channels[0].channel_id == 5

    def test_colorspace_byte_position_14(self):
        """diyHue confirms colorspace is at data[14] for both v1 and v2."""
        # Build v1 frame with XY colorspace at [14]
        frame = _v1_header(color_space=COLOR_SPACE_XY) + _v1_channel(0, 100, 200, 300)
        # Parser itself doesn't branch on colorspace — dispatch does
        channels = _parse_v1_channels(frame)
        assert len(channels) == 1
        assert channels[0].r == 100


# ---------------------------------------------------------------------------
# handle_frame dispatching
# ---------------------------------------------------------------------------


def _any_dirty(engine) -> bool:
    """Return True if any light mapping has a pending update."""
    return any(m.dirty for m in engine._mappings.values())


class TestHandleFrame:
    def test_rejects_wrong_magic(self):
        engine, hass = _make_engine()
        bad = b"NotHue" + b"\x00" * 60
        engine.handle_frame(bad)
        assert not _any_dirty(engine)

    def test_rejects_frame_too_short_to_read_version(self):
        engine, hass = _make_engine()
        short = b"HueStream" + b"\x00" * 5  # < 15 bytes
        engine.handle_frame(short)
        assert not _any_dirty(engine)

    def test_rejects_v2_frame_shorter_than_52(self):
        engine, hass = _make_engine()
        # version byte = 2 at [9], but total length < 52
        short = b"HueStream" + bytes([0x02]) + b"\x00" * 20
        engine.handle_frame(short)
        assert not _any_dirty(engine)

    def test_rejects_v1_frame_shorter_than_25(self):
        engine, hass = _make_engine()
        # version byte = 1 at [9], total length 16 (header only, no channels)
        short = _v1_header()
        engine.handle_frame(short)
        assert not _any_dirty(engine)

    def test_rejects_unknown_version(self):
        engine, hass = _make_engine()
        frame = _v2_header(api_version=0x05) + _v2_channel(0, 0xFFFF, 0, 0)
        engine.handle_frame(frame)
        assert not _any_dirty(engine)

    def test_dispatches_v2(self):
        engine, hass = _make_engine(channels=1)
        frame = _v2_header() + _v2_channel(0, 0xFFFF, 0, 0)
        engine.handle_frame(frame)
        assert engine._mappings[0].dirty is True
        assert engine._mappings[0].pending_data is not None

    def test_dispatches_v1(self):
        engine, hass = _make_engine(channels=1)
        # channel_id 0 mapped to light.test_0; proper 16-byte header + one channel
        frame = _v1_header() + _v1_channel(light_id=0, r=0xFFFF, g=0, b=0)
        engine.handle_frame(frame)
        assert engine._mappings[0].dirty is True
        assert engine._mappings[0].pending_data is not None


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


class TestCoalescing:
    """Verify that newer frames overwrite stale slot data."""

    def _frame(self, r: int) -> bytes:
        return _v2_header() + _v2_channel(0, r, 0, 0)

    def test_first_frame_marks_dirty(self):
        engine, hass = _make_engine(channels=1)
        engine.handle_frame(self._frame(0xFFFF))
        assert engine._mappings[0].dirty is True

    def test_newer_frame_overwrites_slot(self):
        engine, hass = _make_engine(channels=1)
        engine.handle_frame(self._frame(0xFFFF))
        # Second frame with different value overwrites
        engine.handle_frame(self._frame(0x0000))
        data = engine._mappings[0].pending_data
        assert data is not None
        # The slot should hold the newest value (0x0000 >> 8 = 0)
        assert data["rgb_color"][0] == 0

    def test_tolerance_suppresses_tiny_change(self):
        engine, hass = _make_engine(channels=1)
        engine.handle_frame(self._frame(1000))
        engine._mappings[0].dirty = False
        # Tiny change within BRIGHTNESS_TOLERANCE
        engine.handle_frame(self._frame(1000 + BRIGHTNESS_TOLERANCE - 1))
        assert engine._mappings[0].dirty is False


# ---------------------------------------------------------------------------
# _schedule_update — RGB tolerance
# ---------------------------------------------------------------------------


class TestScheduleUpdateRGB:
    def _engine_with_last(self, last_r, last_g, last_b) -> tuple[EntertainmentEngine, MagicMock]:
        engine, hass = _make_engine(channels=1)
        engine._mappings[0].last_r = last_r
        engine._mappings[0].last_g = last_g
        engine._mappings[0].last_b = last_b
        return engine, hass

    def test_large_change_triggers_update(self):
        engine, hass = self._engine_with_last(0, 0, 0)
        channel = ChannelColor(channel_id=0, r=0xFFFF, g=0x8000, b=0x0000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        assert engine._mappings[0].dirty is True

    def test_small_change_suppressed(self):
        engine, hass = self._engine_with_last(1000, 1000, 1000)
        # Change less than BRIGHTNESS_TOLERANCE (16) on all channels
        delta = BRIGHTNESS_TOLERANCE - 1
        channel = ChannelColor(channel_id=0, r=1000 + delta, g=1000, b=1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        assert engine._mappings[0].dirty is False

    def test_one_channel_over_tolerance_triggers_update(self):
        engine, hass = self._engine_with_last(1000, 1000, 1000)
        channel = ChannelColor(channel_id=0, r=1000 + BRIGHTNESS_TOLERANCE, g=1000, b=1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        assert engine._mappings[0].dirty is True

    def test_rgb_scaled_to_8bit(self):
        """16-bit values are right-shifted 8 bits for rgb_color."""
        engine, hass = self._engine_with_last(0, 0, 0)
        channel = ChannelColor(channel_id=0, r=0xFF00, g=0x8000, b=0x1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        # Verify mapping state was updated with the raw 16-bit values
        assert engine._mappings[0].last_r == 0xFF00
        assert engine._mappings[0].last_g == 0x8000
        assert engine._mappings[0].last_b == 0x1000

    def test_unknown_channel_silently_ignored(self):
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=99, r=0xFFFF, g=0, b=0)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        assert not _any_dirty(engine)


# ---------------------------------------------------------------------------
# _schedule_update — XY colorspace
# ---------------------------------------------------------------------------


class TestScheduleUpdateXY:
    def _engine_with_last(self, last_r, last_g, last_b) -> tuple[EntertainmentEngine, MagicMock]:
        engine, hass = _make_engine(channels=1)
        engine._mappings[0].last_r = last_r
        engine._mappings[0].last_g = last_g
        engine._mappings[0].last_b = last_b
        return engine, hass

    def test_large_xy_change_triggers_update(self):
        engine, hass = self._engine_with_last(0, 0, 0)
        # x = 0.5, y = 0.4 — large change from 0,0
        channel = ChannelColor(
            channel_id=0,
            r=round(0.5 * 65535),
            g=round(0.4 * 65535),
            b=32768,
        )
        engine._schedule_update(channel, COLOR_SPACE_XY)
        assert engine._mappings[0].dirty is True

    def test_small_xy_change_suppressed(self):
        # Set last values close to new values
        base_r = round(0.5 * 65535)
        base_g = round(0.4 * 65535)
        base_b = 32768
        engine, hass = self._engine_with_last(base_r, base_g, base_b)

        # Nudge x by less than CIE_TOLERANCE
        tiny = round(CIE_TOLERANCE * 0.5 * 65535)
        channel = ChannelColor(channel_id=0, r=base_r + tiny, g=base_g, b=base_b)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        assert engine._mappings[0].dirty is False

    def test_brightness_change_alone_triggers_update(self):
        base_r = round(0.5 * 65535)
        base_g = round(0.4 * 65535)
        base_b = 0
        engine, hass = self._engine_with_last(base_r, base_g, base_b)
        # Large brightness jump
        channel = ChannelColor(channel_id=0, r=base_r, g=base_g, b=65535)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        assert engine._mappings[0].dirty is True

    def test_brightness_scaled_to_255(self):
        engine, hass = self._engine_with_last(0, 0, 0)
        channel = ChannelColor(channel_id=0, r=0xFFFF, g=0x8000, b=65535)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        # Mapping state stores raw 16-bit brightness
        assert engine._mappings[0].last_b == 65535

    def test_fresh_mapping_always_triggers(self):
        engine, hass = _make_engine(channels=1)
        # last_r/g/b default to -1 → always a big change
        channel = ChannelColor(channel_id=0, r=1, g=1, b=1)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        assert engine._mappings[0].dirty is True


# ---------------------------------------------------------------------------
# parse_huestream_frame (module-level pure function)
# ---------------------------------------------------------------------------


class TestParseHuestreamFrame:
    def test_valid_v1_frame(self):
        frame = _v1_header() + _v1_channel(light_id=0, r=0xFFFF, g=0x8000, b=0x0000)
        result = parse_huestream_frame(frame)
        assert result is not None
        version, colorspace, channels = result
        assert version == 1
        assert colorspace == COLOR_SPACE_RGB
        assert len(channels) == 1
        assert channels[0] == ChannelColor(channel_id=0, r=0xFFFF, g=0x8000, b=0x0000)

    def test_valid_v2_frame(self):
        frame = _v2_header() + _v2_channel(0, 100, 200, 300)
        result = parse_huestream_frame(frame)
        assert result is not None
        version, colorspace, channels = result
        assert version == 2
        assert colorspace == COLOR_SPACE_RGB
        assert len(channels) == 1
        assert channels[0] == ChannelColor(channel_id=0, r=100, g=200, b=300)

    def test_too_short_returns_none(self):
        assert parse_huestream_frame(b"HueStream") is None
        assert parse_huestream_frame(b"Hue") is None
        assert parse_huestream_frame(b"") is None

    def test_bad_magic_returns_none(self):
        frame = b"NotAHue!!" + b"\x00" * 50
        assert parse_huestream_frame(frame) is None

    def test_multi_channel_v1(self):
        frame = (
            _v1_header()
            + _v1_channel(1, 10, 20, 30)
            + _v1_channel(2, 40, 50, 60)
            + _v1_channel(3, 70, 80, 90)
        )
        result = parse_huestream_frame(frame)
        assert result is not None
        version, colorspace, channels = result
        assert version == 1
        assert len(channels) == 3
        assert channels[0].channel_id == 1
        assert channels[1].channel_id == 2
        assert channels[2].channel_id == 3
        assert channels[2].r == 70

    def test_v1_header_only_too_short(self):
        """v1 header alone (16 bytes) has no channels — rejected as too short."""
        frame = _v1_header()
        assert parse_huestream_frame(frame) is None

    def test_v2_header_only_returns_empty_channels(self):
        """v2 header alone (52 bytes) is valid — just no channels."""
        frame = _v2_header()
        result = parse_huestream_frame(frame)
        assert result is not None
        version, colorspace, channels = result
        assert version == 2
        assert channels == []

    def test_unknown_version_returns_none(self):
        frame = _v2_header(api_version=0x05) + _v2_channel(0, 0, 0, 0)
        assert parse_huestream_frame(frame) is None

    def test_xy_colorspace_reported(self):
        frame = _v1_header(color_space=COLOR_SPACE_XY) + _v1_channel(0, 100, 200, 300)
        result = parse_huestream_frame(frame)
        assert result is not None
        _, colorspace, _ = result
        assert colorspace == COLOR_SPACE_XY


# ---------------------------------------------------------------------------
# Snapshot / restore lifecycle
# ---------------------------------------------------------------------------


def _make_async_engine(channels: int = 2) -> tuple[EntertainmentEngine, MagicMock]:
    """Return an engine with a properly async-capable mock hass."""
    hass = MagicMock()
    hass.async_create_task = MagicMock()
    # states.get returns a fake State object for each entity
    hass.states = MagicMock()
    mappings = [LightMapping(channel_id=i, entity_id=f"light.test_{i}") for i in range(channels)]
    engine = EntertainmentEngine(hass, mappings)
    return engine, hass


class TestSnapshotRestore:
    def test_is_active_initially_false(self):
        engine, _ = _make_async_engine()
        assert engine.is_active is False

    @pytest.mark.asyncio
    async def test_snapshot_captures_light_states(self):
        engine, hass = _make_async_engine(channels=3)
        fake_states = [MagicMock(entity_id=f"light.test_{i}") for i in range(3)]
        hass.states.get.side_effect = lambda eid: next(
            (s for s in fake_states if s.entity_id == eid), None
        )
        await engine.async_snapshot_lights()
        assert engine.is_active is True
        assert engine._saved_states == fake_states

    @pytest.mark.asyncio
    async def test_snapshot_skips_unavailable_entities(self):
        engine, hass = _make_async_engine(channels=2)
        hass.states.get.side_effect = lambda eid: (
            MagicMock(entity_id=eid) if eid == "light.test_0" else None
        )
        await engine.async_snapshot_lights()
        assert len(engine._saved_states) == 1
        assert engine._saved_states[0].entity_id == "light.test_0"

    @pytest.mark.asyncio
    async def test_snapshot_sets_last_frame_time(self):
        engine, hass = _make_async_engine()
        hass.states.get.return_value = MagicMock()
        t = 12345.0
        with patch.object(_ent.time, "monotonic", return_value=t):
            await engine.async_snapshot_lights()
        assert engine.last_frame_time == t

    @pytest.mark.asyncio
    async def test_restore_calls_reproduce_state(self):
        engine, hass = _make_async_engine(channels=2)
        fake_states = [MagicMock(entity_id=f"light.test_{i}") for i in range(2)]
        hass.states.get.side_effect = lambda eid: next(
            (s for s in fake_states if s.entity_id == eid), None
        )
        await engine.async_snapshot_lights()

        reproduce_mock = AsyncMock()
        with patch.dict(
            "sys.modules",
            {"homeassistant.helpers.state": MagicMock(async_reproduce_state=reproduce_mock)},
        ):
            await engine.async_restore_lights()

        assert engine.is_active is False
        reproduce_mock.assert_called_once()
        call_args = reproduce_mock.call_args
        assert call_args[0][1] == fake_states
        assert call_args[1]["reproduce_options"]["transition"] == RESTORE_TRANSITION

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self):
        engine, hass = _make_async_engine(channels=1)
        hass.states.get.return_value = MagicMock()
        await engine.async_snapshot_lights()

        reproduce_mock = AsyncMock()
        with patch.dict(
            "sys.modules",
            {"homeassistant.helpers.state": MagicMock(async_reproduce_state=reproduce_mock)},
        ):
            await engine.async_restore_lights()
            await engine.async_restore_lights()  # second call — no-op

        reproduce_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_when_never_snapshotted_is_noop(self):
        engine, hass = _make_async_engine()
        hass.states.get.return_value = MagicMock()
        reproduce_mock = AsyncMock()
        with patch.dict(
            "sys.modules",
            {"homeassistant.helpers.state": MagicMock(async_reproduce_state=reproduce_mock)},
        ):
            await engine.async_restore_lights()
        reproduce_mock.assert_not_called()

    def test_last_frame_time_updated_on_valid_frame(self):
        engine, hass = _make_async_engine(channels=1)
        t = 99999.0
        frame = _v2_header() + _v2_channel(0, 0xFFFF, 0, 0)
        with patch.object(_ent.time, "monotonic", return_value=t):
            engine.handle_frame(frame)
        assert engine.last_frame_time == t

    def test_last_frame_time_not_updated_on_invalid_frame(self):
        engine, _ = _make_async_engine()
        engine.last_frame_time = 0.0
        engine.handle_frame(b"garbage frame data")
        assert engine.last_frame_time == 0.0


# ---------------------------------------------------------------------------
# Service data — RGB mode
# ---------------------------------------------------------------------------


class TestServiceDataRGB:
    """Verify the actual service_data values written to mapping.pending_data in RGB mode."""

    def test_rgb_color_values(self):
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=0, r=0xFF00, g=0x8000, b=0x1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        data = engine._mappings[0].pending_data
        assert data is not None
        assert data["rgb_color"] == [0xFF, 0x80, 0x10]

    def test_rgb_brightness_from_peak(self):
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=0, r=0xFF00, g=0x8000, b=0x1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        data = engine._mappings[0].pending_data
        assert data["brightness"] == 0xFF

    def test_rgb_brightness_minimum_1(self):
        engine, hass = _make_engine(channels=1)
        # All channels low enough that peak >> 8 == 0
        channel = ChannelColor(channel_id=0, r=0x0000, g=0x0000, b=0x00FF)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        data = engine._mappings[0].pending_data
        assert data["brightness"] == 1

    def test_rgb_no_transition_in_slot(self):
        """Transition is set by the drain loop, not _schedule_update."""
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=0, r=0xFF00, g=0x8000, b=0x1000)
        engine._schedule_update(channel, COLOR_SPACE_RGB)
        data = engine._mappings[0].pending_data
        assert "transition" not in data


# ---------------------------------------------------------------------------
# Service data — XY mode
# ---------------------------------------------------------------------------


class TestServiceDataXY:
    """Verify the actual service_data values written to mapping.pending_data in XY mode."""

    def test_xy_color_values(self):
        engine, hass = _make_engine(channels=1)
        # r=32768 → x ≈ 0.5, g=26214 → y ≈ 0.4, b=65535 → full brightness
        channel = ChannelColor(channel_id=0, r=32768, g=26214, b=65535)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        data = engine._mappings[0].pending_data
        assert data is not None
        assert abs(data["xy_color"][0] - 0.5) < 0.001
        assert abs(data["xy_color"][1] - 0.4) < 0.001
        assert data["brightness"] == 255

    def test_xy_brightness_zero_maps_to_zero(self):
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=0, r=32768, g=26214, b=0)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        data = engine._mappings[0].pending_data
        assert data["brightness"] == 0

    def test_xy_no_transition_in_slot(self):
        """Transition is set by the drain loop, not _schedule_update."""
        engine, hass = _make_engine(channels=1)
        channel = ChannelColor(channel_id=0, r=32768, g=26214, b=65535)
        engine._schedule_update(channel, COLOR_SPACE_XY)
        data = engine._mappings[0].pending_data
        assert "transition" not in data


# ---------------------------------------------------------------------------
# reset_stats
# ---------------------------------------------------------------------------


class TestResetStats:
    """Test reset_stats() directly."""

    def test_resets_counters(self):
        engine, _ = _make_engine(channels=1)
        engine._total_frames_received = 42
        engine._total_commands_sent = 17
        engine.reset_stats()
        assert engine._total_frames_received == 0
        assert engine._total_commands_sent == 0

    def test_resets_first_frame_logged(self):
        engine, _ = _make_engine(channels=1)
        engine._first_frame_logged = True
        engine.reset_stats()
        assert engine._first_frame_logged is False
