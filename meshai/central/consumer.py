"""Central connector — consumes Central's NATS JetStream firehose and
normalizes CloudEvents envelopes into meshai pipeline Events.

v0.4 C.1: backend only. The consumer subscribes only to subjects derived from
adapters whose config `source == "central"`. With every adapter defaulting to
`native`, it starts as a no-op (0 subscriptions) and introduces no NATS
dependency at boot. Flipping an adapter to central is Phase C.3.

Wire format (see Central CONSUMER-INTEGRATION guide, confirmed in v0.4 Phase A):
  envelope (CloudEvents v1.0) -> envelope["data"] (Central Event)
    -> Event["data"] (upstream payload, verbatim, incl `_enriched`)
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from meshai.notifications.events import Event, make_event
from meshai.notifications.categories import get_category

logger = logging.getLogger("meshai.central.consumer")


def consumer_config():
    """JetStream consumer config for Central subscriptions.

    deliver_policy=NEW: subscribe to messages published AFTER consumer creation.
    Avoids replaying the entire retained backlog on first flip (could be 330k+
    msgs for high-volume streams like traffic_flow).
    """
    from nats.js.api import ConsumerConfig, DeliverPolicy
    return ConsumerConfig(deliver_policy=DeliverPolicy.NEW)


# Bare-wildcard subjects, pre-v0.9.20. Still used when `central.region` is
# empty (backward-compat fallback) and as the canonical adapter -> family map.
# Adapters with no Central equivalent (avalanche, ducting) are absent; flipping
# those to source=central subscribes to nothing (logged).
_SUBJECTS_BARE: dict[str, list[str]] = {
    "nws": ["central.wx.>"],
    "fires": ["central.fire.incident.>", "central.fire.perimeter.>"],
    "firms": ["central.fire.>"],
    "usgs_quake": ["central.quake.>"],
    "usgs": ["central.hydro.>"],
    "swpc": ["central.space.>"],
    "traffic": ["central.traffic.>"],
    "roads511": ["central.traffic.>"],   # shared with traffic; sub-adapter routing
}

# Backwards-compat: keep ADAPTER_SUBJECTS importable for legacy readers/tests.
ADAPTER_SUBJECTS = _SUBJECTS_BARE


def _subjects_for(adapter: str, region: Optional[str]) -> list[str]:
    """Build region-aware Central subject filters for an adapter (v0.5.4).

    Central v0.9.20 (shipped 2026-05-28) added per-region subject suffixes so
    consumers interested in a single region can have the firehose filtered
    server-side instead of dragging all-US events and discarding 95% locally.

    `region` is a dotted token tree, e.g. 'us.id' for Idaho. Adapters use
    one of three suffix patterns; the v0.9.20 scheme is not uniform:

      - region BEFORE the wildcard (nws):
            central.wx.alert.us.id.>
      - USGS NWIS hydro — three single-token wildcards + bare region tail:
            central.hydro.*.*.*.us.id        (per-state, e.g. Idaho)
            central.hydro.*.*.*.unknown      (gauges whose state Central
                                              couldn't resolve; documented
                                              workaround until backfill)
        Per Central v0.10.0 nwis.py producer code, the actual published
        subject is `central.hydro.<param>.<agency>.<site>.<region>` where
        <region> is `us.<state>` (7 tokens) or `unknown` (6 tokens). The
        doc §nwis text shows only the 4-token category-shape stem and is
        stale w.r.t. the regional suffix. v0.5.7-water fixes the
        pre-v0.5.7-water `central.hydro.>.<state>` shape, which was
        invalid NATS (`>` mid-subject).
      - USGS quake — no region in subject (per Central v0.10.0 guide §usgs_quake):
            central.quake.event.<tier>
        4 tokens total. <tier> is one of {minor, light, moderate, strong,
        major, great} -- USGS magnitude bands, NOT a severity integer.
        State filtering must happen client-side via data.latitude/longitude
        (same situation as FIRMS, fixed in v0.5.7-fire).
        v0.5.7-seismic restored the legal tail-only `>` here; the pre-
        v0.5.7-seismic `central.quake.event.>.us.id` was syntactically
        invalid AND wouldn't have matched anything Central publishes (only
        4 tokens, no us.<state>).
      - FIRMS — no region in subject at all (per Central v0.10.0 guide):
            central.fire.hotspot.<satellite>.<confidence>
        State filtering must happen client-side via data.latitude/longitude.
        v0.5.7-fire restored the legal tail-only `>` here; the pre-v0.5.7-fire
        `central.fire.hotspot.>.us.id` was syntactically invalid AND wouldn't
        have matched anything Central publishes (only 5 tokens, no us.<state>).
      - state-only token at a fixed depth (fires WFIGS):
            central.fire.incident.<state>.>      (active)
            central.fire.perimeter.<state>.>     (active)
            central.fire.incident.removed.<state>   (removal tombstone)
            central.fire.perimeter.removed.<state>  (removal tombstone)
        v0.5.7-fire added the tombstone subjects: pre-v0.5.7-fire we only
        subscribed to the active subjects, silently dropping all WFIGS
        fall-off signals.
      - traffic family — Convention B, bare state, no wildcard:
            central.traffic.<event_type>.id           (wzdx, tomtom_incidents,
                                                       state_511_atis)
      - traffic family — Convention A, us.<state>:
            central.traffic.<event_type>.us.id        (itd_511, Idaho-only)
      - region ignored (swpc) — space weather is planetary.

    NATS rule: `>` is only legal at the tail. Pre-v0.5.7-traffic this file
    shipped `central.traffic.>.{state}` for traffic+roads511, which was
    syntactically invalid (`>` mid-subject). Fixed by switching to single-
    token `*` wildcards for the per-event-type slot. roads511 now owns
    BOTH the bare-state (Convention B, shared with traffic) and the
    us.<state> (Convention A, itd_511-only) subjects so itd_511 events
    attribute to roads511 in meshai.

    The .unknown workaround: v0.9.20 leaves USGS hydro events whose gauge
    state can't be inferred at the `central.hydro.*.*.*.unknown` subject
    (6 tokens). Subscribing to both the per-state and the unknown filters
    avoids losing those rows until the upstream NWIS state-tag backfill.

    Empty/None region returns the bare-wildcard form (v0.5.3 behaviour).
    Adapters without a Central equivalent (avalanche, ducting) return [].
    """
    if not region:
        return list(_SUBJECTS_BARE.get(adapter, []))
    state = region.split(".")[-1]
    table: dict[str, list[str]] = {
        "nws":        [f"central.wx.alert.{region}.>"],
        # WFIGS (fires): active + removal tombstones. v0.5.7-fire added the
        # two removed.<state> subjects so fall-off signals reach meshai.
        "fires":      [f"central.fire.incident.{state}.>",
                       f"central.fire.perimeter.{state}.>",
                       f"central.fire.incident.removed.{state}",
                       f"central.fire.perimeter.removed.{state}"],
        # FIRMS: Central publishes central.fire.hotspot.<satellite>.<confidence>
        # with NO region in the subject. Tail-only `>` is the only NATS-legal
        # subscription that covers all combinations; client-side filters lat/lon.
        "firms":      ["central.fire.hotspot.>"],
        # USGS quake: Central publishes central.quake.event.<tier> with NO
        # region in the subject (per guide §usgs_quake). Same situation as
        # FIRMS -- tail-only `>` is the legal form; client-side filters lat/lon.
        "usgs_quake": ["central.quake.event.>"],
        # USGS NWIS hydro: 3 single-token wildcards for <param>.<agency>.<site>
        # + bare region tail. Pre-v0.5.7-water shipped `central.hydro.>.<region>`
        # which is invalid NATS (`>` only legal at the tail). Verified against
        # the v0.10.0-itd-511 nwis.py producer subject_for() body which
        # publishes `central.hydro.<param>.<agency>.<site>.<region>`.
        "usgs":       [f"central.hydro.*.*.*.{region}",
                       "central.hydro.*.*.*.unknown"],
        # SWPC space weather: planetary (no region). The umbrella subject
        # central.space.> catches all three SWPC adapters per Central v0.10.0
        # guide §swpc_alerts/§swpc_kindex/§swpc_protons:
        #   - swpc_alerts:  central.space.alert.<product_id>
        #   - swpc_kindex:  central.space.kindex          (fixed)
        #   - swpc_protons: central.space.proton_flux     (fixed)
        # All three publish severity=0 by default (verified against the
        # live samples in the guide); map_severity(0) -> "routine", which
        # routes through the NotificationToggle's "routine" severity_channels
        # entry (dict is string-keyed, no IndexError risk).
        "swpc":       ["central.space.>"],
        # Convention B (bare state) — shared by traffic family (wzdx,
        # tomtom_incidents, state_511_atis). Single-token `*` matches the
        # event_type slot; `>` was illegal here.
        "traffic":    [f"central.traffic.*.{state}"],
        # roads511 dual-subscribes: bare state (shared with traffic) + the
        # us.<state> form that the new itd_511 Idaho-only adapter publishes
        # (Convention A). Sub-adapter routing (_subject_owned) keeps the
        # shared bare-state subject scoped to both source names.
        "roads511":   [f"central.traffic.*.{state}",
                       f"central.traffic.*.{region}"],
    }
    return list(table.get(adapter, []))

# Bridge between Central's adapter taxonomy and meshai's family-tab source names.
# Central names some adapters differently (e.g. "wfigs_incidents" vs meshai's
# "fires"); remap so dashboard per-adapter event filtering (which keys on the
# native source name) works whether a feed is native or central. 1:1 names
# (nws, usgs_quake, firms) are intentionally omitted -> passthrough.
CENTRAL_ADAPTER_TO_SOURCE: dict[str, str] = {
    "wfigs_incidents": "fires",
    "wfigs_perimeters": "fires",
    "nwis": "usgs",
    "swpc_alerts": "swpc",
    "swpc_kindex": "swpc",
    "swpc_protons": "swpc",
    "wzdx": "traffic",
    "tomtom_incidents": "traffic",
    "state_511_atis": "roads511",
    # v0.5.7-traffic: itd_511 is the new Idaho-only Central adapter
    # (Convention A publishing). Routes to meshai's roads511 source so
    # ALERT_CATEGORIES roads-family rules cover both 511 feeds. A future
    # v0.6 may split them; for now collapsed for UX simplicity.
    "itd_511": "roads511",
    "firms": "firms",
}

# Central hierarchical category prefix -> meshai flat category.
# First matching prefix wins; order matters (most specific first).
_CATEGORY_MAP: list[tuple[str, str]] = [
    ("wx.alert", "weather_warning"),
    ("wx.", "weather_statement"),
    ("fire.hotspot", "wildfire_hotspot"),
    ("fire.incident", "wildfire_incident"),
    ("fire.perimeter", "wildfire_incident"),
    ("fire.", "wildfire_incident"),
    ("quake.", "earthquake_event"),
    ("hydro.", "stream_flow"),
    ("space.alert", "rf_propagation_alert"),
    ("space.kindex", "geomagnetic_storm"),
    ("space.proton", "solar_radiation_storm"),
    ("space.", "geomagnetic_storm"),
    ("disaster.", "disaster_event"),
    ("traffic_flow", "traffic_flow"),
    ("traffic_cameras", "traffic_camera"),
    # v0.5.7-traffic: preserve traffic event_type distinctions instead of
    # flattening to traffic_congestion. Central publishes category strings
    # like "work_zone.wzdx", "incident.tomtom_incidents", "closure" (raw
    # from state_511_atis / itd_511). startswith() catches both the bare
    # form and the ".<adapter>" suffixed form.
    ("work_zone", "work_zone"),
    ("incident", "road_incident"),
    ("closure", "road_closure"),
    ("traffic.", "traffic_congestion"),
]


def map_category(central_category: str) -> str:
    """Map Central's hierarchical category string to a meshai flat category."""
    cat = central_category or ""
    for prefix, flat in _CATEGORY_MAP:
        if cat.startswith(prefix):
            return flat
    return "other"


# Subject-domain fallback: some Central categories are not domain-prefixed
# (e.g. traffic's "work_zone.wzdx"), so when the category table misses we map by
# the stable subject domain token (central.<domain>.<...>) instead of "other".
_SUBJECT_DOMAIN_CATEGORY = {
    "wx": "weather_warning",
    "fire": "wildfire_incident",
    "quake": "earthquake_event",
    "hydro": "stream_flow",
    "space": "geomagnetic_storm",
    "disaster": "disaster_event",
    "traffic": "traffic_congestion",
    "traffic_flow": "traffic_flow",
    "traffic_cameras": "traffic_camera",
}


def category_from_subject(subject: str) -> Optional[str]:
    """Map a NATS subject (central.<domain>.<...>) to a meshai category."""
    parts = (subject or "").split(".")
    if len(parts) >= 2 and parts[0] == "central":
        return _SUBJECT_DOMAIN_CATEGORY.get(parts[1])
    return None


def map_severity(sev: Optional[int]) -> str:
    """Central int severity (0-4 / None) -> meshai severity string.

    0|1 -> routine, 2 -> priority, 3|4 -> immediate, None -> routine.

    The `sev >= 3` branch is intentionally a high-side CLAMP, not an
    equality: any out-of-range value (5+ -- e.g. a hypothetical future
    "great quake" severity that exceeds the documented 0-4 vocabulary, or
    a malformed upstream value) maps to "immediate" (meshai's highest
    severity bucket). Non-int / negative / NaN inputs degrade safely to
    "routine" via the try/except. Downstream NotificationToggle.severity_channels
    is dict-keyed by severity STRING ({"routine","priority","immediate"})
    not int -- so no IndexError can ever propagate from this boundary.
    """
    if sev is None:
        return "routine"
    try:
        sev = int(sev)
    except (TypeError, ValueError):
        return "routine"
    if sev >= 3:  # 3, 4, or any 5+ great-quake / malformed value
        return "immediate"
    if sev == 2:
        return "priority"
    return "routine"


def _parse_time(s) -> Optional[float]:
    """Parse a Central ISO-8601 timestamp to epoch seconds."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class CentralConsumer:
    """Subscribes to Central JetStream subjects and emits normalized Events."""

    def __init__(self, env_config, event_bus):
        """Args:
        env_config: the EnvironmentalConfig (provides .central + per-adapter .source)
        event_bus: the pipeline EventBus to emit normalized Events onto
        """
        self._env = env_config
        self._central = getattr(env_config, "central", None)
        self._bus = event_bus
        self._nc = None
        self._js = None
        self._subs: list = []

    # ---- subject derivation ----
    def _region(self) -> str:
        """Active Central region (v0.5.4). Empty string = pre-v0.9.20 bare wildcards."""
        if self._central is None:
            return ""
        return getattr(self._central, "region", "") or ""

    def _subject_owned(self) -> dict:
        """Map each Central subject filter -> set of meshai source names (adapter
        attrs) that are feed_source=central and consume it. A shared subject
        (central.traffic.>.id for both traffic and roads511) carries multiple
        owned sources; _handle drops events whose remapped source isn't in the
        set. v0.5.4: subject shapes are region-aware via _subjects_for()."""
        region = self._region()
        owned: dict = {}
        for attr in _SUBJECTS_BARE.keys():
            cfg = getattr(self._env, attr, None)
            if cfg is not None and getattr(cfg, "feed_source", "native") == "central":
                for subj in _subjects_for(attr, region):
                    owned.setdefault(subj, set()).add(attr)
        for attr in ("avalanche", "ducting"):
            cfg = getattr(self._env, attr, None)
            if cfg is not None and getattr(cfg, "feed_source", "native") == "central":
                logger.warning("Adapter %r set to source=central but Central has no "
                               "matching stream; nothing will be consumed for it.", attr)
        return owned

    def subjects(self) -> list[str]:
        """Unique Central subject filters for adapters set to central."""
        return sorted(self._subject_owned().keys())

    def _make_cb(self, owned):
        async def _cb(msg):
            await self._on_message(msg, owned)
        return _cb

    # ---- normalization ----
    def _normalize(self, subject: str, envelope: dict) -> Optional[Event]:
        """CloudEvents envelope -> meshai Event (None if unusable)."""
        inner = envelope.get("data") or {}
        env_id = envelope.get("id") or inner.get("id")
        if not env_id:
            return None

        # v0.5.7-fire: tombstone detection now matches both the legacy GDACS
        # `<id>:removed` form and the WFIGS `<IrwinID>:removed:<iso>` form.
        is_tombstone = (
            (".removed." in (subject or ""))
            or str(env_id).endswith(":removed")
            or ":removed:" in str(env_id)
        )
        # The clear event shares the ORIGINAL event's group_key so the grouper/
        # inhibitor lets the prior event lapse naturally. v0.5.7-fire: strip
        # both `:removed` (GDACS) AND `:removed:<iso_now>` (WFIGS) tails. Per
        # Central v0.10.0 guide §wfigs_incidents, the same incident may be
        # tombstoned multiple times over its lifecycle; each tombstone is a
        # distinct Event but they all share the IrwinID as group_key.
        group_key = str(env_id)
        if is_tombstone:
            group_key = re.sub(r":removed(:.*)?$", "", group_key)

        cat_raw = inner.get("category") or envelope.get("centralcategory") or ""
        category = map_category(cat_raw)
        if category == "other":
            category = category_from_subject(subject) or "other"

        geo = inner.get("geo") or {}
        lat = lon = None
        centroid = geo.get("centroid")
        if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
            lon, lat = centroid[0], centroid[1]  # GeoJSON [lon, lat] -> (lat, lon)

        # Preserve the upstream payload verbatim (incl. `_enriched`) in Event.data.
        data = dict(inner.get("data") or {})
        if is_tombstone:
            data["_central_tombstone"] = True
            # v0.5.7-fire: stash the full env_id (with the :removed:<iso> tail)
            # so downstream consumers can tell apart multiple tombstones for
            # the same incident. The group_key collapses to the bare IrwinID
            # by design (so they lapse the original together); this preserves
            # lifecycle distinctness for accounting.
            data["_central_tombstone_id"] = str(env_id)

        # v0.5.7-regression: upstream Central payloads for most adapters
        # (firms, nwis, swpc_*, wfigs_*, tomtom_incidents, ...) carry per-
        # adapter fields but NOT a top-level `title` or `headline`. Falling
        # back to `cat_raw` produced category-as-title broadcasts that
        # leaked the raw Central hierarchical category onto the mesh
        # (e.g. "incident.tomtom_incidents" instead of "Road Incident").
        # Prefer the meshai-friendly registry name from get_category() over
        # the raw category. cat_raw stays as the last-resort tail so
        # genuinely-unknown categories still produce *something* readable.
        friendly_name = None
        try:
            ci = get_category(category)
            if ci and ci.get("name"):
                friendly_name = str(ci["name"])
        except Exception:
            pass

        # v0.5.8 (first per-adapter normalizer): state_511_atis work_zone /
        # closure / incident events get a rich one-line title synthesized by
        # the meshai.central_normalizer module + the work_zone renderer.
        # Failures / unmapped adapters fall through to the registry-friendly
        # name chain below.
        synthesized = None
        try:
            from meshai.central_normalizer import normalize as _norm_envelope
            from meshai.notifications.renderers.work_zone import format_work_zone_mesh
            # v0.5.9 unified incident pipeline -- tomtom_incidents +
            # state_511_atis + itd_511 for incident/closure/special_event
            # categories all flow through meshai.central.incident_handler.
            # state_511_atis with category=work_zone stays on the v0.5.8
            # _parse_state_511_atis path below.
            _adapter_v9 = inner.get("adapter") or ""
            if (
                _adapter_v9 in ("tomtom_incidents", "state_511_atis", "itd_511")
                and (cat_raw.startswith("incident.")
                      or cat_raw.startswith("closure.")
                      or cat_raw.startswith("special_event."))
            ):
                from meshai.central.incident_handler import handle_incident
                synthesized = handle_incident(envelope, subject, data=data) or None
            else:
                # v0.5.9 GAMMA: state_511_atis Idaho cutover via helper.
                # Applies BEFORE normalize+dispatch so neither the work_zone
                # renderer nor the incident_handler ever sees an ID-state_511
                # envelope.
                from meshai.central_normalizer import (
                    should_skip_state_511_atis_id as _skip_s5_id,
                )
                # v0.5.9 GAMMA universal freshness gate -- applies to ALL
                # incident-pipeline adapters BEFORE dispatch, so itd_511
                # work_zone (which goes through central_normalizer +
                # format_work_zone_mesh, NOT handle_incident) is now also
                # gated. Per-source field paths defined in the helper.
                from meshai.central_normalizer import (
                    is_incident_envelope_stale as _stale_check,
                )
                if _stale_check(envelope, now=int(time.time())):
                    try:
                        from meshai.persistence import get_db as _get_db_st
                        _conn_st = _get_db_st()
                        _conn_st.execute(
                            "INSERT INTO event_log(received_at, source, "
                            "category, severity_word, event_id_external, "
                            "nats_subject, handled, table_name, table_pk) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (int(time.time()),
                             inner.get("adapter") or "",
                             cat_raw + "|freshness_drop", None,
                             inner.get("id"),
                             subject, 0, None, None),
                        )
                    except Exception:
                        logger.exception("freshness_drop log failed")
                    synthesized = None
                    n = None
                elif _skip_s5_id(envelope):
                    try:
                        from meshai.persistence import get_db as _get_db_skip
                        _conn_skip = _get_db_skip()
                        _conn_skip.execute(
                            "INSERT INTO event_log(received_at, source, "
                            "category, severity_word, event_id_external, "
                            "nats_subject, handled, table_name, table_pk) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (int(time.time()), "state_511_atis",
                             cat_raw + "|skip_id", None,
                             inner.get("id"),
                             subject, 0, None, None),
                        )
                    except Exception:
                        logger.exception("state_511_id_skip log failed")
                    synthesized = None
                    n = None  # short-circuit downstream dispatch
                else:
                    n = _norm_envelope(envelope)
                # v0.5.8 wfigs_handler dispatch -- WFIGS events route through
                # the persistence-backed change-detection handler (which also
                # logs to event_log for tombstones + perimeters). Other adapters
                # with a normalized dict (state_511_atis, wzdx) flow through the
                # work_zone renderer as before.
                if n is not None and str(n.get("_kind", "")).startswith("wfigs"):
                    from meshai.central.wfigs_handler import handle_wfigs
                    synthesized = handle_wfigs(n, envelope, subject, data=data) or None
                # v0.5.10 nws + usgs_quake + swpc handlers. Adapter-specific
                # filters: NWS severity gate, quake magnitude+Idaho-distance,
                # SWPC G3+/R3+/S1+ NOAA scales. Universal freshness gate above
                # already dropped stale envelopes per central_normalizer.
                elif inner.get("adapter") == "nws":
                    from meshai.central.nws_handler import handle_nws
                    synthesized = handle_nws(envelope, subject, data=data) or None
                elif inner.get("adapter") == "usgs_quake":
                    from meshai.central.quake_handler import handle_quake
                    synthesized = handle_quake(envelope, subject, data=data) or None
                elif inner.get("adapter") in ("swpc_alerts", "swpc_kindex", "swpc_protons"):
                    from meshai.central.swpc_handler import handle_swpc
                    synthesized = handle_swpc(envelope, subject, data=data) or None
                # v0.5.12 nwis stream-gauge handler. Filters to the
                # 9-site Idaho curation (idaho_gauge_sites.py); upward
                # threshold crossings only (mirrors WFIGS forward-only).
                elif inner.get("adapter") == "nwis":
                    from meshai.central.nwis_handler import handle_nwis
                    synthesized = handle_nwis(envelope, subject, data=data) or None
                elif n is not None and category in ("work_zone", "road_closure", "road_incident"):
                    synthesized = format_work_zone_mesh(n) or None
        except Exception:
            logger.exception("normalizer/renderer failed for adapter=%s category=%s",
                             inner.get("adapter"), category)
            synthesized = None

        # v0.5.13 default-deny: per-adapter handlers gate broadcasts, not
        # just titles. If no handler synthesized a wire string for this
        # envelope (either because no per-adapter handler matched OR a
        # matched handler explicitly returned None as a filter/dedup/
        # threshold decision), return None from _normalize() -- the Event
        # never enters the bus and the dispatcher never fires. This is
        # the architectural fix for the v0.5.7-regression leak that came
        # back through the v0.5.x live flip: handlers were gating titles
        # but not broadcasts. See memory rule 19.
        #
        # Scheduled broadcasters (band_conditions) bypass _normalize()
        # entirely -- they enter via Dispatcher.dispatch_scheduled_broadcast()
        # and are unaffected by this gate.
        if synthesized is None:
            logger.debug(
                "consumer: default-deny -- no handler synthesized for "
                "adapter=%s category=%s subject=%s",
                inner.get("adapter"), category, subject,
            )
            return None

        title = synthesized

        # v0.5.8 Option A: when the per-adapter normalizer produced a fully
        # formatted mesh string, set a marker on event.data so the composer
        # at dispatch time can pass it through verbatim (no family prefix,
        # no region tail, no severity append).
        if synthesized and title == synthesized:
            data["_meshai_precomposed"] = True

        kwargs = dict(
            title=str(title)[:200],
            summary="",
            lat=lat,
            lon=lon,
            region=geo.get("primary_region"),
            regions=geo.get("regions") or [],
            group_key=group_key,
            inhibit_keys=[group_key],
            data=data,
        )
        ts = _parse_time(inner.get("time"))
        if ts is not None:
            kwargs["timestamp"] = ts
        exp = _parse_time(inner.get("expires"))
        if exp is not None:
            kwargs["expires"] = exp

        raw_adapter = inner.get("adapter") or "central"
        source = CENTRAL_ADAPTER_TO_SOURCE.get(raw_adapter, raw_adapter)
        if source != raw_adapter:
            logger.debug("Central adapter %r -> meshai source %r", raw_adapter, source)
        return make_event(
            source=source,
            category=category,
            severity=map_severity(inner.get("severity")),
            **kwargs,
        )

    def _handle(self, subject: str, raw: bytes, owned=None) -> Optional[Event]:
        """Normalize a raw message body and emit to the bus. Returns the Event.

        owned: set of meshai source names this subscription may emit (sub-adapter
        routing for shared subjects); None = no filtering.
        """
        try:
            envelope = json.loads(raw)
        except Exception:
            logger.exception("CentralConsumer: bad JSON on %s", subject)
            return None
        event = self._normalize(subject, envelope)
        if event is None:
            return None
        if owned is not None and event.source not in owned:
            logger.debug("CentralConsumer: dropping %s source=%s -- not owned by "
                         "subscription %s", subject, event.source, sorted(owned))
            return None
        if self._bus is not None:
            self._bus.emit(event)
        return event

    async def _on_message(self, msg, owned=None) -> None:
        """JetStream callback: normalize + emit, then ack."""
        try:
            self._handle(msg.subject, msg.data, owned)
        except Exception:
            logger.exception("CentralConsumer: handler failed on %s",
                             getattr(msg, "subject", "?"))
        finally:
            ack = getattr(msg, "ack", None)
            if ack is not None:
                try:
                    await ack()
                except Exception:
                    pass

    # ---- lifecycle ----
    async def start(self) -> None:
        subject_owned = self._subject_owned()
        if not subject_owned:
            logger.info("CentralConsumer started; 0 subjects subscribed -- "
                        "no adapters set to central")
            return
        if self._central is None or not getattr(self._central, "enabled", False):
            logger.warning("CentralConsumer: adapter(s) want source=central but "
                           "environmental.central.enabled is false; not subscribing: %s",
                           sorted(subject_owned))
            return

        region = self._region()
        logger.info("CentralConsumer: connecting region=%r subjects=%s",
                    region or "(bare wildcards)", sorted(subject_owned))
        import nats  # lazy: no NATS dependency at boot unless actually consuming
        self._nc = await nats.connect(
            self._central.url,
            connect_timeout=getattr(self._central, "connect_timeout", 10.0),
        )
        self._js = self._nc.jetstream()
        for subj, owned in subject_owned.items():
            durable = self._central.durable + "-" + re.sub(r"[^a-z0-9]+", "_", subj.lower())
            sub = await self._js.subscribe(
                subj, durable=durable, cb=self._make_cb(owned), config=consumer_config())
            self._subs.append(sub)
            logger.info("CentralConsumer subscribed %s owned-sources=%s", subj, sorted(owned))
        logger.info("CentralConsumer started; %d subjects subscribed", len(subject_owned))

    async def stop(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass
            try:
                await self._nc.close()
            except Exception:
                pass
            self._nc = None
            self._js = None
            self._subs = []
