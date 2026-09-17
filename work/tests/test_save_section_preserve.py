"""v0.16-config-preserve: save_section must MERGE onto the on-disk file for
"dedicated file" sections (env_feeds.yaml, notifications.yaml, ...) instead of
replacing it wholesale.

Root cause (confirmed on prod 2026-09-16): config_loader.py::save_section's
"dedicated file" branch used to do ``existing = domain_data`` -- the
serialized dataclass IS the new file, full stop. A PUT that changed a single
field (e.g. environmental.watchduty.enabled) silently dropped:
  - top-level keys the EnvironmentalConfig dataclass has never heard of
    (a `central:` block from the retired Central integration),
  - keys nested inside a KNOWN dataclass that aren't dataclass fields
    (`wzdx.endpoints`),
  - and comments.
The same one-level-down wholesale replace existed for the
meshtastic.yaml/config.yaml per-key path (`existing[section_name] =
domain_data`) -- see test_meshtastic_yaml_unknown_key_survives below.

These tests pin the fix: only what a section's dataclass schema actually
declares is allowed to change; everything else on disk rides through
untouched, dict[str, Dataclass] fields (notifications.destinations/toggles)
still support real deletion, and lists are replaced whole (no index
alignment) per the documented merge design.
"""

import yaml

from meshai.config import EnvironmentalConfig, _dataclass_to_dict, _dict_to_dataclass
from meshai.config_loader import save_section


def _setup(tmp_path, env_yaml, dotenv=""):
    cfg = tmp_path / "config"
    cfg.mkdir()
    sec = tmp_path / "secrets"
    sec.mkdir()
    (cfg / "env_feeds.yaml").write_text(env_yaml)
    (sec / ".env").write_text(dotenv)
    return cfg


# ---------------------------------------------------------------------------
# (a) The prod replay: central: block, wzdx.endpoints, blank secret fields,
#     comments -- only the changed field's lines should differ.
# ---------------------------------------------------------------------------

PROD_LIKE_ENV_FEEDS = """\
# Environmental feeds -- hand-edited comment that PyYAML cannot preserve
enabled: true
nws_zones:
  - IDZ016
  - IDZ030
# Retired Central integration block -- not an EnvironmentalConfig field at all
central:
  nats_url: nats://central.echo6.mesh:4222
  enabled: false
watchduty:
  enabled: false
  feed_source: native
  tick_seconds: 900
wzdx:
  enabled: false
  feed_source: native
  # legacy key, never became a WZDxConfig field
  endpoints:
    - /get/event
traffic:
  enabled: false
  feed_source: native
  api_key: ''
roads511:
  enabled: false
  feed_source: native
  api_key: ''
"""


def test_env_feeds_replay_only_changed_field_differs(tmp_path):
    cfg = _setup(tmp_path, PROD_LIKE_ENV_FEEDS)

    before_raw = yaml.safe_load((cfg / "env_feeds.yaml").read_text())

    # Emulate the real GET->edit->PUT round trip: coerce the raw file through
    # the dataclass (this is what GET /api/config/environmental returns) then
    # flip exactly one field, exactly like the dashboard would submit.
    current = _dict_to_dataclass(EnvironmentalConfig, before_raw)
    current.watchduty.enabled = True
    submitted = _dataclass_to_dict(current)

    save_section("environmental", submitted, cfg)

    after_raw = yaml.safe_load((cfg / "env_feeds.yaml").read_text())

    # The unknown top-level block and the unknown nested key both survive.
    assert after_raw["central"] == before_raw["central"]
    assert after_raw["wzdx"]["endpoints"] == before_raw["wzdx"]["endpoints"]
    # The blank secret fields (no on-disk ${VAR} ref) are rejected on write,
    # same as today -- but rejection must not delete them from disk anymore
    # (the old wholesale-replace dropped the whole key when check_secrets
    # produced no value for it); '' survives as '' since it never changed.
    assert after_raw["traffic"]["api_key"] == ""
    assert after_raw["roads511"]["api_key"] == ""

    # Only the field we changed actually changed -- restricted to keys that
    # already existed on disk, since a full-object round trip legitimately
    # ADDS every other EnvironmentalConfig field (satpass, firms, ...) with
    # its dataclass default the first time it's saved. That's expected
    # (identical to what the pre-fix wholesale replace also did) and not
    # part of what this bug is about; what matters is that nothing the file
    # ALREADY HAD changed except the one field we edited.
    diffs = []
    def walk(b, a, path=""):
        if isinstance(b, dict) and isinstance(a, dict):
            for k in b.keys():  # only pre-existing keys
                walk(b.get(k), a.get(k), f"{path}.{k}" if path else k)
        elif b != a:
            diffs.append((path, b, a))
    walk(before_raw, after_raw)
    assert diffs == [("watchduty.enabled", False, True)], diffs


# ---------------------------------------------------------------------------
# (b) An unknown key nested inside a known dataclass survives a sibling save.
# ---------------------------------------------------------------------------

def test_unknown_nested_key_survives_sibling_change(tmp_path):
    cfg = _setup(
        tmp_path,
        "enabled: true\n"
        "wzdx:\n"
        "  enabled: false\n"
        "  feed_source: native\n"
        "  endpoints: ['/get/event']\n"
        "  mystery_field: 123\n",
    )
    save_section("environmental", {
        "enabled": True,
        "wzdx": {"enabled": True, "feed_source": "native"},
    }, cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["wzdx"]["enabled"] is True          # the sent change applied
    assert written["wzdx"]["endpoints"] == ["/get/event"]
    assert written["wzdx"]["mystery_field"] == 123      # unknown key preserved


# ---------------------------------------------------------------------------
# (c) notifications.destinations: add / remove / unknown-key-in-survivor.
# ---------------------------------------------------------------------------

def _notif_setup(tmp_path, notif_yaml):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "notifications.yaml").write_text(notif_yaml)
    return cfg


def test_destinations_entry_removed_on_disk(tmp_path):
    cfg = _notif_setup(
        tmp_path,
        "enabled: true\n"
        "destinations:\n"
        "  keep_me:\n"
        "    type: mesh_broadcast\n"
        "    broadcast_channel: 0\n"
        "  delete_me:\n"
        "    type: webhook\n"
        "    webhook_url: http://example.invalid\n",
    )
    save_section("notifications", {
        "enabled": True,
        "destinations": {
            "keep_me": {"type": "mesh_broadcast", "broadcast_channel": 0},
        },
    }, cfg)
    written = yaml.safe_load((cfg / "notifications.yaml").read_text())
    assert "delete_me" not in written["destinations"]
    assert "keep_me" in written["destinations"]


def test_destinations_entry_added_on_disk(tmp_path):
    cfg = _notif_setup(
        tmp_path,
        "enabled: true\n"
        "destinations:\n"
        "  keep_me:\n"
        "    type: mesh_broadcast\n"
        "    broadcast_channel: 0\n",
    )
    save_section("notifications", {
        "enabled": True,
        "destinations": {
            "keep_me": {"type": "mesh_broadcast", "broadcast_channel": 0},
            "brand_new": {"type": "webhook", "webhook_url": "http://x.invalid"},
        },
    }, cfg)
    written = yaml.safe_load((cfg / "notifications.yaml").read_text())
    assert written["destinations"]["brand_new"]["webhook_url"] == "http://x.invalid"
    assert written["destinations"]["keep_me"]["broadcast_channel"] == 0


def test_destinations_survivor_keeps_unknown_key(tmp_path):
    cfg = _notif_setup(
        tmp_path,
        "enabled: true\n"
        "destinations:\n"
        "  ops:\n"
        "    type: email\n"
        "    smtp_host: mail.example.invalid\n"
        "    legacy_note: do-not-delete\n",
    )
    save_section("notifications", {
        "enabled": False,  # change a sibling top-level field, not the destination
        "destinations": {
            "ops": {"type": "email", "smtp_host": "mail.example.invalid"},
        },
    }, cfg)
    written = yaml.safe_load((cfg / "notifications.yaml").read_text())
    assert written["enabled"] is False
    assert written["destinations"]["ops"]["legacy_note"] == "do-not-delete"


# ---------------------------------------------------------------------------
# (d) A list field shortened on save is shortened on disk.
# ---------------------------------------------------------------------------

def test_list_field_shortened_on_save(tmp_path):
    cfg = _notif_setup(
        tmp_path,
        "enabled: true\n"
        "rules:\n"
        "  - name: r0\n    enabled: true\n"
        "  - name: r1\n    enabled: true\n"
        "  - name: r2\n    enabled: true\n",
    )
    save_section("notifications", {
        "enabled": True,
        "rules": [{"name": "r0", "enabled": True}],
    }, cfg)
    written = yaml.safe_load((cfg / "notifications.yaml").read_text())
    assert [r["name"] for r in written["rules"]] == ["r0"]


# ---------------------------------------------------------------------------
# (e) Non-secret ${VAR} reference: kept when unchanged, replaced when changed.
# ---------------------------------------------------------------------------

def test_nonsecret_var_ref_preserved_when_unchanged(tmp_path):
    cfg = _setup(
        tmp_path,
        "enabled: true\n"
        "roads511:\n"
        "  enabled: true\n"
        "  feed_source: native\n"
        "  base_url: ${ROADS511_BASE_URL}\n",
        "ROADS511_BASE_URL=https://511.idaho.gov/api/v2\n",
    )
    save_section("environmental", {
        "enabled": True,
        "roads511": {
            "enabled": True,
            "feed_source": "native",
            # GET returns the interpolated value; GUI round-trips it unchanged.
            "base_url": "https://511.idaho.gov/api/v2",
        },
    }, cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["roads511"]["base_url"] == "${ROADS511_BASE_URL}"


def test_nonsecret_var_ref_replaced_when_changed(tmp_path):
    cfg = _setup(
        tmp_path,
        "enabled: true\n"
        "roads511:\n"
        "  enabled: true\n"
        "  feed_source: native\n"
        "  base_url: ${ROADS511_BASE_URL}\n",
        "ROADS511_BASE_URL=https://511.idaho.gov/api/v2\n",
    )
    save_section("environmental", {
        "enabled": True,
        "roads511": {
            "enabled": True,
            "feed_source": "native",
            "base_url": "https://511.oregon.gov/api/v2",  # operator changed it
        },
    }, cfg)
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["roads511"]["base_url"] == "https://511.oregon.gov/api/v2"


# ---------------------------------------------------------------------------
# (f) Secrets: existing preserve/reject tests already re-run unmodified in
#     tests/test_save_section_secret_preserve.py -- nothing to duplicate here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (g) The include file is written in place; config.yaml's !include is
#     unchanged and never inlined.
# ---------------------------------------------------------------------------

def test_include_file_written_config_yaml_untouched(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    config_yaml_text = (
        "timezone: America/Boise\n"
        "bot:\n  name: TestBot\n"
        "environmental: !include 'env_feeds.yaml'\n"
    )
    (cfg / "config.yaml").write_text(config_yaml_text)
    (cfg / "env_feeds.yaml").write_text("enabled: true\nwatchduty:\n  enabled: false\n")

    save_section("environmental", {"enabled": True, "watchduty": {"enabled": True}}, cfg)

    # config.yaml (the orchestrator, with the !include directive) is
    # completely untouched -- environmental lives in its own file.
    assert (cfg / "config.yaml").read_text() == config_yaml_text
    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["watchduty"]["enabled"] is True


def test_meshtastic_yaml_unknown_key_survives(tmp_path):
    """Item 4: the same one-level-down wholesale replace existed for the
    meshtastic.yaml/config.yaml per-key path. A legacy/unknown key inside
    `connection` must survive a save of a sibling field."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "meshtastic.yaml").write_text(
        "connection:\n"
        "  type: tcp\n"
        "  tcp_host: 192.168.1.100\n"
        "  legacy_unknown_field: keep-me\n"
        "commands:\n"
        "  enabled: true\n"
        "  legacy_commands_field: also-keep-me\n"
    )
    save_section("connection", {"type": "tcp", "tcp_port": 4404}, cfg)
    written = yaml.safe_load((cfg / "meshtastic.yaml").read_text())
    assert written["connection"]["tcp_port"] == 4404
    assert written["connection"]["legacy_unknown_field"] == "keep-me"
    # untouched sibling section (commands) is completely unaffected
    assert written["commands"]["legacy_commands_field"] == "also-keep-me"


# ---------------------------------------------------------------------------
# (h) local.yaml overlay: a field extracted to local.yaml (LOCAL_FIELDS) must
#     never be written into the base domain file, even when the request
#     round-trips the live (overlay-merged) value on an unrelated save.
#
#     This also pins the fix to _extract_local_fields' dotted-path walk: the
#     multi-level LOCAL_FIELDS entries (environmental.ducting.latitude/
#     longitude) never matched before (the code compared the WHOLE dotted
#     remainder "ducting.latitude" against domain_data's flat top-level keys,
#     which never exists), so the local.yaml env_center override silently
#     leaked into env_feeds.yaml on every environmental save.
# ---------------------------------------------------------------------------

def test_local_overlay_ducting_latlon_not_written_to_base_file(tmp_path):
    from meshai.config_loader import load_config

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "timezone: America/Boise\nenvironmental: !include 'env_feeds.yaml'\n"
    )
    (cfg / "env_feeds.yaml").write_text(
        "enabled: true\nducting:\n  enabled: true\n  latitude: 0.0\n  longitude: 0.0\n"
    )
    (cfg / "local.yaml").write_text(
        "env_center:\n  latitude: 42.5\n  longitude: -114.4\n"
    )

    config = load_config(cfg)
    assert config.environmental.ducting.latitude == 42.5  # overlay applied at runtime

    # Simulate a PUT that only changes an unrelated field, submitting the
    # live (overlay-merged) section dict -- exactly what
    # dashboard/api/config_routes.py._merge_over_current builds its body
    # from.
    submitted = _dataclass_to_dict(config.environmental)
    submitted["nws_zones"] = ["IDZ999"]
    save_section("environmental", submitted, cfg)

    on_disk = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert on_disk["ducting"]["latitude"] == 0.0
    assert on_disk["ducting"]["longitude"] == 0.0

    local = yaml.safe_load((cfg / "local.yaml").read_text())
    assert local["env_center"]["latitude"] == 42.5
    assert local["env_center"]["longitude"] == -114.4


# ---------------------------------------------------------------------------
# (i) PUT /api/config/<section> end-to-end on a temp dir: response shape is
#     unchanged, and an unknown key on disk is preserved through the route.
# ---------------------------------------------------------------------------

def test_put_route_preserves_unknown_key(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meshai.config import Config, EnvironmentalConfig
    from meshai.dashboard.api.config_routes import router

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "timezone: America/Boise\nenvironmental: !include 'env_feeds.yaml'\n"
    )
    (cfg / "env_feeds.yaml").write_text(
        "enabled: true\n"
        "central:\n"
        "  nats_url: nats://central.echo6.mesh:4222\n"
        "watchduty:\n"
        "  enabled: false\n"
        "  feed_source: native\n"
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    config = Config()
    config.environmental = EnvironmentalConfig()
    app.state.config = config
    app.state.config_path = str(cfg / "config.yaml")
    client = TestClient(app)

    resp = client.put(
        "/api/config/environmental",
        json={"watchduty": {"enabled": True}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) >= {"saved", "restart_required", "changed_keys"}
    assert body["saved"] is True

    written = yaml.safe_load((cfg / "env_feeds.yaml").read_text())
    assert written["watchduty"]["enabled"] is True
    assert written["central"]["nats_url"] == "nats://central.echo6.mesh:4222"
