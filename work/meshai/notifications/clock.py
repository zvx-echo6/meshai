"""Determinism seam for time reads across the notification pipeline.

All handlers and formatters should call clock.now() / clock.now_dt() instead
of time.time() / datetime.now() directly.  A single monkeypatch point makes
golden-file tests and time-freeze fixtures trivial:

    monkeypatch.setattr("meshai.notifications.clock.now", lambda: 1_700_000_000.0)

clock.now() is semantically identical to time.time() at runtime.
"""

import time
from datetime import datetime


def now() -> float:
    """Return the current Unix timestamp (float). Monkeypatchable seam."""
    return time.time()


def now_dt(tz=None):
    """Return the current datetime (optionally timezone-aware). Monkeypatchable seam."""
    return datetime.now(tz)
