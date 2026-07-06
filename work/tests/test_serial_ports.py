"""Tests for meshai.serial_ports — USB serial port scanner.

All tests are hermetic: no real /dev access, no real pyserial comports call.
Fake comport objects use types.SimpleNamespace.
"""

import os
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_port(
    device,
    vid=None,
    pid=None,
    description="Test Device",
    hwid="USB",
    serial_number="SN001",
    manufacturer="Acme",
    product="Widget",
):
    return types.SimpleNamespace(
        device=device,
        vid=vid,
        pid=pid,
        description=description,
        hwid=hwid,
        serial_number=serial_number,
        manufacturer=manufacturer,
        product=product,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_module_cache():
    """Ensure meshai.serial_ports is re-imported fresh for each test when
    BY_ID_DIR/BY_PATH_DIR are monkeypatched."""
    yield


# ---------------------------------------------------------------------------
# (a) stable_path resolves to by-id link when present
# ---------------------------------------------------------------------------

def test_stable_path_by_id(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    # Create a fake device file and a by-id symlink pointing to it.
    fake_device = tmp_path / "ttyACM0"
    fake_device.write_text("")

    by_id_dir = tmp_path / "by-id"
    by_id_dir.mkdir()
    link = by_id_dir / "usb-RAK-nRF52840_ABC123-if00"
    link.symlink_to(fake_device)

    monkeypatch.setattr(sp, "BY_ID_DIR", str(by_id_dir))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "by-path-nonexistent"))

    port = _fake_port(str(fake_device), vid=0x239A, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["by_id"] == str(link)
    assert result[0]["stable_path"] == str(link)
    assert result[0]["by_path"] is None


# ---------------------------------------------------------------------------
# (b) falls back to by-path when only by-path matches
# ---------------------------------------------------------------------------

def test_stable_path_by_path_fallback(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    fake_device = tmp_path / "ttyACM0"
    fake_device.write_text("")

    by_path_dir = tmp_path / "by-path"
    by_path_dir.mkdir()
    link = by_path_dir / "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0"
    link.symlink_to(fake_device)

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "by-id-nonexistent"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(by_path_dir))

    port = _fake_port(str(fake_device), vid=0x239A, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["by_id"] is None
    assert result[0]["by_path"] == str(link)
    assert result[0]["stable_path"] == str(link)


# ---------------------------------------------------------------------------
# (c) falls back to raw device when neither dir has a match
# ---------------------------------------------------------------------------

def test_stable_path_raw_device_fallback(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    fake_device = tmp_path / "ttyACM0"
    fake_device.write_text("")

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "by-id-missing"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "by-path-missing"))

    port = _fake_port(str(fake_device), vid=0x239A, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["by_id"] is None
    assert result[0]["by_path"] is None
    assert result[0]["stable_path"] == str(fake_device)


# ---------------------------------------------------------------------------
# (d) likely_radio True for known VID, False for unknown
# ---------------------------------------------------------------------------

def test_likely_radio_known_vid(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    fake_device = tmp_path / "ttyACM0"
    fake_device.write_text("")

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))

    port = _fake_port(str(fake_device), vid=0x239A, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["likely_radio"] is True


def test_likely_radio_unknown_vid(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    fake_device = tmp_path / "ttyACM1"
    fake_device.write_text("")

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))

    port = _fake_port(str(fake_device), vid=0x1234, pid=0x5678)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["likely_radio"] is False


# ---------------------------------------------------------------------------
# (e) ttyS* legacy port is excluded (no vid/pid)
# ---------------------------------------------------------------------------

def test_ttys_legacy_excluded(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))

    legacy = _fake_port("/dev/ttyS0", vid=None, pid=None)
    monkeypatch.setattr(sp, "comports", lambda: [legacy])

    result = sp.list_serial_ports()
    assert result == []


def test_ttys_with_vid_included(tmp_path, monkeypatch):
    """Edge case: a ttyS* port WITH a vid/pid should still be included."""
    import meshai.serial_ports as sp

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))

    # ttyS* + vid → NOT excluded by the legacy rule (the rule only applies when vid is None)
    port = _fake_port("/dev/ttyS0", vid=0x10C4, pid=0xEA60)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1


# ---------------------------------------------------------------------------
# (f) missing by-id / by-path dirs → no raise, stable_path == device
# ---------------------------------------------------------------------------

def test_missing_serial_dirs_no_raise(tmp_path, monkeypatch):
    import meshai.serial_ports as sp

    # Both dirs are nonexistent paths — should not raise.
    monkeypatch.setattr(sp, "BY_ID_DIR", "/does/not/exist/by-id")
    monkeypatch.setattr(sp, "BY_PATH_DIR", "/does/not/exist/by-path")

    port = _fake_port("/dev/ttyACM0", vid=0x1915, pid=0x520F)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["by_id"] is None
    assert result[0]["by_path"] is None
    assert result[0]["stable_path"] == "/dev/ttyACM0"


# ---------------------------------------------------------------------------
# Extra: multiple VIDs from RADIO_VIDS set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vid", [0x239A, 0x1915, 0x10C4, 0x1A86, 0x55D4])
def test_all_radio_vids_flagged(tmp_path, monkeypatch, vid):
    import meshai.serial_ports as sp

    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))

    port = _fake_port("/dev/ttyACM0", vid=vid, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["likely_radio"] is True


# ---------------------------------------------------------------------------
# serial_by_id_available
# ---------------------------------------------------------------------------

def test_serial_by_id_available_true(tmp_path, monkeypatch):
    import meshai.serial_ports as sp
    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path))
    assert sp.serial_by_id_available() is True


def test_serial_by_id_available_false(monkeypatch):
    import meshai.serial_ports as sp
    monkeypatch.setattr(sp, "BY_ID_DIR", "/does/not/exist/by-id")
    assert sp.serial_by_id_available() is False
