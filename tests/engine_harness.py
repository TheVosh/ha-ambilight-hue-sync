"""Hermetic loader for ``entertainment.py`` shared by the plain-pytest engine tests.

``test_entertainment.py`` and ``test_pause_release.py`` exercise the
EntertainmentEngine without a running Home Assistant. This module loads
``const.py`` and ``entertainment.py`` once, under the ``hue_entertainment.*``
names, in a way that works in both environments the suite runs in:

* the nix shell (no Home Assistant installed) — the handful of
  ``homeassistant.*`` modules entertainment.py imports are stubbed;
* the HA venv / CI (Home Assistant installed) — the real modules are used.

Either way **nothing here mutates a real ``homeassistant`` module**. The fakes
for ``async_call_later`` and ``async_reproduce_state`` are set as attributes on
the *loaded entertainment module* (``_ent``), which is where the engine looks
them up. Patching ``sys.modules["homeassistant.core"].callback`` etc. instead
(what the tests used to do) silently rewrote Home Assistant itself for every
test collected afterwards — ``@callback``-decorated entity methods lost their
marker, the dispatcher ran them in the executor, and the HA-harness tests in
``test_integration.py`` failed only when run as part of the full suite.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_BASE = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"


def _stub_homeassistant_if_missing() -> None:
    try:
        import homeassistant.core  # noqa: F401
        import homeassistant.helpers.event  # noqa: F401
        import homeassistant.helpers.state  # noqa: F401
    except ImportError:
        pass
    else:
        return  # real Home Assistant available — leave it untouched
    for name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.event",
        "homeassistant.helpers.state",
    ):
        sys.modules.setdefault(name, MagicMock())
    # `callback` is a decorator that must return the function unchanged — a
    # bare MagicMock would replace every decorated method with a mock.
    sys.modules["homeassistant.core"].callback = lambda f: f


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _BASE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_stub_homeassistant_if_missing()

# Stub package so the relative imports in entertainment.py resolve without
# executing the integration's __init__.py (which needs a full HA).
if "hue_entertainment" not in sys.modules:
    _pkg_stub = MagicMock()
    _pkg_stub.__path__ = [str(_BASE)]
    _pkg_stub.__package__ = "hue_entertainment"
    sys.modules["hue_entertainment"] = _pkg_stub

const = _load("hue_entertainment.const", "const.py")
ent = _load("hue_entertainment.entertainment", "entertainment.py")


# ---------------------------------------------------------------------------
# Fakes installed on the loaded module (not on homeassistant.*)
# ---------------------------------------------------------------------------


class FakeCallLater:
    """One scheduled call recorded by the fake ``async_call_later``."""

    def __init__(self, delay, action):
        self.delay = delay
        self.action = action
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        """Invoke the scheduled action as if its delay had elapsed."""
        return self.action(None)


scheduled_calls: list[FakeCallLater] = []


def fake_async_call_later(hass, delay, action):
    """Record (delay, action) instead of scheduling real time; tests fire the
    recorded action directly for deterministic pause/release timers."""
    call = FakeCallLater(delay, action)
    scheduled_calls.append(call)
    return call.cancel


ent.async_call_later = fake_async_call_later
ent.async_reproduce_state = AsyncMock()

EntertainmentEngine = ent.EntertainmentEngine
LightMapping = ent.LightMapping


# ---------------------------------------------------------------------------
# Frame builders shared by both test files
# ---------------------------------------------------------------------------

UUID_PLACEHOLDER = b"a" * 36


def v2_header(color_space: int = const.COLOR_SPACE_RGB, api_version: int = 0x02) -> bytes:
    """Build a 52-byte v2 HueStream header."""
    return (
        const.HUESTREAM_HEADER  # 9 bytes "HueStream"
        + bytes([api_version, 0x00])  # version major/minor
        + bytes([0x00])  # sequence
        + b"\x00\x00"  # reserved
        + bytes([color_space])
        + b"\x00"  # reserved
        + UUID_PLACEHOLDER
    )


def v2_channel(channel_id: int, r: int, g: int, b: int) -> bytes:
    return bytes([channel_id]) + struct.pack(">HHH", r, g, b)
