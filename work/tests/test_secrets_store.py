"""Tests for meshai.secrets_store — no /data access, no env leakage."""

import json
import pytest

from meshai import secrets_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path):
    """Return a config_dir under tmp_path (parent gets a sibling secrets/)."""
    d = tmp_path / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Round-trip: set -> status True -> delete -> status False
# ---------------------------------------------------------------------------

def test_roundtrip_set_get_delete(tmp_path):
    cfg = _cfg(tmp_path)
    assert secrets_store.get_status(config_dir=cfg)["TOMTOM_API_KEY"] is False

    secrets_store.set_secret("TOMTOM_API_KEY", "abc", config_dir=cfg)
    assert secrets_store.get_status(config_dir=cfg)["TOMTOM_API_KEY"] is True

    secrets_store.delete_secret("TOMTOM_API_KEY", config_dir=cfg)
    assert secrets_store.get_status(config_dir=cfg)["TOMTOM_API_KEY"] is False


# ---------------------------------------------------------------------------
# Unknown var raises ValueError
# ---------------------------------------------------------------------------

def test_set_unknown_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="Unknown secret var"):
        secrets_store.set_secret("NOPE_KEY", "x", config_dir=cfg)


def test_delete_unknown_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="Unknown secret var"):
        secrets_store.delete_secret("NOPE_KEY", config_dir=cfg)


# ---------------------------------------------------------------------------
# Values never leak
# ---------------------------------------------------------------------------

def test_get_status_no_values(tmp_path):
    cfg = _cfg(tmp_path)
    secrets_store.set_secret("TOMTOM_API_KEY", "abc", config_dir=cfg)
    status = secrets_store.get_status(config_dir=cfg)
    # All values must be booleans
    for v in status.values():
        assert isinstance(v, bool), f"Expected bool, got {type(v)}: {v!r}"
    # The literal secret value must not appear anywhere
    assert "abc" not in str(status)


def test_list_secrets_no_values(tmp_path):
    cfg = _cfg(tmp_path)
    secrets_store.set_secret("TOMTOM_API_KEY", "abc", config_dir=cfg)
    items = secrets_store.list_secrets(config_dir=cfg)
    dumped = json.dumps(items)
    assert "abc" not in dumped, "Secret value leaked into list_secrets output"


# ---------------------------------------------------------------------------
# list_secrets shape
# ---------------------------------------------------------------------------

def test_list_secrets_shape(tmp_path):
    cfg = _cfg(tmp_path)
    items = secrets_store.list_secrets(config_dir=cfg)
    required_keys = {"env_var", "is_set", "fields", "label"}
    for item in items:
        assert required_keys == set(item.keys()), f"Item missing keys: {item}"

    tomtom = next(i for i in items if i["env_var"] == "TOMTOM_API_KEY")
    assert tomtom["fields"] == ["environmental.traffic.api_key"]
    assert isinstance(tomtom["is_set"], bool)
    assert isinstance(tomtom["label"], str)


# ---------------------------------------------------------------------------
# delete on missing file is a no-op (not an error)
# ---------------------------------------------------------------------------

def test_delete_missing_file_noop(tmp_path):
    cfg = _cfg(tmp_path)
    # .env file does not exist yet — should not raise
    secrets_store.delete_secret("SMTP_PASSWORD", config_dir=cfg)


# ---------------------------------------------------------------------------
# llm_env_var backend mapping
# ---------------------------------------------------------------------------

def test_llm_env_var_known_backends():
    assert secrets_store.llm_env_var("openai") == "OPENAI_API_KEY"
    assert secrets_store.llm_env_var("anthropic") == "ANTHROPIC_API_KEY"
    assert secrets_store.llm_env_var("google") == "GOOGLE_API_KEY"


def test_llm_env_var_unknown_falls_back():
    assert secrets_store.llm_env_var("ollama") == "LLM_API_KEY"
    assert secrets_store.llm_env_var(None) == "LLM_API_KEY"
    assert secrets_store.llm_env_var("") == "LLM_API_KEY"


# ---------------------------------------------------------------------------
# _env_to_fields: LLM vars include llm.api_key
# ---------------------------------------------------------------------------

def test_env_to_fields_llm():
    m = secrets_store._env_to_fields()
    for var in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        assert "llm.api_key" in m.get(var, []), f"Missing llm.api_key for {var}"


# ---------------------------------------------------------------------------
# SECRET_LABELS completeness
# ---------------------------------------------------------------------------

def test_secret_labels_keys():
    expected = {
        "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY",
        "TOMTOM_API_KEY", "FIRMS_MAP_KEY", "ROADS511_API_KEY",
        "SMTP_PASSWORD", "MESHMONITOR_API_TOKEN", "MQTT_PASSWORD",
    }
    assert set(secrets_store.SECRET_LABELS.keys()) == expected
