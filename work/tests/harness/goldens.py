"""Phase-0b golden + gate-sequence test helpers.

pinned_time(epoch)
    Context manager: monkeypatches meshai.notifications.clock.now / now_dt to
    a fixed epoch so golden files are byte-stable.  SCOPED — does not touch
    the process TZ.  Restores the originals on exit (exception-safe).

pinned_tz(name)
    Separate opt-in context manager: sets os.environ['TZ'] and calls
    time.tzset(), restores on exit.  Only golden tests that format %Z need
    this; enabling it globally would break naive-datetime tests.

render_golden(handler_render_fn, envelope, *, at)
    Run handler_render_fn(envelope) under pinned_time(at) and return the wire
    string.  Used to CAPTURE the baseline golden (not to assert — compare
    against assert_byte_identical later).

assert_byte_identical(new, golden)
    Compare two strings by their UTF-8 byte sequences (len + content).  On
    mismatch emit a readable unified diff and raise AssertionError.

load_fixtures(hazard)
    Read every tests/fixtures/<hazard>/*.json file (sorted) and return a list
    of dicts, each with keys {envelope, subject, captured_epoch}.

run_gate_sequence(old_handler, new_decider, ordered_fixtures, *, timeline)
    Stub comparator: exercise both the old and new gating path across an
    explicit now timeline and return a diff report.

    old_handler  : callable(fixture: dict, *, now: float) -> bool
                   True = old path would broadcast, False = suppress.
    new_decider  : callable(fixture: dict, *, now: float) -> GateResult | None
                   May return None (no decision) → treated as suppress.
    ordered_fixtures : list[dict]  — fixtures in replay order
    timeline         : list[float] — one epoch per fixture (len must match)

    Returns list[dict] with keys:
        fixture_n   : int  — 0-based index into ordered_fixtures
        now         : float
        old_broadcast : bool
        new_broadcast : bool
        match         : bool  — True when both paths agree
        diffs         : dict  — empty when match; keys describe divergence
"""
from __future__ import annotations

import difflib
import json
import os
import pathlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# pinned_time — deterministic clock seam for golden tests
# ---------------------------------------------------------------------------

@contextmanager
def pinned_time(epoch: float):
    """Monkeypatch meshai.notifications.clock.now / now_dt to *epoch*.

    Restores the originals on exit even if the body raises.
    Does NOT touch TZ — use pinned_tz for %Z-sensitive golden tests.

    Usage::

        with pinned_time(1_700_000_000.0):
            result = some_handler(envelope)
        # result is deterministic for golden comparison
    """
    import meshai.notifications.clock as _clock_mod
    _orig_now = _clock_mod.now
    _orig_now_dt = _clock_mod.now_dt
    _clock_mod.now = lambda: float(epoch)
    _clock_mod.now_dt = lambda tz=None: datetime.fromtimestamp(
        epoch, tz=(tz if tz is not None else timezone.utc)
    )
    try:
        yield epoch
    finally:
        _clock_mod.now = _orig_now
        _clock_mod.now_dt = _orig_now_dt


# ---------------------------------------------------------------------------
# pinned_tz — opt-in TZ override (only for %Z-sensitive golden tests)
# ---------------------------------------------------------------------------

@contextmanager
def pinned_tz(name: str = "America/Boise"):
    """Set TZ=*name*, call time.tzset(), restore on exit.

    Opt-in only: enabling TZ globally breaks existing tests that format
    naive datetimes or rely on UTC.  Golden tests that include a %Z token
    in their expected output should wrap only that call in pinned_tz.

    Usage::

        with pinned_tz("America/Boise"):
            with pinned_time(1_700_000_000.0):
                result = handler(envelope)
    """
    orig = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield name
    finally:
        if orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig
        time.tzset()


# ---------------------------------------------------------------------------
# render_golden — capture a wire string under frozen time
# ---------------------------------------------------------------------------

def render_golden(
    handler_render_fn: Callable,
    envelope: dict,
    *,
    at: float,
) -> str:
    """Return the wire string from handler_render_fn(envelope) with time frozen.

    handler_render_fn : callable(envelope: dict) -> str
        Typically a lambda wrapper around a handler function, e.g.::

            render_golden(
                lambda env: handle_nws(env, subject, data={}),
                envelope,
                at=1_700_000_000.0,
            )

    The result is used as the *golden* baseline.  Store it (e.g. in a .txt
    fixture) and compare future renders with assert_byte_identical.
    """
    with pinned_time(at):
        return handler_render_fn(envelope)


# ---------------------------------------------------------------------------
# assert_byte_identical — byte-level golden comparison
# ---------------------------------------------------------------------------

def assert_byte_identical(new: str, golden: str) -> None:
    """Assert that *new* and *golden* are identical as UTF-8 byte sequences.

    Checks both len(x.encode('utf-8')) and content.  On mismatch emits a
    unified diff and raises AssertionError with a readable message.
    """
    new_bytes = new.encode("utf-8")
    golden_bytes = golden.encode("utf-8")
    if new_bytes == golden_bytes:
        return
    # Build a readable unified diff (character-level, single-line strings).
    diff_lines = list(
        difflib.unified_diff(
            [golden],
            [new],
            fromfile="golden",
            tofile="new",
            lineterm="",
        )
    )
    diff_str = "\n".join(diff_lines) if diff_lines else "<no text diff — byte sequences differ>"
    raise AssertionError(
        f"assert_byte_identical failed:\n"
        f"  golden: {len(golden_bytes)} bytes\n"
        f"  new:    {len(new_bytes)} bytes\n"
        f"{diff_str}"
    )


# ---------------------------------------------------------------------------
# load_fixtures — read fixture JSON files for a hazard category
# ---------------------------------------------------------------------------

def load_fixtures(hazard: str) -> list[dict]:
    """Read all tests/fixtures/<hazard>/*.json files (sorted) and return as list.

    Each JSON file is expected to contain::

        {
            "envelope":       {...},   # raw Central CloudEvents envelope
            "subject":        "...",   # NATS subject
            "captured_epoch": 1234567  # wall-clock at capture time
        }

    Returns an empty list if the directory does not exist (no fixtures yet).
    """
    fixtures_dir = (
        pathlib.Path(__file__).parent.parent / "fixtures" / hazard
    )
    if not fixtures_dir.is_dir():
        return []
    result = []
    for p in sorted(fixtures_dir.glob("*.json")):
        with open(p, encoding="utf-8") as fh:
            result.append(json.load(fh))
    return result


# ---------------------------------------------------------------------------
# run_gate_sequence — old-vs-new broadcast decision comparator
# ---------------------------------------------------------------------------

def run_gate_sequence(
    old_handler: Callable,
    new_decider: Callable,
    ordered_fixtures: list,
    *,
    timeline: list,
) -> list[dict]:
    """Compare broadcast/suppress decisions of old_handler vs new_decider.

    Parameters
    ----------
    old_handler:
        callable(fixture: dict, *, now: float) -> bool
        True  = old path would broadcast
        False = old path would suppress
    new_decider:
        callable(fixture: dict, *, now: float) -> GateResult | None
        None → treated as suppress (False).
        The returned GateResult.commit is NEVER called here.
    ordered_fixtures:
        list of fixture dicts in replay order.
    timeline:
        list[float] — one epoch per fixture.  Must be same length as
        ordered_fixtures; ValueError raised otherwise.

    Returns
    -------
    list[dict] — one entry per fixture:
        fixture_n     : int   — 0-based index
        now           : float — epoch used for this step
        old_broadcast : bool
        new_broadcast : bool
        match         : bool  — True when old_broadcast == new_broadcast
        diffs         : dict  — empty when match; populated fields on divergence:
            "broadcast": {"old": bool, "new": bool}
    """
    if len(ordered_fixtures) != len(timeline):
        raise ValueError(
            f"run_gate_sequence: ordered_fixtures has {len(ordered_fixtures)} entries "
            f"but timeline has {len(timeline)} — they must match."
        )

    results = []
    for i, (fixture, now_ts) in enumerate(zip(ordered_fixtures, timeline)):
        # --- old path ---
        old_broadcast = bool(old_handler(fixture, now=now_ts))

        # --- new path (never commit) ---
        new_result = None
        try:
            new_result = new_decider(fixture, now=now_ts)
        except Exception:
            pass  # treat a crashing decider as suppress
        new_broadcast = (new_result.broadcast if new_result is not None else False)

        # --- diff ---
        diffs: dict = {}
        if old_broadcast != new_broadcast:
            diffs["broadcast"] = {"old": old_broadcast, "new": new_broadcast}

        results.append(
            {
                "fixture_n": i,
                "now": now_ts,
                "old_broadcast": old_broadcast,
                "new_broadcast": new_broadcast,
                "match": not diffs,
                "diffs": diffs,
            }
        )

    return results
