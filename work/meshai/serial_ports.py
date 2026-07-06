"""USB serial port scanner for MeshCore connection setup.

Enumerates available serial ports, resolves stable by-id/by-path symlinks,
and flags likely radio devices by USB VID.

In addition to pyserial's ``comports()`` (which reads ``/sys`` USB metadata and
therefore misses bind-mounted device nodes inside containers), a supplementary
direct ``/dev`` scan finds USB-serial character devices by their device major.
This catches passed-through nodes (e.g. ``/dev/meshcore-rak``, major 166) and
custom udev symlinks that ``comports()`` returns nothing for.
"""

import os
import re
import stat as _stat

from serial.tools.list_ports import comports

# Module-level dir constants — tests monkeypatch these to tmp_path locations.
BY_ID_DIR = "/dev/serial/by-id"
BY_PATH_DIR = "/dev/serial/by-path"
DEV_DIR = "/dev"

# USB VIDs for known mesh-radio hardware:
#   0x239A  Adafruit/RAK nRF52840
#   0x1915  Nordic Semiconductor
#   0x10C4  Silicon Labs CP210x (ESP32 bridge)
#   0x1A86  WCH CH340/CH9102
#   0x55D4  WCH CH9102 (alternate VID)
RADIO_VIDS: frozenset[int] = frozenset({0x239A, 0x1915, 0x10C4, 0x1A86, 0x55D4})

# USB-serial character-device majors (Linux):
#   166  ttyACM* / USB CDC-ACM (RAK nRF52840, native-USB radios)
#   188  ttyUSB* / USB serial (CP210x, CH340, FTDI bridges)
# Legacy ttyS* (major 4, 16550 UART) is deliberately NOT here.
USB_SERIAL_MAJORS: frozenset[int] = frozenset({166, 188})

# Pattern for ACM/USB tty devices (not legacy ttyS*)
_ACMUSB_RE = re.compile(r"tty(ACM|USB)\d")
# Anchored pattern for a RAW ACM/USB name (ttyACM0, ttyUSB1). A device name that
# does NOT match this (e.g. "meshcore-rak") is treated as a stable custom name.
_RAW_ACMUSB_RE = re.compile(r"tty(ACM|USB)\d+$")
# Pattern for legacy ttyS ports to exclude
_TTYS_RE = re.compile(r"ttyS\d")
# Heuristic: basename words that suggest a mesh radio (used for bare /dev nodes
# that carry no USB VID/PID metadata).
_RADIO_NAME_RE = re.compile(
    r"mesh|meshcore|rak|lora|tbeam|t-?beam|heltec|nrf|companion",
    re.IGNORECASE,
)


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


def _char_major(path: str) -> int | None:
    """Return the device major of ``path`` if it is a character device.

    Follows symlinks. Returns None when the path can't be stat'd or is not a
    character device. Isolated in its own helper so tests can monkeypatch the
    stat/major lookup without needing real device nodes.
    """
    try:
        st = os.stat(path)  # follows symlinks
    except OSError:
        return None
    if not _stat.S_ISCHR(st.st_mode):
        return None
    return os.major(st.st_rdev)


def list_serial_ports() -> list[dict]:
    """Enumerate and return usable serial ports.

    Combines two sources:
      * pyserial ``comports()`` — rich vid/pid/serial metadata on real hosts.
      * a direct ``/dev`` scan — finds USB-serial character devices by major
        (166 ttyACM / 188 ttyUSB), catching container-passed-through nodes and
        custom udev names that ``comports()`` misses.

    Results are merged and deduped by the real device path; a device found by
    both keeps the pyserial metadata.

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

    ports: list[dict] = []
    seen: set[str] = set()

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
        seen.add(real_device)
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

    # Supplement with a direct /dev scan for nodes comports() couldn't see.
    ports.extend(_scan_dev_ports(seen, by_id_map, by_path_map))

    return ports


def _gather_dev_entries() -> list[str]:
    """Return candidate paths to examine: /dev entries (non-recursive) plus the
    two /dev/serial/ subdirs when present. Never raises."""
    paths: list[str] = []
    for dirpath in (DEV_DIR, BY_ID_DIR, BY_PATH_DIR):
        try:
            names = os.listdir(dirpath)
        except OSError:
            continue
        for name in names:
            paths.append(os.path.join(dirpath, name))
    return paths


def _scan_dev_ports(
    seen: set[str],
    by_id_map: dict[str, str],
    by_path_map: dict[str, str],
) -> list[dict]:
    """Scan /dev for USB-serial character devices missed by comports().

    ``seen`` holds realpaths already emitted by the comports() pass; devices
    resolving to one of those are skipped (deduped, pyserial metadata wins).
    """
    dev_dir_norm = os.path.normpath(DEV_DIR)

    # Group candidate paths by the real device node they resolve to.
    groups: dict[str, set[str]] = {}
    for path in _gather_dev_entries():
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        groups.setdefault(real, set()).add(path)

    ports: list[dict] = []
    for real, sources in groups.items():
        if real in seen:
            continue  # already found via comports() — keep its rich metadata

        major = _char_major(real)
        if major not in USB_SERIAL_MAJORS:
            continue

        by_id_link = by_id_map.get(real)
        by_path_link = by_path_map.get(real)

        # A direct /dev entry whose basename is NOT a raw ttyACM<N>/ttyUSB<N>
        # (and not a legacy ttyS) is a stable custom udev name in its own right.
        custom_name: str | None = None
        for src in sorted(sources):
            if os.path.dirname(src) != dev_dir_norm:
                continue
            base = os.path.basename(src)
            if _TTYS_RE.match(base):
                continue
            if not _RAW_ACMUSB_RE.match(base):
                custom_name = src
                break

        # stable_path precedence: by-id > stable custom name > by-path > raw.
        if by_id_link:
            stable_path = by_id_link
        elif custom_name:
            stable_path = custom_name
        elif by_path_link:
            stable_path = by_path_link
        else:
            stable_path = real

        # likely_radio heuristic: any candidate basename hits the radio words.
        candidate_names = {os.path.basename(s) for s in sources}
        candidate_names.add(os.path.basename(stable_path))
        candidate_names.add(os.path.basename(real))
        likely_radio = any(_RADIO_NAME_RE.search(n) for n in candidate_names)

        seen.add(real)
        ports.append({
            "device": real,
            "by_id": by_id_link,
            "by_path": by_path_link,
            "description": os.path.basename(stable_path),
            "hwid": "",
            "vid": None,
            "pid": None,
            "serial_number": None,
            "manufacturer": None,
            "product": None,
            "likely_radio": likely_radio,
            "stable_path": stable_path,
        })

    return ports
