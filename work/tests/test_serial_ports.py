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


# ---------------------------------------------------------------------------
# /dev supplementary scan — detects nodes comports() misses
# ---------------------------------------------------------------------------

def _major_by_basename(mapping):
    """Return a _char_major replacement that looks up majors by basename.

    ``mapping`` maps a basename -> major (int). Unlisted paths return None
    (treated as "not a USB-serial char device").
    """
    def _fake(path):
        return mapping.get(os.path.basename(path))
    return _fake


def _setup_dev_scan(sp, tmp_path, monkeypatch, filenames, majors, comports=()):
    """Create a fake /dev dir with ``filenames`` and wire up the module.

    Points DEV_DIR at the tmp dir, disables the by-id/by-path dirs, fakes
    _char_major from ``majors`` (basename -> major), and sets comports().
    Returns the tmp dev dir path.
    """
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in filenames:
        (dev / name).write_text("")  # stand-in for a device node

    monkeypatch.setattr(sp, "DEV_DIR", str(dev))
    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "by-id-none"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "by-path-none"))
    monkeypatch.setattr(sp, "_char_major", _major_by_basename(majors))
    monkeypatch.setattr(sp, "comports", lambda: list(comports))
    return dev


def test_dev_scan_custom_node_meshcore_rak(tmp_path, monkeypatch):
    """A passed-through custom node /dev/meshcore-rak (major 166) with no /sys
    backing (comports() returns []) is detected with likely_radio=True and its
    own name as the stable path."""
    import meshai.serial_ports as sp

    dev = _setup_dev_scan(
        sp, tmp_path, monkeypatch,
        filenames=["meshcore-rak"],
        majors={"meshcore-rak": 166},
        comports=[],
    )

    result = sp.list_serial_ports()
    assert len(result) == 1
    entry = result[0]
    assert entry["device"] == str(dev / "meshcore-rak")
    assert entry["stable_path"] == str(dev / "meshcore-rak")
    assert entry["likely_radio"] is True
    assert entry["vid"] is None and entry["pid"] is None
    assert entry["serial_number"] is None


def test_dev_scan_raw_ttyacm(tmp_path, monkeypatch):
    """A raw ttyACM0 (major 166) with no by-id link is included; its basename
    doesn't match the radio-name heuristic so likely_radio is False and the
    stable path is the raw device."""
    import meshai.serial_ports as sp

    dev = _setup_dev_scan(
        sp, tmp_path, monkeypatch,
        filenames=["ttyACM0"],
        majors={"ttyACM0": 166},
        comports=[],
    )

    result = sp.list_serial_ports()
    assert len(result) == 1
    entry = result[0]
    assert entry["device"] == str(dev / "ttyACM0")
    assert entry["stable_path"] == str(dev / "ttyACM0")
    assert entry["likely_radio"] is False


def test_dev_scan_ttyusb_included(tmp_path, monkeypatch):
    """ttyUSB0 (major 188) is a USB-serial major and is included."""
    import meshai.serial_ports as sp

    dev = _setup_dev_scan(
        sp, tmp_path, monkeypatch,
        filenames=["ttyUSB0"],
        majors={"ttyUSB0": 188},
        comports=[],
    )

    result = sp.list_serial_ports()
    assert len(result) == 1
    assert result[0]["stable_path"] == str(dev / "ttyUSB0")


def test_dev_scan_ttys_excluded(tmp_path, monkeypatch):
    """ttyS0 (major 4, legacy UART) is NOT a USB-serial major → excluded."""
    import meshai.serial_ports as sp

    _setup_dev_scan(
        sp, tmp_path, monkeypatch,
        filenames=["ttyS0"],
        majors={"ttyS0": 4},
        comports=[],
    )

    assert sp.list_serial_ports() == []


def test_dev_scan_dedup_keeps_pyserial_metadata(tmp_path, monkeypatch):
    """A device found by BOTH comports() and the /dev scan yields one entry
    that keeps the pyserial vid/pid/manufacturer metadata."""
    import meshai.serial_ports as sp

    dev = _setup_dev_scan(
        sp, tmp_path, monkeypatch,
        filenames=["ttyACM0"],
        majors={"ttyACM0": 166},
        comports=[],
    )
    device_path = str(dev / "ttyACM0")

    # comports() reports the same node with rich metadata.
    port = _fake_port(device_path, vid=0x239A, pid=0x0001)
    monkeypatch.setattr(sp, "comports", lambda: [port])

    result = sp.list_serial_ports()
    assert len(result) == 1
    entry = result[0]
    assert entry["vid"] == 0x239A
    assert entry["pid"] == 0x0001
    assert entry["manufacturer"] == "Acme"
    assert entry["serial_number"] == "SN001"
    assert entry["likely_radio"] is True


def test_dev_scan_custom_symlink_stable_path(tmp_path, monkeypatch):
    """A custom udev symlink /dev/meshcore-rak -> ttyACM0 (both in /dev) groups
    to one node; the custom name is preferred as the stable path over the raw
    ttyACM0, and the radio heuristic fires on the custom name."""
    import meshai.serial_ports as sp

    dev = tmp_path / "dev"
    dev.mkdir()
    raw = dev / "ttyACM0"
    raw.write_text("")
    link = dev / "meshcore-rak"
    link.symlink_to(raw)

    monkeypatch.setattr(sp, "DEV_DIR", str(dev))
    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "by-id-none"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "by-path-none"))
    monkeypatch.setattr(sp, "_char_major", _major_by_basename({"ttyACM0": 166}))
    monkeypatch.setattr(sp, "comports", lambda: [])

    result = sp.list_serial_ports()
    assert len(result) == 1
    entry = result[0]
    # realpath collapses to the raw node; stable_path prefers the custom name.
    assert entry["device"] == str(raw)
    assert entry["stable_path"] == str(link)
    assert entry["likely_radio"] is True


def test_dev_scan_unreadable_dir_no_raise(tmp_path, monkeypatch):
    """A nonexistent DEV_DIR must not raise — just yields nothing extra."""
    import meshai.serial_ports as sp

    monkeypatch.setattr(sp, "DEV_DIR", "/does/not/exist/dev")
    monkeypatch.setattr(sp, "BY_ID_DIR", str(tmp_path / "noid"))
    monkeypatch.setattr(sp, "BY_PATH_DIR", str(tmp_path / "nopath"))
    monkeypatch.setattr(sp, "comports", lambda: [])

    assert sp.list_serial_ports() == []
