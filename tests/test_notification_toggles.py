"""v0.5 Section 1: NotificationToggle dispatch routing tests."""

import asyncio

from meshai.config import Config
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event


class RecChannel:
    def __init__(self, rec):
        self.rec = rec

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "name": rule.name,
            "broadcast_channel": rule.broadcast_channel,
            "node_ids": list(rule.node_ids),
            "override_quiet": rule.override_quiet,
        })
        return True


def _dispatch(cfg, event):
    rec = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec), connector=None)
    asyncio.run(d.dispatch(event))
    return rec


def _cfg(enable="weather", **kw):
    cfg = Config()
    cfg.notifications.rules = []
    t = cfg.notifications.toggles[enable]
    t.enabled = True
    t.min_severity = kw.get("min_severity", "priority")
    t.regions = kw.get("regions", [])
    t.severity_channels = kw.get("severity_channels", {"priority": ["mesh_broadcast"]})
    return cfg


def _ev(severity="priority", category="weather_warning", region=None, regions=None):
    return make_event(source="nws", category=category, severity=severity,
                      region=region, regions=regions or [], title="t")


def test_disabled_toggle_no_dispatch():
    cfg = Config(); cfg.notifications.rules = []  # weather disabled by default
    assert _dispatch(cfg, _ev()) == []


def test_enabled_toggle_dispatches():
    rec = _dispatch(_cfg(), _ev(severity="priority"))
    assert len(rec) == 1 and rec[0]["delivery_type"] == "mesh_broadcast"
    assert rec[0]["name"] == "toggle:weather"


def test_region_empty_allows_all():
    rec = _dispatch(_cfg(regions=[]), _ev(region="Boise"))
    assert len(rec) == 1


def test_region_populated_blocks_mismatch():
    cfg = _cfg(regions=["Magic Valley"])
    assert _dispatch(cfg, _ev(region="Boise")) == []
    assert len(_dispatch(cfg, _ev(region="Magic Valley"))) == 1


def test_region_matches_via_regions_list():
    cfg = _cfg(regions=["Magic Valley"])
    assert len(_dispatch(cfg, _ev(region=None, regions=["Magic Valley", "X"]))) == 1


def test_severity_threshold():
    cfg = _cfg(min_severity="priority",
               severity_channels={"routine": ["mesh_broadcast"], "priority": ["mesh_broadcast"],
                                  "immediate": ["mesh_broadcast"]})
    assert _dispatch(cfg, _ev(severity="routine")) == []     # below threshold
    assert len(_dispatch(cfg, _ev(severity="priority"))) == 1
    assert len(_dispatch(cfg, _ev(severity="immediate"))) == 1


def test_per_severity_channel_routing():
    cfg = _cfg(min_severity="routine",
               severity_channels={"priority": ["mesh_broadcast"],
                                  "immediate": ["mesh_broadcast", "mesh_dm"]})
    assert len(_dispatch(cfg, _ev(severity="priority"))) == 1
    imm = _dispatch(cfg, _ev(severity="immediate"))
    assert {r["delivery_type"] for r in imm} == {"mesh_broadcast", "mesh_dm"}


def test_digest_channel_skipped_in_live_dispatch():
    cfg = _cfg(severity_channels={"priority": ["digest", "mesh_broadcast"]})
    rec = _dispatch(cfg, _ev(severity="priority"))
    assert [r["delivery_type"] for r in rec] == ["mesh_broadcast"]  # digest not live-dispatched


def test_quiet_hours_override_immediate_only():
    cfg = _cfg(min_severity="routine",
               severity_channels={"priority": ["mesh_broadcast"], "immediate": ["mesh_broadcast"]})
    cfg.notifications.toggles["weather"].quiet_hours_override = True
    assert _dispatch(cfg, _ev(severity="priority"))[0]["override_quiet"] is False
    assert _dispatch(cfg, _ev(severity="immediate"))[0]["override_quiet"] is True


def test_category_maps_to_correct_family():
    # seismic family toggle handles earthquake_event via get_toggle fallback
    cfg = Config(); cfg.notifications.rules = []
    cfg.notifications.toggles["seismic"].enabled = True
    cfg.notifications.toggles["seismic"].severity_channels = {"priority": ["mesh_broadcast"]}
    rec = _dispatch(cfg, _ev(severity="priority", category="earthquake_event"))
    assert len(rec) == 1 and rec[0]["name"] == "toggle:seismic"


def test_rules_and_toggles_both_fire():
    from meshai.config import NotificationRuleConfig
    cfg = _cfg()
    cfg.notifications.rules = [NotificationRuleConfig(
        name="legacy", enabled=True, trigger_type="condition",
        categories=["weather_warning"], min_severity="routine",
        delivery_type="mesh_broadcast")]
    rec = _dispatch(cfg, _ev(severity="priority"))
    names = {r["name"] for r in rec}
    assert "legacy" in names and "toggle:weather" in names  # parallel paths both fire
