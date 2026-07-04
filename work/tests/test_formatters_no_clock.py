"""Phase-0 purity guard: formatter modules must not call time directly.

Every file under meshai/notifications/formatters/ must route all time reads
through meshai.notifications.clock (the determinism seam).  Direct calls to
datetime.now or time.time( inside a formatter break the monkeypatch contract.

This test is intentionally static (source-text check) so it catches new
formatters that forget the contract even before any test calls them.
"""

import glob
import os


def _formatter_sources():
    """Return (path, source) pairs for all .py files in the formatters package."""
    pattern = os.path.join(
        os.path.dirname(__file__),
        "..", "meshai", "notifications", "formatters", "*.py",
    )
    paths = sorted(glob.glob(pattern))
    return [(p, open(p).read()) for p in paths]


def test_no_datetime_now_in_formatters():
    """No formatter file may call datetime.now directly."""
    violations = []
    for path, src in _formatter_sources():
        if "datetime.now" in src:
            violations.append(os.path.basename(path))
    assert not violations, (
        "The following formatter files call datetime.now directly "
        "(use clock.now_dt() instead):\n" + "\n".join(violations)
    )


def test_no_time_time_in_formatters():
    """No formatter file may call time.time( directly."""
    violations = []
    for path, src in _formatter_sources():
        if "time.time(" in src:
            violations.append(os.path.basename(path))
    assert not violations, (
        "The following formatter files call time.time( directly "
        "(use clock.now() instead):\n" + "\n".join(violations)
    )
