"""GUI-managed secrets store for MeshAI.

Secret VALUES live in a .env file (default /data/secrets/.env).
Config YAML holds only ${VAR} references — never raw values.
This module NEVER returns or logs secret values.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import set_key, unset_key, dotenv_values


def secret_env_path(config_dir: Path = Path("/data/config")) -> Path:
    """Resolve the secrets .env path the same way load_config does; ensure parent dir exists."""
    p = Path(config_dir).parent / "secrets" / ".env"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# config dotted-field -> env var name. llm.api_key is backend-dependent (see llm_env_var).
SECRET_FIELD_TO_ENV: dict[str, str] = {
    "environmental.traffic.api_key": "TOMTOM_API_KEY",
    "environmental.firms.map_key": "FIRMS_MAP_KEY",
    "environmental.roads511.api_key": "ROADS511_API_KEY",
    "notifications.toggles.*.smtp_password": "SMTP_PASSWORD",
    "notifications.rules.*.smtp_password": "SMTP_PASSWORD",
    "mesh_sources.*.api_token": "MESHMONITOR_API_TOKEN",
}

# llm.api_key backend -> env var (mirrors config_loader.py backend dict; else LLM_API_KEY)
LLM_BACKEND_TO_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def llm_env_var(backend: str | None) -> str:
    return LLM_BACKEND_TO_ENV.get((backend or "").lower(), "LLM_API_KEY")


# Human labels for every managed env var.
SECRET_LABELS: dict[str, str] = {
    "GOOGLE_API_KEY": "Google (Gemini) API key",
    "OPENAI_API_KEY": "OpenAI API key",
    "ANTHROPIC_API_KEY": "Anthropic API key",
    "LLM_API_KEY": "LLM API key (generic)",
    "TOMTOM_API_KEY": "TomTom API key",
    "FIRMS_MAP_KEY": "NASA FIRMS MAP_KEY",
    "ROADS511_API_KEY": "511 Roads API key",
    "SMTP_PASSWORD": "SMTP password",
    "MESHMONITOR_API_TOKEN": "MeshMonitor API token",
    "MQTT_PASSWORD": "MQTT password",
}


def _managed_vars() -> list[str]:
    """Return list of managed env var names (stable insertion order)."""
    return list(SECRET_LABELS)


def get_status(config_dir: Path = Path("/data/config")) -> dict[str, bool]:
    """For each managed var, True if set (non-empty) in .env OR os.environ.

    NEVER returns values — only booleans.
    """
    path = secret_env_path(config_dir)
    try:
        env_file = dotenv_values(path) if path.exists() else {}
    except Exception:
        env_file = {}

    result: dict[str, bool] = {}
    for var in _managed_vars():
        file_val = env_file.get(var, "")
        env_val = os.environ.get(var, "")
        result[var] = bool(file_val) or bool(env_val)
    return result


def set_secret(var: str, value: str, config_dir: Path = Path("/data/config")) -> None:
    """Write var=value into the secrets .env file.

    Raises ValueError if var is not a managed secret.
    """
    if var not in SECRET_LABELS:
        raise ValueError(f"Unknown secret var: {var!r}. Allowed: {list(SECRET_LABELS)}")
    path = secret_env_path(config_dir)
    set_key(str(path), var, value)


def delete_secret(var: str, config_dir: Path = Path("/data/config")) -> None:
    """Remove var from the secrets .env file (no-op if file/key missing).

    Raises ValueError if var is not a managed secret.
    """
    if var not in SECRET_LABELS:
        raise ValueError(f"Unknown secret var: {var!r}. Allowed: {list(SECRET_LABELS)}")
    path = secret_env_path(config_dir)
    try:
        unset_key(str(path), var)
    except (KeyError, FileNotFoundError):
        pass


def _env_to_fields() -> dict[str, list[str]]:
    """Reverse of SECRET_FIELD_TO_ENV: env var -> list of dotted config fields.

    Also includes llm.api_key for all four LLM env vars.
    """
    result: dict[str, list[str]] = {}
    for field, var in SECRET_FIELD_TO_ENV.items():
        result.setdefault(var, []).append(field)

    # LLM keys all map to llm.api_key
    for llm_var in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        result.setdefault(llm_var, []).append("llm.api_key")

    return result


def list_secrets(config_dir: Path = Path("/data/config")) -> list[dict]:
    """Build the API payload for GET /secrets.

    Each item: {"env_var": str, "is_set": bool, "fields": list[str], "label": str}.
    Values are NEVER included.
    """
    status = get_status(config_dir)
    field_map = _env_to_fields()
    return [
        {
            "env_var": var,
            "is_set": status[var],
            "fields": field_map.get(var, []),
            "label": SECRET_LABELS[var],
        }
        for var in _managed_vars()
    ]


# ---------------------------------------------------------------------------
# MeshCore room-server passwords (dynamic, per-room secrets)
# ---------------------------------------------------------------------------
#
# Unlike the fixed SECRET_LABELS allowlist above, room passwords are keyed by
# the room server's public key, so the env var name is DERIVED from the pubkey
# rather than drawn from a static vocabulary. Each password is stored in the
# same secrets .env file under a per-room var name:
#
#     MESHCORE_ROOM_<PREFIX>_PWD
#
# where <PREFIX> is the first ROOM_PUBKEY_PREFIX_LEN hex chars of the room's
# public key, uppercased. The transport resolves the password by pubkey at
# send time (login-before-send); the GUI/API sets it via set_room_password().
#
# The routing cell NEVER holds the password — only ``room:<pubkey>``.

ROOM_PUBKEY_PREFIX_LEN = 12
_ROOM_PWD_PREFIX = "MESHCORE_ROOM_"
_ROOM_PWD_SUFFIX = "_PWD"


def room_pwd_env_var(pubkey: str) -> str:
    """Derive the .env var name for a room server's password from its pubkey.

    ``pubkey`` may be a full 32-byte hex key or a shorter prefix; the first
    ROOM_PUBKEY_PREFIX_LEN hex chars are used (uppercased) so a cell holding a
    prefix and a cell holding the full key resolve to the SAME secret.

    Raises ValueError on an empty pubkey.
    """
    pk = (pubkey or "").strip()
    if not pk:
        raise ValueError("room pubkey must be non-empty")
    prefix = pk[:ROOM_PUBKEY_PREFIX_LEN].upper()
    return f"{_ROOM_PWD_PREFIX}{prefix}{_ROOM_PWD_SUFFIX}"


def get_room_password(
    pubkey: str, config_dir: Path = Path("/data/config")
) -> str | None:
    """Resolve a room server's password by pubkey, or None if none is set.

    Resolution mirrors config_loader._interpolate_env_vars: os.environ takes
    precedence over the .env file. Returns the raw value for internal use
    (transport login-before-send) — do NOT expose this via a status API; the
    GUI status path must use ``room_password_is_set`` (booleans only).
    """
    try:
        var = room_pwd_env_var(pubkey)
    except ValueError:
        return None
    val = os.environ.get(var)
    if val:
        return val
    path = secret_env_path(config_dir)
    try:
        env_file = dotenv_values(path) if path.exists() else {}
    except Exception:
        env_file = {}
    val = env_file.get(var)
    return val or None


def room_password_is_set(
    pubkey: str, config_dir: Path = Path("/data/config")
) -> bool:
    """True if a password is configured for this room (never returns the value)."""
    return get_room_password(pubkey, config_dir) is not None


def set_room_password(
    pubkey: str, value: str, config_dir: Path = Path("/data/config")
) -> None:
    """Store a room server's password (GUI/API write path).

    Keyed by pubkey via room_pwd_env_var(). Raises ValueError on empty pubkey.
    An empty value deletes the entry (mirrors "clear the password").
    """
    var = room_pwd_env_var(pubkey)
    path = secret_env_path(config_dir)
    if value:
        set_key(str(path), var, value)
    else:
        try:
            unset_key(str(path), var)
        except (KeyError, FileNotFoundError):
            pass


def delete_room_password(
    pubkey: str, config_dir: Path = Path("/data/config")
) -> None:
    """Remove a room server's password from the secrets .env (no-op if absent)."""
    var = room_pwd_env_var(pubkey)
    path = secret_env_path(config_dir)
    try:
        unset_key(str(path), var)
    except (KeyError, FileNotFoundError):
        pass
