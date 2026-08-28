"""Tests for pause/resume/release, the status sensor's source property, and the
drain-loop hardening (try/finally around _drain_running, the _drain_running-not-
Task.done() guard in _ensure_drain_task).

entertainment.py is loaded hermetically (no running HA) by tests/engine_harness.py,
shared with test_entertainment.py; pause/release timers are recorded by its fake
async_call_later and fired explicitly here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.engine_harness import (
    EntertainmentEngine,
    LightMapping,
    scheduled_calls,
    v2_channel,
    v2_header,
)
from tests.engine_harness import (
    const as _const,
)
from tests.engine_harness import (
    ent as _ent,
)

DEFAULT_YIELD_SECONDS = _const.DEFAULT_YIELD_SECONDS


class _FakeTask:
    """A task-shaped object whose await raises CancelledError, like a real
    cancelled asyncio.Task — matches the pattern in test_entertainment.py."""

    def __init__(self):
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self._done = True

    def __await__(self):
        if False:
            yield
        raise asyncio.CancelledError


def _make_live_engine(channels: int = 2, notify=None):
    """Engine on a real loop; hass.services.async_call is an AsyncMock, and
    hass.async_create_task really runs the coroutine on the current loop so
    the drain loop's try/finally and _drain_running bookkeeping are exercised
    for real, not mocked away."""
    hass = MagicMock()
    hass.async_create_task = lambda coro: asyncio.get_running_loop().create_task(coro)
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=MagicMock(state="on"))
    mappings = [LightMapping(channel_id=i, entity_id=f"light.test_{i}") for i in range(channels)]
    return EntertainmentEngine(hass, mappings, notify=notify), hass


@pytest.fixture(autouse=True)
def _clear_scheduled_calls():
    scheduled_calls.clear()
    yield
    scheduled_calls.clear()


# ---------------------------------------------------------------------------
# status — single source of truth, precedence
# ---------------------------------------------------------------------------


class TestStatus:
    def test_idle_by_default(self):
        engine, _ = _make_live_engine()
        assert engine.status == "idle"
        assert engine.is_driving_lights is False

    @pytest.mark.asyncio
    async def test_streaming_while_active(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        assert engine.status == "streaming"
        assert engine.is_driving_lights is True

    @pytest.mark.asyncio
    async def test_classic_while_drain_running_without_active(self):
        engine, hass = _make_live_engine()
        engine.handle_light_command(0, {"on": True, "bri": 200})
        await asyncio.sleep(0)  # let the drain task actually start
        assert engine.status == "classic"
        assert engine.is_driving_lights is True
        engine._drain_task.cancel()

    @pytest.mark.asyncio
    async def test_paused_overrides_streaming_and_reports_off(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_pause(5)
        assert engine.status == "paused"
        assert engine.is_driving_lights is False
        assert engine.status_attributes["underlying_activity"] == "streaming"

    @pytest.mark.asyncio
    async def test_releasing_overrides_everything(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        assert engine.status == "releasing"
        assert engine.is_driving_lights is False


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_suppresses_frame_effects_but_keeps_counting(self):
        engine, hass = _make_live_engine(channels=1)
        await engine.async_pause(5)
        frame = _make_v2_frame()
        engine.handle_frame(frame)
        assert engine._mappings[0].dirty is False  # effect dropped
        assert engine._total_frames_received == 1  # still counted

    @pytest.mark.asyncio
    async def test_pause_never_blocks_a_session_starting(self):
        engine, _ = _make_live_engine()
        await engine.async_pause(5)
        await engine.async_snapshot_lights()
        assert engine.is_active is True  # handshake/session start unaffected by pause

    @pytest.mark.asyncio
    async def test_pause_zero_resolves_to_default(self):
        engine, _ = _make_live_engine()
        with patch.object(_ent.time, "monotonic", return_value=1000.0):
            await engine.async_pause(0)
        assert engine._paused_until == 1000.0 + DEFAULT_YIELD_SECONDS

    @pytest.mark.asyncio
    async def test_pause_auto_expires_via_scheduled_callback(self):
        engine, _ = _make_live_engine()
        notify = MagicMock()
        engine._notify = notify
        await engine.async_pause(5)
        assert engine.status == "paused"
        scheduled_calls[-1].fire()
        assert engine.status == "idle"
        assert notify.called

    @pytest.mark.asyncio
    async def test_pause_self_heals_even_if_callback_lost(self):
        """_is_paused is wall-clock derived — correctness doesn't depend on
        the scheduled callback actually firing."""
        engine, _ = _make_live_engine()
        with patch.object(_ent.time, "monotonic", return_value=1000.0):
            await engine.async_pause(5)
        # Callback never fires (simulating it being lost) — but time moves on
        with patch.object(_ent.time, "monotonic", return_value=1006.0):
            assert engine.status == "idle"

    @pytest.mark.asyncio
    async def test_resume_cancels_pause_early(self):
        engine, _ = _make_live_engine()
        await engine.async_pause(30)
        await engine.async_resume()
        assert engine.status == "idle"
        assert scheduled_calls[-1].cancelled is True

    @pytest.mark.asyncio
    async def test_resume_is_noop_when_nothing_paused(self):
        engine, _ = _make_live_engine()
        await engine.async_resume()  # must not raise
        assert engine.status == "idle"

    @pytest.mark.asyncio
    async def test_repeated_pause_resets_timer_last_call_wins(self):
        engine, _ = _make_live_engine()
        with patch.object(_ent.time, "monotonic", return_value=1000.0):
            await engine.async_pause(5)
        with patch.object(_ent.time, "monotonic", return_value=1002.0):
            await engine.async_pause(5)  # re-called 2s later
        assert engine._paused_until == 1002.0 + 5
        assert scheduled_calls[0].cancelled is True  # first timer superseded

    @pytest.mark.asyncio
    async def test_pause_flushes_a_command_already_queued_before_the_call(self):
        """Regression for the live 2026-08-28 finding: the gate in
        handle_frame only blocks *new* commands from being queued while
        suppressed — it never touched what a frame already queued a moment
        earlier. Against a live ~25fps stream something is queued almost
        continuously, so without a flush at pause()/release() time, that
        stale command survives into the drain loop and gets sent anyway."""
        engine, _ = _make_live_engine(channels=1)
        engine.handle_frame(_make_v2_frame())
        assert engine._mappings[0].dirty is True  # sanity: it really queued

        await engine.async_pause(5)

        assert engine._mappings[0].dirty is False
        assert engine._mappings[0].pending_data is None


def _make_v2_frame(channel_id: int = 0) -> bytes:
    return v2_header() + v2_channel(channel_id, 30000, 30000, 30000)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_discards_saved_states(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        assert engine._saved_states  # snapshot was taken
        await engine.async_release(2)
        assert engine._saved_states is None

    @pytest.mark.asyncio
    async def test_release_suppresses_frame_and_classic_effects(self):
        engine, _ = _make_live_engine(channels=1)
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        engine.handle_frame(_make_v2_frame())
        engine.handle_light_command(0, {"on": True, "bri": 200})
        assert engine._mappings[0].dirty is False

    @pytest.mark.asyncio
    async def test_release_wins_over_in_progress_pause(self):
        engine, _ = _make_live_engine()
        await engine.async_pause(30)
        await engine.async_release(2)
        assert engine.status == "releasing"
        assert scheduled_calls[0].cancelled is True  # the pause timer

    @pytest.mark.asyncio
    async def test_pause_is_noop_while_releasing(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        await engine.async_pause(5)
        assert engine.status == "releasing"  # unchanged, not "paused"

    @pytest.mark.asyncio
    async def test_resume_is_noop_while_releasing(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        await engine.async_resume()
        assert engine.status == "releasing"

    @pytest.mark.asyncio
    async def test_repeated_release_restarts_grace_period(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        first_cancel_call = scheduled_calls[0]
        await engine.async_release(2)
        assert first_cancel_call.cancelled is True
        assert len(scheduled_calls) == 2

    @pytest.mark.asyncio
    async def test_grace_expiry_starts_forcing_which_freezes_last_frame_time(self):
        engine, _ = _make_live_engine(channels=1)
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        scheduled_calls[-1].fire()  # grace period elapses, TV hasn't complied
        assert engine._release_forcing is True
        before = engine.last_frame_time
        engine.handle_frame(_make_v2_frame())
        assert engine.last_frame_time == before  # deliberately not advanced

    @pytest.mark.asyncio
    async def test_new_session_while_releasing_resolves_it_with_a_fresh_snapshot(self):
        """Corner case: the TV reconnects mid-grace-period. That reconnect
        itself resolves the release — treated as a normal fresh start, not
        as 'already active, keep the old snapshot'."""
        engine, hass = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        assert engine._saved_states is None

        hass.states.get = MagicMock(return_value=MagicMock(state="off"))
        await engine.async_snapshot_lights()

        assert engine.status == "streaming"
        assert engine._saved_states is not None  # a fresh snapshot was taken
        assert scheduled_calls[0].cancelled is True  # stale grace timer cancelled

    @pytest.mark.asyncio
    async def test_restore_resolves_a_pending_release(self):
        engine, _ = _make_live_engine()
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        await engine.async_restore_lights()
        assert engine.status == "idle"
        assert engine._releasing is False

    @pytest.mark.asyncio
    async def test_release_flushes_a_command_already_queued_before_the_call(self):
        """Regression for the live 2026-08-28 finding: a Living Room bulb,
        swept off by house_lights_off right after release() was called,
        relit ~770ms later while the TV kept streaming — a command a frame
        had already queued a moment before release() survived into the
        drain loop and got sent regardless of the new suppression state."""
        engine, _ = _make_live_engine(channels=1)
        await engine.async_snapshot_lights()
        engine.handle_frame(_make_v2_frame())
        assert engine._mappings[0].dirty is True  # sanity: it really queued

        await engine.async_release(2)

        assert engine._mappings[0].dirty is False
        assert engine._mappings[0].pending_data is None

    @pytest.mark.asyncio
    async def test_release_prevents_the_drain_loop_from_actually_sending_it(self):
        """End-to-end version of the flush test: not just that the mapping
        looks clean, but that the drain loop — running for real on the event
        loop, not mocked away — never issues the stale light.turn_on at all."""
        engine, hass = _make_live_engine(channels=1)
        await engine.async_snapshot_lights()
        engine.handle_frame(_make_v2_frame())  # queues a command; drain task
        # has not had a chance to run yet — nothing has awaited since.

        await engine.async_release(2)
        await asyncio.sleep(0.05)  # let the (already-running) drain loop take a turn

        hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Drain loop hardening — the two gaps found reviewing the abandoned branch
# ---------------------------------------------------------------------------


class TestDrainLoopHardening:
    @pytest.mark.asyncio
    async def test_drain_running_cleared_even_when_the_loop_raises(self):
        """Any exception besides CancelledError escaping the loop must not
        leave _drain_running stuck True forever — without the try/finally,
        nothing would ever clear it, and _ensure_drain_task would then
        refuse to ever start a fresh task for a later command."""
        engine, hass = _make_live_engine(channels=1)
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        # _light_available reads hass.states.get; force it to raise so the
        # exception escapes the loop body itself, not just the guarded
        # service call.
        hass.states.get = MagicMock(side_effect=RuntimeError("boom"))

        engine.handle_light_command(0, {"on": True, "bri": 200})
        task = engine._drain_task
        with pytest.raises(RuntimeError):
            await task

        assert engine._drain_running is False

    @pytest.mark.asyncio
    async def test_ensure_drain_task_guards_on_drain_running_not_task_done(self):
        """A task that is logically finished but hasn't unwound yet must not
        cause a new command to be silently dropped."""
        engine, _ = _make_live_engine()
        fake_task = _FakeTask()  # done() is False until .cancel() is called
        engine._drain_task = fake_task
        engine._drain_running = False  # the loop's finally already ran

        engine._ensure_drain_task()

        assert engine._drain_task is not fake_task  # a new task was started


# ---------------------------------------------------------------------------
# release in classic mode (no DTLS session to tear down)
# ---------------------------------------------------------------------------


class TestReleaseClassicMode:
    @pytest.mark.asyncio
    async def test_release_suppresses_classic_commands_and_shows_underlying_activity(self):
        engine, _ = _make_live_engine(channels=1)
        with patch.object(_ent, "CLASSIC_DRAIN_IDLE", 0.05):
            engine.handle_light_command(0, {"on": True, "bri": 200})
            await asyncio.sleep(0)
            assert engine.status == "classic"
            await engine.async_release(2)
            assert engine.status == "releasing"
            assert engine.status_attributes["underlying_activity"] == "classic"
            engine.handle_light_command(0, {"bri": 10})
            assert engine._mappings[0].dirty is False  # dropped
            await asyncio.sleep(0.2)  # drain loop goes idle and exits
        # Still releasing: the grace period, not the drain loop, bounds a release.
        assert engine.status == "releasing"
        assert engine.status_attributes["underlying_activity"] == "idle"

    @pytest.mark.asyncio
    async def test_grace_expiry_resolves_release_when_no_stream_is_active(self):
        """Without a DTLS session there is nothing for the watchdog to tear
        down, so the grace timer itself must resolve the release — otherwise
        a classic-mode TV would leave the sensor stuck on `releasing`."""
        notify = MagicMock()
        engine, _ = _make_live_engine(channels=1, notify=notify)
        with patch.object(_ent, "CLASSIC_DRAIN_IDLE", 0.05):
            engine.handle_light_command(0, {"on": True})
            await asyncio.sleep(0)
            await engine.async_release(2)
            await asyncio.sleep(0.2)  # drain loop idles out underneath the release
        assert engine.status == "releasing"
        notify.reset_mock()
        scheduled_calls[-1].fire()
        assert engine.status == "idle"
        assert engine._releasing is False
        assert engine._release_forcing is False
        assert notify.called
        # The next classic command drives the lights again
        engine.handle_light_command(0, {"bri": 10})
        assert engine.is_driving_lights is True
        engine._drain_task.cancel()

    @pytest.mark.asyncio
    async def test_grace_expiry_forces_rather_than_resolves_while_streaming(self):
        engine, _ = _make_live_engine(channels=1)
        await engine.async_snapshot_lights()
        await engine.async_release(2)
        scheduled_calls[-1].fire()
        assert engine.status == "releasing"
        assert engine._release_forcing is True
        await engine.async_restore_lights()
        assert engine.status == "idle"
