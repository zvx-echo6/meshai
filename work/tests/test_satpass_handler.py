"""v0.7 satpass_handler tests.

The Central envelope-ingest path (`handle_satpass`,
`consolidate_satpass_pending`, and the observer/norad/elevation/staleness
filtering that lived inside them) was retired with the Central NATS
consumer and deleted 2026-07 when the still-live wire-formatting + gate
code (`format_pass`, `gate_consolidated_pass`) moved to
`meshai.env.satellite.pass_format`. That live code's coverage now lives in
tests/test_satpass_native.py (dedup, wire format, commit callback, via the
native SatpassAdapter path) and tests/test_satpass_broadcast_safety.py
(format_pass formatting rules called directly). What remains here is the
one test that calls `format_pass` directly and doesn't fit either of those
files' focus.
"""

from __future__ import annotations


# ============================================================================
# Budget-fit SAFETY CAP: a pathologically long satellite name must not push
# the broadcast string past 140 chars. Calls format_pass directly (bypasses
# the consolidation path entirely).
# ============================================================================

def test_format_pass_worst_case_fits_140():
    from meshai.env.satellite.pass_format import format_pass
    wire = format_pass(
        sat_name=("NOAA-19 EXPERIMENTAL SUPER LONG SATELLITE DESIGNATION "
                  "PAYLOAD REVISION X PROTOTYPE FLIGHT MODEL SERIAL 00042"),
        max_el=78.0,
        aos_epoch=1719900000, los_epoch=1719900780,
        aos_compass="NNW", los_compass="SSE",
        entry_observer="Treasure Valley Observatory West Ridge Site",
        exit_observer="Magic Valley Observatory East Rim Station",
        broadcast=True,
    )
    assert len(wire) <= 140, f"{len(wire)} chars:\n{wire!r}"
