"""Integration C1 — reusable NotificationDestinations (additive, inline fallback).

The delivery config was DUPLICATED inline on every NotificationToggle and
NotificationRuleConfig. C1 introduces a shared, named NotificationDestination
that toggles/rules reference by name. The safety contract is ADDITIVE +
regression-free:

  * EMPTY `destinations` (all pre-C1 config) -> the existing inline-field
    delivery path runs BYTE-IDENTICALLY (proven here against _toggle_to_rule /
    the rule object itself).
  * NON-EMPTY `destinations` -> delivery resolves from the shared destination's
    fields, gated only by region scope + the min_severity floor. The
    severity_channels matrix is intentionally BYPASSED on the destination path.
"""

import asyncio
import dataclasses

import pytest

from meshai.config import (
    Config,
    NotificationDestination,
    NotificationRuleConfig,
    load_config,
    save_config,
    synthesize_destinations,
)
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event


class RecChannel:
    """Records the FULL rule object handed to create_channel/deliver so tests
    can assert delivery params are byte-identical."""

    def __init__(self, rule, rec, succeed=True):
        self._rule = rule
        self._rec = rec
        self._succeed = succeed

    async def deliver(self, payload, rule):
        self._rec.append({
            "delivery_type": rule.delivery_type,
            "rule": rule,
            "message": payload.message,
        })
        return self._succeed


def _disp(cfg, succeed=True):
    rec: list = []
    d = Dispatcher(cfg,
                   lambda rule, conn: RecChannel(rule, rec, succeed),
                   connector=None)
    return d, rec


def _base_cfg():
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    return cfg


def _weather_ev(eid="ev-1", severity="priority"):
    ev = make_event(source="nws", category="weather.alert.severe",
                    severity=severity, title="t")
    ev.id = eid
    return ev


def _rule_fields(rule):
    """Delivery-relevant subset for byte-identical comparison."""
    return {
        k: getattr(rule, k) for k in (
            "delivery_type", "broadcast_channel", "meshcore_channel",
            "node_ids", "meshcore_dm_contacts", "smtp_host", "smtp_port",
            "smtp_user", "smtp_password", "smtp_tls", "from_address",
            "recipients", "webhook_url", "webhook_headers",
        )
    }


# --------------------------------------------------------------- inline path


def test_inline_toggle_delivery_is_byte_identical():
    """A toggle with inline delivery fields + EMPTY destinations must deliver
    via the EXACT same synthesized rule params as the unchanged inline path
    (_toggle_to_rule)."""
    cfg = _base_cfg()
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "routine"
    t.cooldown_seconds = 0
    t.severity_channels = {"priority": ["mesh_broadcast", "email", "webhook"]}
    t.broadcast_channel = 3
    t.smtp_host = "smtp.example.com"
    t.smtp_port = 2525
    t.smtp_user = "u"
    t.smtp_password = "p"
    t.from_address = "alerts@example.com"
    t.recipients = ["ops@example.com", "oncall@example.com"]
    t.webhook_url = "https://hook.example.com/x"
    t.webhook_headers = {"X-Token": "abc"}
    assert t.destinations == []          # opted out -> inline fallback

    d, rec = _disp(cfg)
    ev = _weather_ev(severity="priority")
    asyncio.run(d.dispatch(ev))

    # One delivery per channel type in the matrix row, same order.
    assert [r["delivery_type"] for r in rec] == \
        ["mesh_broadcast", "email", "webhook"]

    # Each delivered rule is byte-identical to the unchanged _toggle_to_rule
    # construction -> proves the inline path routes exactly as before.
    for ct, r in zip(["mesh_broadcast", "email", "webhook"], rec):
        expected = _rule_fields(d._toggle_to_rule(t, ct, ev))
        assert _rule_fields(r["rule"]) == expected


def test_inline_rule_delivery_unchanged_when_no_destinations():
    """A condition rule with inline delivery + EMPTY destinations delivers via
    the rule object itself (drule IS rule)."""
    cfg = _base_cfg()
    rule = NotificationRuleConfig(
        name="ops-email", enabled=True, trigger_type="condition",
        categories=["weather.alert.severe"], min_severity="routine",
        delivery_type="email", smtp_host="smtp.example.com",
        recipients=["ops@example.com"], from_address="a@example.com",
    )
    assert rule.destinations == []
    cfg.notifications.rules = [rule]

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="immediate")))
    assert len(rec) == 1
    # The delivered rule is the SAME object -> byte-identical, zero regression.
    assert rec[0]["rule"] is rule


# ----------------------------------------------------------- destination path


def test_toggle_destination_path_uses_destination_params():
    """A toggle referencing a destination delivers via the DESTINATION's fields,
    not the toggle's inline fields (inline values differ to prove it)."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "email_ops": NotificationDestination(
            name="email_ops", type="email",
            smtp_host="dest-smtp.example.com", smtp_port=465, smtp_user="du",
            smtp_password="dp", from_address="dest@example.com",
            recipients=["dest-ops@example.com"],
        ),
    }
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "routine"
    t.cooldown_seconds = 0
    t.destinations = ["email_ops"]
    # Divergent inline values that MUST be ignored on the destination path.
    t.severity_channels = {"priority": ["mesh_broadcast"]}
    t.smtp_host = "INLINE-should-not-be-used"
    t.recipients = ["inline@example.com"]

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="priority")))

    assert len(rec) == 1
    r = rec[0]["rule"]
    assert r.delivery_type == "email"
    assert r.smtp_host == "dest-smtp.example.com"
    assert r.smtp_port == 465
    assert r.recipients == ["dest-ops@example.com"]
    assert r.from_address == "dest@example.com"


def test_toggle_destinations_bypass_severity_channels_matrix():
    """The destination path is gated by the min_severity FLOOR only; an empty
    severity_channels matrix does NOT suppress it (matrix is bypassed)."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "mesh_a": NotificationDestination(
            name="mesh_a", type="mesh_broadcast", broadcast_channel=7),
    }
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "priority"
    t.cooldown_seconds = 0
    t.destinations = ["mesh_a"]
    t.severity_channels = {}   # empty matrix would kill the inline path

    d, rec = _disp(cfg)
    # Above the floor -> fires despite empty matrix.
    asyncio.run(d.dispatch(_weather_ev(eid="a", severity="priority")))
    assert len(rec) == 1
    assert rec[0]["rule"].delivery_type == "mesh_broadcast"
    assert rec[0]["rule"].broadcast_channel == 7


def test_toggle_destinations_respect_min_severity_floor():
    """Below the floor, the destination path does not fire (floor still gates)."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "mesh_a": NotificationDestination(
            name="mesh_a", type="mesh_broadcast", broadcast_channel=7),
    }
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "immediate"
    t.cooldown_seconds = 0
    t.destinations = ["mesh_a"]
    t.severity_channels = {}

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="priority")))  # below floor
    assert rec == []


def test_toggle_multiple_destinations_all_fire():
    """A toggle may fan out to several destinations of mixed types; digest-typed
    destinations are skipped on the live path."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "mesh_a": NotificationDestination(
            name="mesh_a", type="mesh_broadcast", broadcast_channel=1),
        "email_ops": NotificationDestination(
            name="email_ops", type="email", smtp_host="s", recipients=["r"]),
        "daily": NotificationDestination(name="daily", type="digest"),
    }
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "routine"
    t.cooldown_seconds = 0
    t.destinations = ["mesh_a", "email_ops", "daily"]

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="priority")))
    # digest is excluded from the live broadcast path.
    assert sorted(r["delivery_type"] for r in rec) == ["email", "mesh_broadcast"]


def test_unknown_destination_reference_is_skipped_not_fatal():
    """A dangling destination name degrades gracefully (skipped, no crash) and,
    when it leaves nothing to deliver, drops rather than falling back to inline."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {}     # registry empty
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "routine"
    t.cooldown_seconds = 0
    t.destinations = ["nope"]
    t.broadcast_channel = 9                 # inline present but not used

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="priority")))
    assert rec == []


def test_rule_destination_fanout():
    """A condition rule referencing destinations fans out to each; the rule's
    own inline delivery_type is superseded."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "mesh_a": NotificationDestination(
            name="mesh_a", type="mesh_broadcast", broadcast_channel=2),
        "email_ops": NotificationDestination(
            name="email_ops", type="email", smtp_host="s", recipients=["r"]),
    }
    rule = NotificationRuleConfig(
        name="fanout", enabled=True, trigger_type="condition",
        categories=["weather.alert.severe"], min_severity="routine",
        delivery_type="webhook", webhook_url="https://inline-not-used",
        destinations=["mesh_a", "email_ops"],
    )
    cfg.notifications.rules = [rule]

    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_weather_ev(severity="immediate")))
    assert sorted(r["delivery_type"] for r in rec) == ["email", "mesh_broadcast"]
    assert all(r["rule"] is not rule for r in rec)   # synthesized, not inline


# ------------------------------------------------------------------ round-trip


def test_destinations_roundtrip_save_load(tmp_path):
    """destinations dict + toggle.destinations + rule.destinations survive a
    YAML save/load cycle as real dataclasses / lists."""
    cfg = _base_cfg()
    cfg.notifications.destinations = {
        "email_ops": NotificationDestination(
            name="email_ops", type="email", smtp_host="smtp.example.com",
            smtp_port=465, recipients=["a@example.com", "b@example.com"],
            webhook_headers={},
        ),
        "mesh_a": NotificationDestination(
            name="mesh_a", type="mesh_broadcast", broadcast_channel=4),
    }
    cfg.notifications.toggles["weather"].destinations = ["email_ops", "mesh_a"]
    cfg.notifications.rules = [NotificationRuleConfig(
        name="r1", destinations=["email_ops"])]

    p = tmp_path / "config.yaml"
    save_config(cfg, p)
    loaded = load_config(p)

    dests = loaded.notifications.destinations
    assert set(dests) == {"email_ops", "mesh_a"}
    assert isinstance(dests["email_ops"], NotificationDestination)
    assert dests["email_ops"].smtp_port == 465
    assert dests["email_ops"].recipients == ["a@example.com", "b@example.com"]
    assert dests["mesh_a"].broadcast_channel == 4
    assert loaded.notifications.toggles["weather"].destinations == \
        ["email_ops", "mesh_a"]
    assert loaded.notifications.rules[0].destinations == ["email_ops"]


# ----------------------------------------------- synthesize_destinations helper


def test_synthesize_destinations_populates_refs_and_dedups():
    """The C2 migration helper creates named destinations from inline fields and
    de-duplicates identical configs. It is NOT auto-run by delivery."""
    cfg = _base_cfg()
    # Two rules with an IDENTICAL email delivery -> should share one destination.
    common = dict(delivery_type="email", smtp_host="s", from_address="f",
                  recipients=["r"])
    cfg.notifications.rules = [
        NotificationRuleConfig(name="r1", **common),
        NotificationRuleConfig(name="r2", **common),
    ]
    for t in cfg.notifications.toggles.values():
        t.severity_channels = {}     # keep toggle synthesis empty for this test

    synthesize_destinations(cfg)

    assert cfg.notifications.rules[0].destinations
    # Same inline config -> same shared destination name.
    assert cfg.notifications.rules[0].destinations == \
        cfg.notifications.rules[1].destinations
    ref = cfg.notifications.rules[0].destinations[0]
    dest = cfg.notifications.destinations[ref]
    assert dest.type == "email"
    assert dest.recipients == ["r"]
