"""USB serial port scanner for MeshCore connection setup.

Enumerates available serial ports, resolves stable by-id/by-path symlinks,
and flags likely radio devices by USB VID.
"""

import os
import re

from serial.tools.list_ports import comports

# Module-level dir constants — tests monkeypatch these to tmp_path locations.
BY_ID_DIR = "/dev/serial/by-id"
BY_PATH_DIR = "/dev/serial/by-path"

# USB VIDs for known mesh-radio hardware:
#   0x239A  Adafruit/RAK nRF52840
#   0x1915  Nordic Semiconductor
#   0x10C4  Silicon Labs CP210x (ESP32 bridge)
#   0x1A86  WCH CH340/CH9102
#   0x55D4  WCH CH9102 (alternate VID)
RADIO_VIDS: frozenset[int] = frozenset({0x239A, 0x1915, 0x10C4, 0x1A86, 0x55D4})

# Pattern for ACM/USB tty devices (not legacy ttyS*)
_ACMUSB_RE = re.compile(r"tty(ACM|USB)\d")
# Pattern for legacy ttyS ports to exclude
_TTYS_RE = re.compile(r"ttyS\d")


def serial_by_id_available() -> bool:
    """Return True when /dev/serial/by-id (or the module constant) exists."""
    return os.path.isdir(BY_ID_DIR)


def _build_symlink_map(dirpath: str) -> dict[str, str]:
    """Return a mapping of realpath -> symlink path for all entries in dirpath.

    Returns an empty dict if the directory doesn't exist or can't be read.
    """
    result: dict[str, str] = {}
    try:
        entries = os.listdir(dirpath)
    except OSError:
        return result
    for entry in entries:
        link = os.path.join(dirpath, entry)
        try:
            real = os.path.realpath(link)
            result[real] = link
        except OSError:
            pass
    return result


def list_serial_ports() -> list[dict]:
    """Enumerate and return usable serial ports.

    Includes a port if it has a USB VID/PID OR its device name matches
    /dev/tty(ACM|USB).  Excludes /dev/ttyS* legacy ports (unless they carry
    a vid/pid — edge case handled by checking vid presence first).

    Each entry dict:
        device, by_id, by_path, description, hwid, vid, pid,
        serial_number, manufacturer, product, likely_radio, stable_path

    Never raises — returns [] on unexpected error.
    """
    try:
        return _scan_ports()
    except Exception:
        return []


def _scan_ports() -> list[dict]:
    by_id_map = _build_symlink_map(BY_ID_DIR)
    by_path_map = _build_symlink_map(BY_PATH_DIR)

    ports = []
    for p in comports():
        device: str = p.device or ""
        vid: int | None = p.vid
        pid: int | None = p.pid
        basename = os.path.basename(device)

        # Exclude legacy ttyS* UNLESS it has a VID (unlikely but handled correctly)
        if _TTYS_RE.match(basename) and vid is None:
            continue

        # Include: has VID/PID or looks like ACM/USB tty
        if vid is None and pid is None and not _ACMUSB_RE.match(basename):
            continue

        # Resolve stable path via realpath comparison.
        real_device = os.path.realpath(device) if device else device
        by_id_link: str | None = by_id_map.get(real_device)
        by_path_link: str | None = by_path_map.get(real_device)

        if by_id_link:
            stable_path = by_id_link
        elif by_path_link:
            stable_path = by_path_link
        else:
            stable_path = device

        likely_radio: bool = vid in RADIO_VIDS if vid is not None else False

        ports.append({
            "device": device,
            "by_id": by_id_link,
            "by_path": by_path_link,
            "description": getattr(p, "description", "") or "",
            "hwid": getattr(p, "hwid", "") or "",
            "vid": vid,
            "pid": pid,
            "serial_number": getattr(p, "serial_number", None),
            "manufacturer": getattr(p, "manufacturer", None),
            "product": getattr(p, "product", None),
            "likely_radio": likely_radio,
            "stable_path": stable_path,
        })

    return ports
