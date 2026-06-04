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


# v0.5.5: secret stripping for LIST-shaped sections.
# C.2.1 added save_section's list-section branch but field paths inside list
# items used `<key>` instead of `<index>.<key>`, so SECRET_FIELDS patterns
# like `mesh_sources.*.api_token` never matched on the list-section save
# path. The fix lands a dotted-index form (mesh_sources.0.api_token).

def test_mesh_sources_list_preserves_secret_ref(tmp_path):
    """List section (mesh_sources): an item's ${VAR} api_token must survive a
    GUI round-trip that submits the resolved value."""
    cfg = _setup(
        tmp_path,
        "",  # env_feeds.yaml unused; we write mesh_sources.yaml directly below
        "MS_TEST_TOKEN=realtoken-xyz\n",
    )
    (cfg / "mesh_sources.yaml").write_text(
        "- name: src-a\n  url: http://a.local\n  api_token: ${MS_TEST_TOKEN}\n"
    )
    res = save_section(
        "mesh_sources",
        [{"name": "src-a", "url": "http://a.local", "api_token": "realtoken-xyz"}],
        cfg,
    )
    written = yaml.safe_load((cfg / "mesh_sources.yaml").read_text())
    assert written[0]["api_token"] == "${MS_TEST_TOKEN}"           # placeholder preserved
    # rejected_secrets stores section-relative paths (no section prefix),
    # matching the convention asserted by test_no_placeholder_still_rejects.
    assert "0.api_token" not in res["rejected_secrets"]


def test_mesh_sources_list_rejects_raw_when_no_placeholder(tmp_path):
    """List section: raw secret without an on-disk placeholder must be rejected
    (same negative case the object-section path already enforces)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    res = save_section(
        "mesh_sources",
        [{"name": "src-a", "url": "http://a.local", "api_token": "RAW_LEAK"}],
        cfg,
    )
    written = yaml.safe_load((cfg / "mesh_sources.yaml").read_text())
    assert "api_token" not in (written[0] if written else {})
    assert "0.api_token" in res["rejected_secrets"]


def test_notifications_rules_nested_list_preserves_secret_ref(tmp_path):
    """Object section with a nested list (notifications.rules) -- same dotted-
    index path semantics; smtp_password placeholder must survive round-trip."""
    cfg = _setup(
        tmp_path,
        "",
        "C55_SMTP_PASS=letmein\n",
    )
    # notifications lives in its own file per SECTION_TO_FILE.
    (cfg / "notifications.yaml").write_text(
        "enabled: true\n"
        "rules:\n"
        "  - name: r0\n    enabled: true\n    smtp_password: ${C55_SMTP_PASS}\n"
    )
    res = save_section(
        "notifications",
        {
            "enabled": True,
            "rules": [{"name": "r0", "enabled": True, "smtp_password": "letmein"}],
        },
        cfg,
    )
    written = yaml.safe_load((cfg / "notifications.yaml").read_text())
    assert written["rules"][0]["smtp_password"] == "${C55_SMTP_PASS}"
    assert "rules.0.smtp_password" not in res["rejected_secrets"]
