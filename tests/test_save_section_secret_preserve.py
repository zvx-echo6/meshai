"""v0.4 C.3.1: save_section preserves on-disk ${VAR} secret refs instead of
dropping them when a GUI save round-trips the interpolated value."""

import yaml

from meshai.config_loader import save_section


def _setup(tmp_path, env_yaml, dotenv):
    cfg = tmp_path / "config"
    cfg.mkdir()
    sec = tmp_path / "secrets"
    sec.mkdir()
    (cfg / "env_feeds.yaml").write_text(env_yaml)
    (sec / ".env").write_text(dotenv)
    return cfg


def test_preserves_unchanged_secret_ref(tmp_path):
    # on-disk has ${C31_TEST_KEY}; GUI submits the resolved value -> keep the ref
    cfg = _setup(
        tmp_path,
        "enabled: true\ntraffic:\n  enabled: true\n  api_key: ${C31_TEST_KEY}\n",
        "C31_TEST_KEY=realkey123\n",
    )
    res = save_section("environmental",
                       {"enabled": True, "traffic": {"enabled": True, "api_key": "realkey123"}},
                       cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["traffic"]["api_key"] == "${C31_TEST_KEY}"   # placeholder preserved
    assert "traffic.api_key" not in res["rejected_secrets"]


def test_changed_secret_value_is_written(tmp_path):
    # on-disk ${C31_TEST_KEY}; GUI submits a DIFFERENT value -> intentional change stored
    cfg = _setup(
        tmp_path,
        "enabled: true\ntraffic:\n  enabled: true\n  api_key: ${C31_TEST_KEY}\n",
        "C31_TEST_KEY=oldkey\n",
    )
    save_section("environmental",
                 {"enabled": True, "traffic": {"enabled": True, "api_key": "NEWKEY999"}},
                 cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["traffic"]["api_key"] == "NEWKEY999"


def test_no_placeholder_still_rejects(tmp_path):
    # no on-disk ${VAR} ref -> a raw secret must be rejected, never written
    cfg = tmp_path / "config"
    cfg.mkdir()
    res = save_section("environmental",
                       {"enabled": True, "traffic": {"enabled": True, "api_key": "RAWSECRET"}},
                       cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert "api_key" not in written.get("traffic", {})
    assert "traffic.api_key" in res["rejected_secrets"]
