"""v0.7 satpass_handler tests."""

import pytest
from unittest.mock import MagicMock, patch


def _envelope(norad_id=25544, sat_name="ISS", observer="Boise",
              max_el=75.0, aos="2026-06-12T03:32:00Z",
              los="2026-06-12T03:38:00Z", direction="NW-SE"):
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
    cfg.norad_ids = []  # empty = all satellites
    with patch("meshai.central.satpass_handler.adapter_config") as mock:
        mock.satpass = cfg
        from meshai.central.satpass_handler import handle_satpass
        if hasattr(handle_satpass, "_disabled_logged"):
            del handle_satpass._disabled_logged
        yield cfg


class TestSatpassHandler:
    """Tests for handle_satpass function."""

    def test_high_elevation_pass_broadcasts(self, mock_db, mock_adapter_config):
        """A pass with high elevation should broadcast."""
        from meshai.central.satpass_handler import handle_satpass

        env = _envelope(max_el=75.0)
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        assert result is not None
        assert "ISS Pass" in result
        assert "75" in result
        assert "Boise" in result

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

    def test_observer_filter_allows_match(self, mock_db, mock_adapter_config):
        """A pass for configured observer should broadcast."""
        from meshai.central.satpass_handler import handle_satpass

        mock_adapter_config.observers = ["Boise", "Magic Valley"]
        env = _envelope(observer="Boise", max_el=45.0)
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        assert result is not None

    def test_norad_id_filter(self, mock_db, mock_adapter_config):
        """NORAD ID filter should block non-matching satellites."""
        from meshai.central.satpass_handler import handle_satpass

        mock_adapter_config.norad_ids = [25544]  # ISS only
        env = _envelope(norad_id=12345, max_el=60.0)
        result = handle_satpass(env, "central.sat.pass.other", data={}, now=1718163120)

        assert result is None

    def test_dedup_blocks_second_broadcast(self, mock_db, mock_adapter_config):
        """Second pass in same hour bucket should be deduplicated."""
        from meshai.central.satpass_handler import handle_satpass

        # First call returns no existing broadcast
        mock_db.execute.return_value.fetchone.return_value = None

        env = _envelope(max_el=60.0)
        result1 = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)
        assert result1 is not None

        # Second call simulates existing broadcast
        mock_db.execute.return_value.fetchone.return_value = {"last_broadcast_at": 1718163120}

        result2 = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163180)
        assert result2 is None

    def test_wire_format(self, mock_db, mock_adapter_config):
        """Wire format should have 3 lines with correct info."""
        from meshai.central.satpass_handler import handle_satpass

        env = _envelope(sat_name="ISS", max_el=75, observer="Boise", direction="NW-SE")
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=1718163120)

        lines = result.split("\n")
        assert len(lines) == 3
        assert "ISS Pass" in lines[0]
        assert "75" in lines[0]
        assert "AOS" in lines[1]
        assert "LOS" in lines[1]
        assert "Boise" in lines[2]

    def test_commit_callback_attached(self, mock_db, mock_adapter_config):
        """Broadcast should attach commit callback."""
        from meshai.central.satpass_handler import handle_satpass

        data = {}
        env = _envelope(max_el=60.0)
        result = handle_satpass(env, "central.sat.pass.iss", data=data, now=1718163120)

        assert result is not None
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
