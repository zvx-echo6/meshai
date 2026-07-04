"""Phase-0b test harness — golden + gate-sequence helpers.

Import surface:

    from tests.harness import (
        pinned_time,
        pinned_tz,
        render_golden,
        assert_byte_identical,
        load_fixtures,
        run_gate_sequence,
    )

All helpers are pure-Python, stdlib-only, and safe to import at collection time.
"""
from tests.harness.goldens import (
    assert_byte_identical,
    load_fixtures,
    pinned_time,
    pinned_tz,
    render_golden,
    run_gate_sequence,
)

__all__ = [
    "assert_byte_identical",
    "load_fixtures",
    "pinned_time",
    "pinned_tz",
    "render_golden",
    "run_gate_sequence",
]
