"""Environmental data store with tick-based adapter polling."""

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..config import EnvironmentalConfig
    from ..notifications.pipeline import EventBus

logger = logging.getLogger(__name__)

# ── Received-delta key format ────────────────────────────────────────────────
# A single, shared key format used by BOTH the live emit path (_seen_key) and
# the durable startup pre-seed (_seed_from_persistent). Keeping the two on one
# helper guarantees they can never drift apart — a drift would silently defeat
# the pre-seed (keys wouldn't match), which is exactly the leak we are fixing.
_SEP = "\x1e"


def _key_ext(source: str, external_id) -> str:
    """Seen-key for an item identified by its upstream external_id."""
    return f"{source}{_SEP}ext:{external_id}"


def _key_eid(source: str, event_id) -> str:
    """Seen-key for an item identified by the adapter's stable event_id."""
    return f"{source}{_SEP}eid:{event_id}"


class EnvironmentalStore:
    """Cache and tick-driver for all environmental feed adapters."""

    def __init__(
        self,
        config: "EnvironmentalConfig",
        region_anchors: list = None,
        event_bus: Optional["EventBus"] = None,
        coverage_bbox: list = None,
        coverage_excluded: list = None,
        generic_sources: list = None,
    ):
        # Config-driven REST/GeoJSON sources (top-level config.generic_sources)
        # for the universal GenericHttpAdapter. Plain list of dicts.
        self._generic_sources = generic_sources or []
        self._adapters = {}  # name -> adapter instance
        self._failed_adapters = {}  # name -> last_error string
        self._events = {}  # (source, event_id) -> event dict
        self._event_bus = event_bus  # Pipeline EventBus for emission
        self._swpc_status = {}  # Kp/SFI/scales snapshot
        self._ducting_status = {}  # tropo ducting assessment
        self._mesh_zones = config.nws_zones or []
        self._region_anchors = region_anchors or []
        self._coverage_bbox = coverage_bbox or []
        self._coverage_excluded = set(coverage_excluded or [])

        # ── Received-delta gate (NATIVE-only) ────────────────────────────
        # The model the operator demanded: a native adapter broadcasts an item
        # ONLY when it was newly RECEIVED from the API this poll — never by
        # scanning an accumulated backlog. Two layers enforce this:
        #
        #   1. DURABLE pre-seed (_seed_from_persistent, below). meshai already
        #      has a durable record of everything it has ever received: the
        #      persistent hazard tables. At startup we load the identifying
        #      keys of every already-received item into `self._seen`, so nothing
        #      ever received can re-broadcast — immune to fetch staging AND to
        #      restarts. Keyed by the event's `source` (NOT adapter name) so the
        #      seed and the live-emit key line up on the same value.
        #   2. IN-MEMORY first-poll seed. The FIRST *non-empty* poll for a
        #      source records every current key as "seen" and emits NOTHING
        #      (that batch is pre-existing backlog); later polls emit only keys
        #      not already seen. Belt-and-suspenders: a source is marked
        #      "seeded" ONLY after a non-empty ingest, so an empty first poll
        #      (e.g. wzdx's registry-only tick) can never mark it seeded and
        #      then leak the real batch on the next tick.
        #
        # `self._seen` is keyed by SOURCE (evt["source"]), matching the durable
        # pre-seed. Durable keys added at startup are never removed, so they
        # suppress a backlog item even AFTER the source is marked seeded (this
        # is what closes cross-tick, non-empty staging leaks).
        self._seen: dict[str, set] = {}   # source -> set of item keys
        self._seeded: set[str] = set()    # sources past their first non-empty poll

        # Native WFIGS cold-start silent-seed gate (see _ingest_fires). The
        # FIRST fires poll after boot treats every fire present as already-known
        # (seed silently, no broadcast) so a fresh deploy never dumps the active-
        # fire backlog to the mesh. Gated on this per-process flag rather than a
        # wall-clock boot grace so it holds no matter how late the first
        # successful WFIGS fetch lands (e.g. a failed first fetch pushes the
        # first real poll ~10min out, well past any grace window). Flag flips
        # only after a non-empty fires ingest; a fire that first appears on a
        # LATER poll is a genuine ignition and broadcasts "New".
        self._fires_seeded: bool = False

        # Generic-source cold-start silent-seed gate (PER source name), mirror
        # of _fires_seeded. The FIRST non-empty poll for a given generic source
        # seeds every current item (records it seen + persists it) and emits
        # NOTHING — that batch is pre-existing backlog. Items that first appear
        # on a LATER poll broadcast. Keyed by the bare configured source name.
        self._generic_seeded: set[str] = set()

        # Create adapter instances with error isolation
        self._register_adapter("nws", config.nws, ".nws", "NWSAlertsAdapter",
            lambda cfg: (cfg, self._coverage_for("nws")))
        self._register_adapter("swpc", config.swpc, ".swpc", "SWPCAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("ducting", config.ducting, ".ducting", "DuctingAdapter",
            lambda cfg: (cfg, self._coverage_for("ducting")))
        self._register_adapter("nifc", config.fires, ".fires", "NICFFiresAdapter",
            lambda cfg: (cfg, self._region_anchors, self._coverage_for("fires")))
        self._register_adapter("avalanche", config.avalanche, ".avalanche", "AvalancheAdapter",
            lambda cfg: (cfg, self._coverage_for("avalanche")))
        self._register_adapter("usgs", config.usgs, ".usgs", "USGSStreamsAdapter",
            lambda cfg: (cfg, self._coverage_for("usgs")))
        self._register_adapter("usgs_quake", config.usgs_quake, ".usgs_quake", "USGSQuakeAdapter",
            lambda cfg: (cfg, self._coverage_for("usgs_quake")))
        self._register_adapter("traffic", config.traffic, ".traffic", "TomTomTrafficAdapter",
            lambda cfg: (cfg, self._coverage_for("traffic")))
        self._register_adapter("roads511", config.roads511, ".roads511", "Roads511Adapter",
            lambda cfg: (cfg, self._coverage_for("roads511")))
        self._register_adapter("wzdx", config.wzdx, ".wzdx", "WZDxAdapter",
            lambda cfg: (cfg, self._coverage_for("wzdx")))
        # Native satpass TLE fetcher (storage-only: populates sat_tles, emits
        # no events). Gated on satpass.feed_source=="native" like the rest.
        self._register_adapter("satpass_tle", config.satpass, ".tle_fetch", "TLEFetchAdapter",
            lambda cfg: (cfg,))
        # Native SGP4 pass predictor (broadcasts consolidated passes locally,
        # no Central dependency). SEPARATE from the satpass_tle fetcher above;
        # both are gated on satpass.enabled and feed_source=="native".
        self._register_adapter("satpass", config.satpass, ".satpass", "SatpassAdapter",
            lambda cfg: (cfg,))

        # FIRMS needs reference to NIFC adapter for cross-referencing
        if config.firms.enabled and config.firms.feed_source == "native":
            try:
                from .firms import FIRMSAdapter
                fires_adapter = self._adapters.get("nifc")
                self._firms = FIRMSAdapter(config.firms, self._region_anchors, fires_adapter, coverage=self._coverage_for("firms"))
                self._adapters["firms"] = self._firms
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                logger.warning("Failed to initialize firms adapter: %s", err_msg)
                self._failed_adapters["firms"] = err_msg

        # Universal config-driven REST/GeoJSON sources. ONE adapter instance
        # handles every source in config.generic_sources; construct it only
        # when at least one source is configured. Guarded like firms so a bad
        # import/config never takes the whole store down.
        if self._generic_sources:
            try:
                from .generic_http import GenericHttpAdapter
                self._generic = GenericHttpAdapter(
                    self._generic_sources,
                    self._coverage_for("generic_http"))
                self._adapters["generic_http"] = self._generic
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                logger.warning("Failed to initialize generic_http adapter: %s", err_msg)
                self._failed_adapters["generic_http"] = err_msg

        _central = [n for n in ("nws", "swpc", "ducting", "fires", "avalanche", "usgs", "usgs_quake", "traffic", "roads511", "wzdx", "firms", "satpass")
                    if getattr(getattr(config, n, None), "feed_source", "native") == "central"]
        if _central:
            logger.debug("Adapters sourced from Central (native skipped): %s", _central)
        if self._failed_adapters:
            logger.warning("Failed adapters: %s", list(self._failed_adapters.keys()))
        logger.info(f"EnvironmentalStore initialized with {len(self._adapters)} adapters")

        # Durable pre-seed: load every already-received item key from the
        # persistent hazard tables into `self._seen` so nothing ever received
        # can re-broadcast, regardless of fetch staging or process restarts.
        self._seed_from_persistent()


    def _register_adapter(self, name: str, cfg, module_path: str, class_name: str, args_fn):
        """Register a single adapter with error isolation."""
        if not cfg.enabled or cfg.feed_source != "native":
            return
        try:
            module = __import__(f"meshai.env{module_path}", fromlist=[class_name])
            cls = getattr(module, class_name)
            self._adapters[name] = cls(*args_fn(cfg))
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.warning("Failed to initialize %s adapter: %s", name, err_msg)
            self._failed_adapters[name] = err_msg

    def _coverage_for(self, adapter: str):
        """Derived coverage scope for a NATIVE adapter, or None to use its own config (override/fallback).

        Returns None immediately for any adapter listed in coverage_excluded, so
        that adapter falls back to its own config fields exactly as if no coverage
        bbox were set (the "advanced override" escape hatch).
        """
        if adapter in self._coverage_excluded:
            return None
        from meshai import coverage as _cov
        return _cov.resolve_adapter_coverage(adapter, self._coverage_bbox, "native")

    def refresh(self) -> bool:
        """Called every second from main loop. Ticks each adapter.

        Returns:
            True if any data changed
        """
        changed = False
        for name, adapter in self._adapters.items():
            try:
                if adapter.tick():
                    changed = True
                    self._ingest(name, adapter)
            except Exception as e:
                logger.warning("Env adapter %s error: %s", name, e)

        self._purge_expired()
        return changed

    def _seen_key(self, raw_evt: dict) -> str:
        """Derive a STABLE per-item key for the received-delta gate.

        Stability across polls is the whole point: the SAME real-world item
        must produce the SAME key every poll, or it would look "newly
        received" forever and re-broadcast on each tick. Preference order,
        most→least explicit id:
          1. external_id — the upstream feed's own stable id (e.g. WZDx)
          2. event_id    — the adapter's stable per-item id; every native raw
             event carries one (the store's own dedup already keys on it, and
             the adapters build it from natural ids: USGS quake id, 511 event
             id, NWS alert id, ``swpc_<scale><level>``, ``ducting_<tier>_<loc>``,
             ``avy_<center>_<zone>``, etc. — all stable across polls).
          3. content hash — last-resort fallback if an item somehow carries no
             id at all.

        Namespaced by the event's ``source`` (NOT the adapter name). The
        persistent hazard tables key on ``source`` (e.g. ``wzdx``), so keying
        the live-emit path on the same value is what lets the durable startup
        pre-seed line up with what the adapter emits. Every native raw event
        carries a stable ``source``; if one somehow does not, fall back to a
        neutral namespace so two feeds still never cross-contaminate.
        """
        source = raw_evt.get("source") or "?"
        ext = raw_evt.get("external_id")
        if ext:
            return _key_ext(source, ext)
        eid = raw_evt.get("event_id")
        if eid:
            return _key_eid(source, eid)
        blob = json.dumps(raw_evt, sort_keys=True, default=str)
        return f"{source}{_SEP}hash:" + hashlib.sha1(blob.encode()).hexdigest()[:16]

    def _delta_emit(self, name: str, adapter, raw_evt: dict, force: bool = False):
        """Received-delta gate — emit ONLY items newly received THIS poll.

        - First poll for ``name`` (name not yet in ``self._seeded``): record
          the item's key as seen and emit NOTHING. That batch is the
          pre-existing backlog.
        - Later polls: emit only when the key is not already in the seen-set
          (it just appeared upstream = "just received"); then record it.
        - ``force=True`` (avalanche danger-level rise) re-emits an already-seen
          item on a legitimate CONTENT change — but is IGNORED on the first
          poll, so a restart still can never replay the backlog.

        This replaces the native path's reliance on the deciders'
        broadcast-state tables for the "is this new" decision. Legitimate
        content filtering (severity/threshold/impact, decider gates) still runs
        downstream in ``_emit_event``.
        """
        source = raw_evt.get("source") or name
        seen = self._seen.setdefault(source, set())
        key = self._seen_key(raw_evt)
        first_poll = source not in self._seeded

        if first_poll:
            seen.add(key)            # seed silently — this is backlog
            return
        if key in seen and not force:
            return                   # already received (prior poll OR durable pre-seed)
        seen.add(key)

        if self._event_bus is not None and hasattr(adapter, "to_event"):
            self._emit_event(adapter, raw_evt)

    def _ingest(self, name: str, adapter):
        """Ingest data from an adapter after it ticks.

        Emission goes through the received-delta gate (``_delta_emit``): the
        adapter's FIRST data-bearing poll seeds the seen-set and broadcasts
        nothing; only items that newly appear on later polls are broadcast.
        ``self._events`` is still maintained for state/queries as before.

        Belt-and-suspenders: a source is marked "seeded" (past its first poll)
        ONLY after a NON-EMPTY ingest for that source. An empty poll — e.g.
        wzdx's registry-only tick that yields 0 events but returns ``tick()``
        True — must never mark a source seeded, or the next tick's real batch
        would be treated as "new" and leak. We collect the sources that
        actually carried ≥1 event this ingest and mark only those.
        """
        touched: set[str] = set()

        if name == "swpc":
            self._swpc_status = adapter.get_status()
            # Also ingest any alert events (R-scale >= 3)
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                touched.add(evt.get("source") or name)
                self._delta_emit(name, adapter, evt)
        elif name == "ducting":
            self._ducting_status = adapter.get_status()
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                touched.add(evt.get("source") or name)
                self._delta_emit(name, adapter, evt)
        elif name == "nifc":
            # Native WFIGS fires DELIBERATELY bypass the received-delta `_seen`
            # gate: a fire's growth updates are repeat sightings of the SAME
            # event_id that `_delta_emit` would wrongly suppress. The DECIDER
            # (gating.fire.decide, backed by the fires table + 8h cooldown) is
            # the gate instead. `_ingest_fires` also silent-seeds EVERY current
            # fire on the first poll (cold start) so boot never dumps a backlog.
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
            self._ingest_fires(adapter)
        elif name == "generic_http":
            # Universal config-driven sources: custom ingest with COLD-START-
            # SILENT per source name + persist-every-item (see _ingest_generic).
            self._ingest_generic(adapter)
        elif name == "avalanche":
            # Avalanche: re-emit on danger_level rise (Update:) not just new
            # events. The rise is a legitimate CONTENT change, so it passes
            # `force=True` — but the received-delta gate still suppresses it on
            # the first poll (backlog stays silent).
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                prior = self._events.get(key)
                prior_level = prior.get("danger_level", -1) if prior else -1
                level_rose = (prior is not None) and (
                    evt.get("danger_level", -1) > prior_level)
                evt["_is_update"] = level_rose   # signal to to_event()
                touched.add(evt.get("source") or name)
                self._delta_emit(name, adapter, evt, force=level_rose)
                self._events[key] = evt   # always update stored state
        else:
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                touched.add(evt.get("source") or name)
                self._delta_emit(name, adapter, evt)

        # First (non-empty) poll for these sources is now complete: later polls
        # may emit. Empty ingests touch nothing and so never mark a source
        # seeded — the leak-proof invariant behind the wzdx staging fix.
        self._seeded.update(touched)

    def _ingest_fires(self, adapter) -> None:
        """Native WFIGS fire ingest — Phase-3 growth-decider path.

        For each polled fire (already recorded in ``self._events`` by the
        caller):

          1. COLD-START silent-seed. On the FIRST fires poll after boot
             (``self._fires_seeded`` is False), EVERY first-sight fire (no
             ``fires`` row) is treated as a pre-existing active fire —
             regardless of age. We INSERT a ``fires`` row stamped as ALREADY
             broadcast (``last_broadcast_*`` = current) and emit NOTHING, so the
             decider treats later polls as Update and only fires on real growth
             — no boot backlog dump (a fresh deploy with an empty ``fires`` table
             must NOT broadcast every fire discovered in the last 48h). A fire
             only broadcasts "New" if it FIRST appears on a LATER poll (a
             genuine ignition since startup). The shared ``gating.fire.decide``
             is untouched. This is gated on the per-process first-poll flag, not
             a wall-clock boot grace, so it holds no matter how late the first
             successful WFIGS fetch lands.

          2. Unconditional current-state write (mirrors the Central
             ``wfigs_handler``): INSERT (``last_broadcast_*`` NULL) on first
             sight else UPDATE ``current_*``. This GUARANTEES a row exists so
             the decider's deferred ``commit`` UPSERT
             (``WHERE irwin_id=?``) persists ``last_broadcast_*`` — without it a
             genuine New would never latch and would re-broadcast every poll.

          3. Decider path via ``_emit_event``: runs ``gating.fire.decide``,
             applies ``gate.data_patch`` (``_dedup_suffix`` / ``_severity_override``
             / render hints) and arms ``gate.commit`` onto the emitted Event.
        """
        from meshai.notifications import clock as _clock

        now = _clock.now()
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception as e:
            logger.warning("nifc fire ingest skipped (DB unavailable): %s", e)
            return

        # Cold-start gate: is this the FIRST fires poll since boot? Captured
        # once for the whole batch, before the flag is flipped below, so every
        # fire in the initial full-state sweep is silent-seeded together.
        cold_start = not self._fires_seeded

        events = adapter.get_events()
        for evt in events:
            try:
                irwin_id = evt.get("irwin_id")
                if not irwin_id:
                    continue  # no stable identity -> not gate-able via fires
                acres = evt.get("acres")
                contained = evt.get("contained_pct")
                declared = evt.get("declared_at_epoch")

                row = conn.execute(
                    "SELECT last_broadcast_at FROM fires WHERE irwin_id=?",
                    (irwin_id,)).fetchone()
                first_sight = row is None

                # (1) Cold-start silent-seed — EVERY first-sight fire on the
                # first poll, regardless of age; seed as already-broadcast, no
                # emit.
                if first_sight and cold_start:
                    conn.execute(
                        "INSERT INTO fires(irwin_id, incident_name, "
                        "current_acres, current_contained_pct, lat, lon, "
                        "county, state, declared_at, last_event_at, "
                        "last_broadcast_at, first_broadcast_at, "
                        "last_broadcast_acres, last_broadcast_contained) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (irwin_id, evt.get("name"), acres, contained,
                         evt.get("lat"), evt.get("lon"), evt.get("county"),
                         evt.get("state"), declared, int(now),
                         int(now), int(now), acres, contained),
                    )
                    logger.info(
                        "nifc cold-start silent-seed irwin=%s acres=%s "
                        "(seeded already-broadcast, no mesh)", irwin_id, acres)
                    continue

                # (2) Unconditional current-state write so commit can latch.
                if first_sight:
                    conn.execute(
                        "INSERT INTO fires(irwin_id, incident_name, "
                        "current_acres, current_contained_pct, lat, lon, "
                        "county, state, declared_at, last_event_at, "
                        "last_broadcast_at, last_broadcast_acres, "
                        "last_broadcast_contained) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (irwin_id, evt.get("name"), acres, contained,
                         evt.get("lat"), evt.get("lon"), evt.get("county"),
                         evt.get("state"), declared, int(now),
                         None, None, None),
                    )
                else:
                    conn.execute(
                        "UPDATE fires SET current_acres=?, "
                        "current_contained_pct=?, lat=COALESCE(?, lat), "
                        "lon=COALESCE(?, lon), last_event_at=? "
                        "WHERE irwin_id=?",
                        (acres, contained, evt.get("lat"), evt.get("lon"),
                         int(now), irwin_id),
                    )

                # (3) Run the decider + emit (or suppress) via the shared path.
                if self._event_bus is not None and hasattr(adapter, "to_event"):
                    self._emit_event(adapter, evt)
            except Exception:
                logger.exception(
                    "nifc fire ingest failed for %s", evt.get("event_id", "?"))

        # First fires poll complete: later polls broadcast genuine ignitions.
        # `_ingest_fires` is only reached when the adapter's tick() reported a
        # change, which for the atomic WFIGS fetch means a non-empty batch on
        # the first successful poll (a 0-fire fetch is `changed=False` and never
        # ingests) — so this flip only ever happens on a real full-state sweep.
        if events:
            self._fires_seeded = True

    def _ingest_generic(self, adapter) -> None:
        """Native generic-source ingest — COLD-START-SILENT per source.

        Mirrors the ``_fires_seeded`` cold-start pattern, but PER configured
        source name:

          1. The FIRST non-empty poll for a given source seeds every current
             item into the received-delta seen-set and PERSISTS it, but emits
             NOTHING — that batch is pre-existing backlog.
          2. On later polls, only items whose key is not already seen (they
             newly appeared upstream = "just received") broadcast via
             ``_emit_event`` (which runs the coverage/decider path).
          3. EVERY item — seeded or new — is upserted into ``generic_events``
             so the LLM (env_reporter.build_generic_detail) sees it immediately.
             Dedup is by (source, event_id).

        The received-delta seen-set is namespaced by the event's ``source``
        (``generic:<name>``) so distinct sources never cross-contaminate.
        """
        conn = None
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception as e:
            logger.warning("generic ingest skipped persistence (DB unavailable): %s", e)

        # Group this poll's events by bare source name to apply the per-source
        # cold-start gate consistently across all of that source's items.
        by_source: dict = {}
        for evt in adapter.get_active():
            by_source.setdefault(evt.get("_source") or "?", []).append(evt)

        for src_name, items in by_source.items():
            cold_start = src_name not in self._generic_seeded
            for evt in items:
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt   # always track current state
                if conn is not None:
                    self._persist_generic(conn, evt)

                seen = self._seen.setdefault(evt["source"], set())
                seen_key = self._seen_key(evt)
                if cold_start:
                    seen.add(seen_key)     # silent seed — backlog, no emit
                    continue
                if seen_key in seen:
                    continue               # already received (prior poll)
                seen.add(seen_key)
                if self._event_bus is not None and hasattr(adapter, "to_event"):
                    self._emit_event(adapter, evt)
            # First non-empty poll for this source complete: later polls emit.
            if items:
                self._generic_seeded.add(src_name)

    def _persist_generic(self, conn, evt: dict) -> None:
        """Upsert one generic event into ``generic_events`` (source keyed by
        the bare configured name). Mirrors how _ingest_fires writes the fires
        table. Never fatal — a persistence error logs and continues."""
        now = int(time.time())
        try:
            conn.execute(
                "INSERT INTO generic_events(source, event_id, category, title, "
                "lat, lon, severity, data_json, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source, event_id) DO UPDATE SET "
                "category=excluded.category, title=excluded.title, "
                "lat=excluded.lat, lon=excluded.lon, "
                "severity=excluded.severity, data_json=excluded.data_json, "
                "last_seen=excluded.last_seen",
                (
                    evt.get("_source"), evt["event_id"], evt.get("category"),
                    evt.get("title"), evt.get("latitude"), evt.get("longitude"),
                    evt.get("severity"),
                    json.dumps(evt.get("data", {}), default=str),
                    now, now,
                ),
            )
        except Exception:
            logger.exception("generic persist failed for %s",
                             evt.get("event_id", "?"))

    def _seed_from_persistent(self) -> None:
        """Pre-seed ``self._seen`` from the durable hazard tables at startup.

        A row exists in these tables iff meshai has ALREADY RECEIVED that item,
        so loading their identifying keys guarantees a native adapter can never
        re-broadcast a previously-received item — immune to fetch staging and
        to process restarts. Durable keys are added to ``self._seen`` and,
        when ≥1 key is loaded for a source, that source is marked ``_seeded``
        so the durable set becomes its baseline: the very next poll emits ONLY
        items NOT already received (the operator's "newly received → send it
        now") and suppresses everything in the table — no silent first-poll
        needed, and no dependency on how the fetch is staged. Because durable
        keys are never removed, they keep suppressing backlog across every
        later poll, which is what closes cross-tick, non-empty staging leaks.
        (When a source has 0 durable rows we do NOT mark it seeded, so it falls
        back to the leak-proof in-memory silent first-poll seed — a fresh DB
        can never cause the first real batch to be treated as new.)

        Only sources whose native emit key PROVABLY equals the persistent key
        are seeded here (verified against the live schema + adapter code):

          * ``wzdx``       — the native WZDx adapter carries a stable
            ``external_id`` on its raw event and the incident decider persists
            it as ``traffic_events(source='wzdx', external_id)``. Same value on
            both sides → ``_key_ext('wzdx', external_id)`` matches exactly.
          * ``usgs_quake`` — the native adapter's raw ``event_id`` is the bare
            USGS id (e.g. ``us6000t9bn``) and the quake decider persists it as
            ``quake_events.event_id`` verbatim → ``_key_eid('usgs_quake', id)``
            matches exactly.
          * ``roads511``    — the native 511 adapter now carries a stable
            ``external_id`` (``511_{itd_id}``, EQUAL to its ``event_id``) on its
            raw event and the incident decider persists it as
            ``traffic_events(source='511', external_id='511_{itd_id}')``. Same
            value on both sides → ``_key_ext('511', external_id)`` matches
            exactly. Central-era rows used source ``itd_511`` with
            ``idaho_511:event:*`` ids — a DIFFERENT keyspace this pre-seed does
            NOT cover. On the first restart after this change there may be ~0
            ``source='511'`` rows yet; the layer-2 in-memory silent-first-poll
            seed covers any Central-era backlog in the interim (safe because
            roads511 fetches atomically per tick), and durability grows as
            native ``source='511'`` rows accumulate.

        DELIBERATELY NOT seeded here (native emit key ≠ any persistent key —
        see the report; these stay protected by the in-memory first-poll +
        non-empty-seed guard, which is sufficient because each fetches
        atomically per tick rather than across ticks):
          traffic (persistent rows are Central-keyed: tomtom_incidents with
          tomtom external_ids, but the native adapter emits source 'traffic'
          with derived event_ids),
          fires (native ``nifc_<name>_<state>`` vs persistent IRWIN GUID),
          firms (native ``firms_<lat>_<lon>_<date>_<time>`` — no matching PK),
          satpass (native ``<norad>:<bucket>`` vs persistent
          ``<norad>:<observer>:<bucket>``), nws (native CAP url vs persistent
          bare urn:oid), swpc (native ``swpc_<scale><level>`` vs timestamped
          persistent ids), usgs hydro (time-series, no per-item PK).

        Resilient by construction: each table is loaded in its own try/except
        so a missing/renamed table (or an unavailable DB) logs and continues —
        startup never crashes. The SELECTs are cheap key-only scans of bounded
        tables.
        """
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception as e:
            logger.warning("received-delta pre-seed skipped (DB unavailable): %s", e)
            return

        # (source, description, SQL, row->key) — one spec per verified source.
        specs = [
            (
                "wzdx",
                "traffic_events(source='wzdx')",
                "SELECT external_id FROM traffic_events "
                "WHERE source='wzdx' AND external_id IS NOT NULL",
                lambda row: _key_ext("wzdx", row[0]),
            ),
            (
                "511",
                "traffic_events(source='511')",
                "SELECT external_id FROM traffic_events "
                "WHERE source='511' AND external_id IS NOT NULL",
                lambda row: _key_ext("511", row[0]),
            ),
            (
                "usgs_quake",
                "quake_events",
                "SELECT event_id FROM quake_events WHERE event_id IS NOT NULL",
                lambda row: _key_eid("usgs_quake", row[0]),
            ),
        ]

        total = 0
        for source, desc, sql, key_fn in specs:
            try:
                seen = self._seen.setdefault(source, set())
                n = 0
                for row in conn.execute(sql).fetchall():
                    seen.add(key_fn(row))
                    n += 1
                total += n
                if n:
                    # Durable baseline established → skip the silent first-poll
                    # seed; the next poll emits only genuinely-new items.
                    self._seeded.add(source)
                logger.info(
                    "received-delta pre-seed: %d key(s) from %s -> source %r%s",
                    n, desc, source, " (marked seeded)" if n else "",
                )
            except Exception as e:
                # Missing/renamed table or bad row — never fatal at startup.
                logger.warning(
                    "received-delta pre-seed: %s failed (continuing): %s",
                    desc, e,
                )
        logger.info("received-delta pre-seed complete: %d durable key(s) loaded", total)

    def _emit_event(self, adapter, raw_evt: dict):
        """Convert raw event to pipeline Event and emit to bus.

        Phase-1 new-arch hook: if a gating decider is registered for the
        event's category (via meshai.notifications.gating.DECIDERS), it is
        called with event.data before the event reaches the bus.  The decider
        mirrors the Central-path gate so native and Central ingestion share
        identical broadcast decisions.

        Decider contract:
          - Returns GateResult.broadcast=False → suppress (event never emits)
          - Returns GateResult.broadcast=True  → apply data_patch, attach
            commit, then emit normally.
          - Exceptions in the decider are caught; event is silently suppressed
            to preserve the default-deny safety property.
        """
        try:
            event = adapter.to_event(raw_evt)
            if event is None:
                return  # adapter declined to emit (non-actionable reading)

            # ── New-arch decider hook ──────────────────────────────────────
            # Applied only when BOTH a decider is registered AND the category
            # has been explicitly cut over via MESHAI_CUTOVER_CATEGORIES.
            # When not cut over, the native adapter emits directly to the bus
            # (original pre-Phase-1 behavior); shadow_gate in consumer handles
            # dry-run comparison for the bake period.
            try:
                from meshai.notifications.gating import get_decider
                from meshai.notifications.cutover import is_cutover, NATIVE_ALWAYS_DECIDE
                from meshai.notifications import clock as _clock
                decider = get_decider(event.category)
                # Native WFIGS fire categories always run the decider — it IS
                # the fire gate (fires table + cooldown), so it can't hang on the
                # shadow-bake env var. Central is off, so this cannot affect any
                # Central render. All other categories stay env-gated (is_cutover).
                if decider is not None and (
                        is_cutover(event.category)
                        or event.category in NATIVE_ALWAYS_DECIDE):
                    if event.data is None:
                        event.data = {}
                    gate = decider(event.data, source=event.source,
                                   now=_clock.now())
                    if not gate.broadcast:
                        logger.debug(
                            "store: decider suppressed %s event %s: %s",
                            event.category, raw_evt.get("event_id", "?"),
                            gate.reason,
                        )
                        return
                    # Apply data_patch into event.data
                    event.data.update(gate.data_patch)
                    if gate.commit is not None:
                        event.data["_on_broadcast_committed"] = gate.commit
            except Exception as _gate_exc:
                logger.warning(
                    "store: decider failed for %s, suppressing: %s",
                    event.category, _gate_exc,
                )
                return  # default-deny on decider error

            self._event_bus.emit(event)
            logger.info(
                "Emitted %s event %s (%s) to pipeline bus",
                event.source,
                event.id,
                event.category,
            )
        except Exception as e:
            logger.warning("Failed to emit event to pipeline: %s", e)

    def _purge_expired(self):
        """Remove expired events."""
        now = time.time()
        expired = [
            k for k, v in self._events.items()
            if v.get("expires") and v["expires"] < now
        ]
        for k in expired:
            del self._events[k]

    def get_active(self, source: str = None) -> list:
        """Get active events, optionally filtered by source.

        Args:
            source: Filter to specific source (nws, swpc, etc.)

        Returns:
            List of event dicts sorted by fetched_at (newest first)
        """
        events = list(self._events.values())
        if source:
            events = [e for e in events if e["source"] == source]
        return sorted(events, key=lambda e: e.get("fetched_at", 0), reverse=True)

    def get_for_zones(self, zones: list) -> list:
        """Get events affecting specific NWS zones.

        Args:
            zones: List of UGC zone codes (e.g., ["IDZ016", "IDZ030"])

        Returns:
            List of events with overlapping zone coverage
        """
        zone_set = set(zones)
        return [
            e for e in self._events.values()
            if set(e.get("areas", [])) & zone_set
        ]

    def get_swpc_status(self) -> dict:
        """Get current SWPC space weather status."""
        return self._swpc_status

    def get_ducting_status(self) -> dict:
        """Get current tropospheric ducting status."""
        return self._ducting_status

    def get_rf_propagation(self) -> dict:
        """Combined HF + UHF propagation summary for dashboard/LLM."""
        return {
            "hf": self._swpc_status,
            "uhf_ducting": self._ducting_status,
        }

    def get_summary(self) -> str:
        """Compact text block for LLM context injection."""
        lines = []
        lines.append(f"### Current Conditions (as of {time.strftime('%H:%M:%S MT')}):")

        # NWS alerts
        nws = self.get_active(source="nws")
        if nws:
            lines.append(f"NWS: {len(nws)} active alert(s):")
            for a in nws[:3]:
                lines.append(f"  - {a['event_type']}: {a['headline'][:120]}")
        else:
            lines.append("NWS: No active alerts for mesh area.")

        # Space weather indices (raw - LLM interprets)
        s = self._swpc_status
        if s:
            kp = s.get("kp_current", "?")
            sfi = s.get("sfi", "?")
            r = s.get("r_scale", 0)
            g = s.get("g_scale", 0)
            lines.append(f"Space Weather: SFI {sfi}, Kp {kp}, R{r}/G{g}")
            warnings = s.get("active_warnings", [])
            if warnings:
                for w in warnings[:2]:
                    lines.append(f"  Warning: {w}")
        else:
            lines.append("Space Weather: Data not available.")

        # Tropospheric ducting (raw - LLM interprets)
        d = self._ducting_status
        if d:
            condition = d.get("condition", "unknown")
            gradient = d.get("min_gradient", "?")
            if condition == "normal":
                lines.append(f"Tropospheric: Normal (dM/dz {gradient} M-units/km)")
            else:
                thickness = d.get("duct_thickness_m", "?")
                lines.append(f"Tropospheric: {condition.replace('_', ' ').title()}")
                lines.append(f"  dM/dz: {gradient} M-units/km, duct ~{thickness}m thick")

        # Active fires
        fires = self.get_active(source="nifc")
        if fires:
            lines.append(f"Wildfires: {len(fires)} active")
            for f in fires[:2]:
                name = f.get("name", "Unknown")
                acres = f.get("acres", 0)
                pct = f.get("pct_contained", 0)
                dist = f.get("distance_km")
                lines.append(f"  - {name}: {int(acres):,} ac, {int(pct)}% contained" +
                            (f" ({int(dist)} km)" if dist else ""))

        # Avalanche advisories
        avy = self.get_active(source="avalanche")
        if avy:
            lines.append(f"Avalanche: {len(avy)} zone(s) with advisories")
            for a in avy[:2]:
                zone = a.get("zone_name", "Unknown")
                danger = a.get("danger_name", "Unknown")
                lines.append(f"  - {zone}: {danger}")

        # Stream gauges
        streams = self.get_active(source="usgs")
        if streams:
            lines.append(f"Stream Gauges: {len(streams)} readings")
            for s in streams[:2]:
                lines.append(f"  - {s['headline']}")

        # Traffic flow
        traffic = self.get_active(source="traffic")
        if traffic:
            lines.append(f"Traffic: {len(traffic)} corridors")
            for t in traffic[:2]:
                lines.append(f"  - {t['headline']}")

        # 511 road events
        roads = self.get_active(source="511")
        if roads:
            lines.append(f"Road Events: {len(roads)} active")
            for r in roads[:2]:
                lines.append(f"  - {r['headline'][:60]}")

        # Satellite hotspots
        hotspots = self.get_active(source="firms")
        if hotspots:
            new_ignitions = [h for h in hotspots if h.get("properties", {}).get("new_ignition")]
            lines.append(f"Satellite Hotspots: {len(hotspots)} detected")
            if new_ignitions:
                lines.append(f"  *** {len(new_ignitions)} POTENTIAL NEW IGNITION(S) ***")
            for h in hotspots[:2]:
                lines.append(f"  - {h['headline']}")

        return "\n".join(lines)


    def get_status(self) -> list:
        """Get status of all adapters including failed ones."""
        status = []
        for name, adapter in self._adapters.items():
            try:
                hs = adapter.health_status
                status.append({
                    "source": name,
                    "is_loaded": True,
                    "last_error": hs.get("last_error"),
                    "consecutive_errors": hs.get("consecutive_errors", 0),
                    "event_count": hs.get("event_count", 0),
                    "last_fetch": hs.get("last_fetch"),
                })
            except Exception:
                status.append({
                    "source": name,
                    "is_loaded": True,
                    "last_error": None,
                    "consecutive_errors": 0,
                    "event_count": 0,
                    "last_fetch": None,
                })
        for name, error in self._failed_adapters.items():
            status.append({
                "source": name,
                "is_loaded": False,
                "last_error": error,
                "consecutive_errors": 0,
                "event_count": 0,
                "last_fetch": None,
            })
        return status

    def get_source_health(self) -> list:
        """Get health status for all adapters."""
        return [a.health_status for a in self._adapters.values()]
