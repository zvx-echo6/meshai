"""v0.4 C.2.1: dashboard config save routes through the multi-file save_section
(the !include-aware writer), not the monolithic save_config."""

import yaml

from meshai.config_loader import save_section


def test_save_section_writes_env_feeds(tmp_path):
    (tmp_path / "env_feeds.yaml").write_text("enabled: true\nnws:\n  enabled: true\n")
    res = save_section("environmental", {
        "enabled": True,
        "nws": {"enabled": True, "feed_source": "native"},
        "usgs_quake": {"enabled": False, "feed_source": "native"},
    }, tmp_path)
    assert res["saved"] is True
    written = yaml.safe_load((tmp_path / "env_feeds.yaml").read_text())
    assert written["nws"]["feed_source"] == "native"
    assert written["usgs_quake"]["feed_source"] == "native"
    # only env_feeds.yaml was written; no config.yaml / orchestrator touched
    assert not (tmp_path / "config.yaml").exists()


def test_save_section_strips_secret_fields(tmp_path):
    res = save_section("environmental", {
        "enabled": True,
        "traffic": {"enabled": True, "api_key": "SECRET123"},
    }, tmp_path)
    written = yaml.safe_load((tmp_path / "env_feeds.yaml").read_text())
    assert "api_key" not in written.get("traffic", {})
    assert "traffic.api_key" in res["rejected_secrets"]


def test_save_section_handles_list_section(tmp_path):
    # mesh_sources is a top-level list section -- must not crash (C.2.1 guard)
    res = save_section("mesh_sources", [{"name": "a", "type": "mqtt", "enabled": True}], tmp_path)
    assert res["saved"] is True
    written = yaml.safe_load((tmp_path / "mesh_sources.yaml").read_text())
    assert isinstance(written, list)
    assert written[0]["name"] == "a"
