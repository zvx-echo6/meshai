"""v0.7 satpass_handler tests."""

import pytest
from unittest.mock import MagicMock, patch


def _envelope(norad_id=25544, sat_name="ISS", observer="Boise",
              max_el=75.0, aos="2026-06-12T03:32:00Z",
              los="2026-06-12T03:38:00Z", direction="NW-SE",
              aos_compass="SW", los_compass="NE"):
    """Build a CloudEvents envelope for a satellite pass."""
    return {
        "specversion": "1.0",
        "type": "central.sat.pass",
        "source": "central",
        "id": f"pass-{norad_id}-{aos}",
        "data": {
            "adapter": "n2yo_visualpasses",
            "category": "pass.n2yo_visualpasses",
            "severity": 0,
            "data": {
                "norad_id": norad_id,
                "satellite_name": sat_name,
                "observer_name": observer,
                "max_elevation_deg": max_el,
                "aos_time": aos,
                "los_time": los,
                "azimuth_at_peak_compass": direction,
                "azimuth_at_aos_compass": aos_compass,
                "azimuth_at_los_compass": los_compass,
            }
        }
    }


@pytest.fixture
def mock_db():
    """Mock database connection."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    conn.execute.return_value.lastrowid = 1
    with patch("meshai.central.satpass_handler.get_db", return_value=conn):
        yield conn


@pytest.fixture
def mock_adapter_config():
    """Mock adapter_config.satpass."""
    cfg = MagicMock()
    cfg.enabled = True
    cfg.observers = []  # empty = all observers
    cfg.min_elevation = 30
    cfg.norad_ids = [25544]  # must be non-empty for opt-in
    cfg.dry_run = False
    cfg.max_broadcasts_per_hour = 4
    cfg.max_aos_horizon_hours = 24
    with patch("meshai.central.satpass_handler.adapter_config") as mock:
        mock.satpass = cfg
        from meshai.central.satpass_handler import handle_satpass
        if hasattr(handle_satpass, "_disabled_logged"):
            del handle_satpass._disabled_logged
        if hasattr(handle_satpass, "_no_norad_ids_logged"):
            del handle_satpass._no_norad_ids_logged
        yield cfg


def _ingest_and_consolidate(env, subject, *, now, data=None):
    """Drive the two-call async satpass contract (ingest -> consolidate).

    handle_satpass() ingests the pass into satpass_pending and ALWAYS
    returns None; the consumer then runs consolidate_satpass_pending(),
    which returns the (wire, data) to broadcast (or None if suppressed).
    Returns the consolidation result so a test can assert on the real wire.
    """
    from meshai.central.satpass_handler import (
        handle_satpass, consolidate_satpass_pending,
        drain_pending_consolidation_ids)
    drain_pending_consolidation_ids()
    assert handle_satpass(
        env, subject, data=data if data is not None else {}, now=now) is None
    for cid in drain_pending_consolidation_ids():
        res = consolidate_satpass_pending(cid)
        if res is not None:
            return res
    return None


class TestSatpassHandler:
    """Tests for handle_satpass function."""

    def test_high_elevation_pass_broadcasts(self, mock_adapter_config):
        """A pass with high elevation should broadcast (via consolidation)."""
        env = _envelope(max_el=75.0)
        result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                         now=1781235000)
        assert result is not None
        wire, _ = result
        assert "ISS" in wire

    def test_low_elevation_pass_filtered(self, mock_db, mock_adapter_config):
        """A pass below min_elevation should be filtered."""
        from meshai.central.satpass_handler import handle_satpass

        mock_adapter_config.min_elevation = 30
        env = _envelope(max_el=25.0)
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        assert result is None

    def test_observer_filter_blocks_mismatch(self, mock_db, mock_adapter_config):
        """A pass for non-configured observer should be filtered."""
        from meshai.central.satpass_handler import handle_satpass

        mock_adapter_config.observers = ["Magic Valley"]
        env = _envelope(observer="Boise")
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        assert result is None

    def test_observer_filter_allows_match(self, mock_adapter_config):
        """A pass for configured observer should broadcast."""
        mock_adapter_config.observers = ["Boise", "Magic Valley"]
        env = _envelope(observer="Boise", max_el=45.0)
        result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                         now=1781235000)
        assert result is not None

    def test_norad_id_filter(self, mock_db, mock_adapter_config):
        """NORAD ID filter should block non-matching satellites."""
        from meshai.central.satpass_handler import handle_satpass

        mock_adapter_config.norad_ids = [25544]  # ISS only
        env = _envelope(norad_id=12345, max_el=60.0)
        result = handle_satpass(env, "central.sat.pass.other", data={}, now=1718163120)

        assert result is None

    def test_dedup_blocks_second_broadcast(self, mock_adapter_config):
        """Second pass in same hour bucket should be deduplicated."""
        env = _envelope(max_el=60.0)

        # First pass consolidates into a real broadcast.
        result1 = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                          now=1781235000)
        assert result1 is not None

        # Simulate the broadcast committing (marks satpass_events broadcast).
        _wire, data1 = result1
        data1["_on_broadcast_committed"](1781235010)

        # Second pass in the same (norad, aos-hour) bucket must be deduped.
        result2 = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                          now=1781235060)
        assert result2 is None

    def test_wire_format(self, mock_adapter_config):
        """Wire format should have 2 lines with correct info."""
        env = _envelope(sat_name="ISS", max_el=75, observer="Boise",
                        direction="S", aos_compass="SW", los_compass="NE")
        result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                         now=1781235000)
        assert result is not None
        wire, _ = result

        lines = wire.split("\n")
        assert len(lines) == 2
        assert "ISS" in lines[0]
        assert "overhead" in lines[0]
        assert "SW" in lines[0]        # aos_compass
        assert "S" in lines[0]         # peak_compass (new)
        assert "NE" in lines[0]        # los_compass
        assert "min window" in lines[1]

    def test_commit_callback_attached(self, mock_adapter_config):
        """Broadcast should attach commit callback (on the consolidation data)."""
        env = _envelope(max_el=60.0)
        result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                         now=1781235000)
        assert result is not None
        _wire, data = result
        assert "_on_broadcast_committed" in data
        assert "_broadcast_audit" in data
        assert data["_broadcast_audit"]["table"] == "satpass_events"

    def test_wrong_adapter_ignored(self, mock_db, mock_adapter_config):
        """Envelope with wrong adapter should be ignored."""
        from meshai.central.satpass_handler import handle_satpass

        env = _envelope()
        env["data"]["adapter"] = "some_other_adapter"
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        assert result is None


# ============================================================================
# Budget-fit SAFETY CAP: a pathologically long satellite name must not push
# the broadcast string past 140 chars. Calls format_pass directly (bypasses
# the broken consolidation path).
# ============================================================================

def test_format_pass_worst_case_fits_140():
    from meshai.central.satpass_handler import format_pass
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
