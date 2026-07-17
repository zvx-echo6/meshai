"""Phase-0: verify the clock seam is monkeypatchable and handlers use it.

Tests:
  1. clock.now() returns a float close to time.time() at runtime.
  2. Monkeypatching clock.now propagates into wfigs_handler._now.
"""

import time

import pytest

import meshai.notifications.clock as clock_mod
import meshai.env.fire_render as wfigs_handler


_FROZEN_TS = 1_700_000_000.0


def test_clock_now_returns_float_close_to_time():
    before = time.time()
    result = clock_mod.now()
    after = time.time()
    assert isinstance(result, float), "clock.now() must return float"
    assert before <= result <= after + 0.1, "clock.now() must be current time"


def test_clock_now_is_monkeypatchable(monkeypatch):
    monkeypatch.setattr(clock_mod, "now", lambda: _FROZEN_TS)
    assert clock_mod.now() == _FROZEN_TS


def test_wfigs_handler_now_uses_clock_seam(monkeypatch):
    """wfigs_handler._now() must reflect a monkeypatched clock.now."""
    monkeypatch.setattr(clock_mod, "now", lambda: _FROZEN_TS)
    result = wfigs_handler._now()
    assert result == int(_FROZEN_TS), (
        f"wfigs_handler._now() returned {result!r}, expected {int(_FROZEN_TS)}"
    )
