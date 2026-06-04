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
from datetime import datetime
from typing import Optional

from meshai.notifications.events import Event, make_event

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
      - region AFTER  the wildcard (quake / firms / usgs / traffic):
            central.quake.event.>.us.id
            central.fire.hotspot.>.us.id
            central.hydro.>.us.id      (+ ".unknown" workaround, see below)
            central.traffic.>.id        (state only, no us. prefix)
      - state-only token at a fixed depth (fires):
            central.fire.incident.<state>.>
            central.fire.perimeter.<state>.>
      - region ignored (swpc) — space weather is planetary.

    The .unknown workaround: v0.9.20 leaves USGS hydro events whose gauge
    state can't be inferred on `central.hydro.>.unknown`. Subscribing to
    both avoids losing those rows until v0.9.20.1 backfills the state tag.

    Empty/None region returns the bare-wildcard form (v0.5.3 behaviour).
    Adapters without a Central equivalent (avalanche, ducting) return [].
    """
    if not region:
        return list(_SUBJECTS_BARE.get(adapter, []))
    state = region.split(".")[-1]
    table: dict[str, list[str]] = {
        "nws":        [f"central.wx.alert.{region}.>"],
        "fires":      [f"central.fire.incident.{state}.>",
                       f"central.fire.perimeter.{state}.>"],
        "firms":      [f"central.fire.hotspot.>.{region}"],
        "usgs_quake": [f"central.quake.event.>.{region}"],
        "usgs":       [f"central.hydro.>.{region}",
                       "central.hydro.>.unknown"],
        "swpc":       ["central.space.>"],
        "traffic":    [f"central.traffic.>.{state}"],
        "roads511":   [f"central.traffic.>.{state}"],   # shared with traffic
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
    """
    if sev is None:
        return "routine"
    try:
        sev = int(sev)
    except (TypeError, ValueError):
        return "routine"
    if sev >= 3:
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

        is_tombstone = (".removed." in (subject or "")) or str(env_id).endswith(":removed")
        # The clear event shares the ORIGINAL event's group_key so the grouper/
        # inhibitor lets the prior event lapse naturally.
        group_key = str(env_id)
        if is_tombstone:
            group_key = re.sub(r":removed$", "", group_key)

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

        title = (data.get("title") or data.get("headline")
                 or cat_raw or f"{inner.get('adapter', 'central')} event")

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
