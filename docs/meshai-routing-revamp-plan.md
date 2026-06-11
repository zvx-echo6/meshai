# MeshAI Broadcast Routing Revamp — Architecture Review & Plan
**Reviewed at:** main @ 98e3fcf (2026-06-10)
**Method:** three-pass read of the notification subsystem — full read, plan construction, line-level verification of every claim.

---

## PART 1 — How routing actually works today

There are **five parallel delivery paths**, not one system with layers.

### Path 1: Legacy NotificationRouter (`notifications/router.py`)
Fed by `main._dispatch_alerts()` (main.py:644-684) ← `alert_engine.check()` — mesh-health
and env-poll alerts as raw dicts. Iterates `config.notifications.rules`
(NotificationRuleConfig): category match → `min_severity` gate → per-rule
`cooldown_minutes` dedup keyed on `(rule_name, category, event_id-or-message[:50])`
(router.py:165-172) → builds channel from the rule's **inline transport fields** → delivers.
- Writes **no** `mesh_broadcasts_out` audit row.
- Rule stats in `/data/rule_stats.json` (router.py:24), separate from SQLite.
- Mesh messages >200 chars go through **LLM summarization** (router.py:187-193).
- Also backs the over-the-mesh `!subscribe` command — `add_mesh_subscription()`
  (router.py:737) dynamically creates a mesh_dm rule per subscriber node — and
  `generate_report()` used by report delivery (router.py:628).

### Path 2: Dispatcher rules path (`pipeline/dispatcher.py:252-279`)
Fed by the EventBus. Evaluates **the same rules list** against bus Events
(`_matching_rules`, dispatcher.py:632-653: enabled → condition → category →
min_severity → region_scope). Then delivers.
- **No cooldown. No dedup. No staleness. No audit row.** None of the toggle-path
  guards apply here.

### Path 3: Dispatcher toggle path (`pipeline/dispatcher.py:281-456`) — the good one
Fed by the EventBus through the pipeline: `bus → Inhibitor (inhibit_keys, ttl 1800s,
persisted) → Grouper (group_key window 60s, persisted) → ToggleFilter (family enabled
set) → tee(dispatch + DigestAccumulator)`.

Inside `_dispatch_toggles`, in order:
0. Cold-start grace (60s after first event, persisted anchor)
1. Staleness — `toggle.freshness_seconds` (fire family reads `wfigs.freshness_seconds`)
2. Cooldown — per `(toggle, category, region|_cooldown_suffix)`, persisted; **immediate
   severity bypasses** (dispatcher.py:363-364)
3. Dedup — `(source, event.id)` LRU 10k, 7-day SQLite window
4. Region scope → **`min_severity` gate (line 420-422)** → **`severity_channels`
   matrix (line 434-435)** → composer (150-byte budget) → channel per matrix entry →
   `_post_broadcast_commit`: `mesh_broadcasts_out` audit row + handler callback.

### Path 4: Scheduled broadcasts (`dispatch_scheduled_broadcast`, dispatcher.py:475-561)
Band conditions (3×/day), fire digest (2×/day), reminders — all bypass the pipeline.
Only cold-start grace applies. **All three hardcode-route through
`rf_propagation.broadcast_channel`** (dispatcher.py:512-519). Writes audit row.

### Path 5: Fallbacks
`mesh_intelligence.alert_channel` + subscriber DMs when no NotificationRouter
(main.py:660-682); SubscriptionManager scheduled DM reports (main.py:686+).

### Pre-pipeline gates (upstream of all of the above)
Adapter floors (swpc kp/flare/proton), handler change-detection + cooldowns (fire 8h
update etc.), consumer **default-deny** — no synthesized wire string → Event never
enters the bus (consumer.py:548-567).

---

## PART 2 — Verified defects and redundancies

Every item re-checked against source in pass 3.

| # | Finding | Evidence |
|---|---------|----------|
| B1 | **Threshold + matrix double-gate.** `min_severity` gates before `severity_channels`; a routine matrix row is dead config when threshold=priority. Exactly the GUI trap from the screenshot. | dispatcher.py:420-422 vs 434-435 |
| B2 | **The "digest" matrix column is a no-op.** Dispatcher skips it (`if ch_type == "digest": continue`); digest membership is actually `digest.include` (toggle-name list). Checking the box changes nothing. By design per `test_digest_channel_skipped_in_live_dispatch`, but the GUI presents it as live routing. | dispatcher.py:436-437; pipeline/__init__.py include_toggles |
| B3 | **Rules-path deliveries are invisible.** Neither Path 1 nor Path 2 writes `mesh_broadcasts_out`. Forensics on that table only sees toggle + scheduled traffic. | router.py:208; dispatcher.py:263-279 |
| B4 | **Path 2 has zero spam protection.** A rule matching a chatty central category re-delivers every event. | dispatcher.py:252-279 |
| B5 | **Double-delivery by design.** `dispatch()` runs rules AND toggles; both matching = same event broadcast twice, possibly same channel. Tested as intended behavior (`test_rules_and_toggles_both_fire`). | dispatcher.py:247-250 |
| B6 | **Channel 0 is falsy.** `if rf is None or not getattr(rf, "broadcast_channel", None)` — `rf_propagation.broadcast_channel = 0` silently drops ALL scheduled broadcasts (band conditions, fire digest, reminders). Channel 0 is a legitimate primary channel. | dispatcher.py:515 |
| B7 | **Fire digest + reminders ride the RF toggle's channel.** Fire content routed by RF-propagation transport config; disabling/misconfiguring the RF toggle silently kills fire digests. | dispatcher.py:512-519; fire_digest.py:255; reminders/__init__.py:315 |
| B8 | **Severity fails open** in the legacy router — unknown severity string returns True. | router.py:223-224 |
| B9 | **Transport config duplicated everywhere.** Full SMTP credential block inline in every rule AND every toggle (config.py:503-556, 558-580). The stale-SMTP-per-rule failure mode is structural. |
| B10 | **Two mesh formatting regimes.** Legacy: LLM-summarize >200 chars. Toggle: deterministic composer, 150-byte budget. Same radio, different text rules. | router.py:187-193 vs composer.py:31 |
| B11 | **Three incompatible dedup keys** (legacy `(rule,cat,event_id|msg[:50])` in-memory; toggle `(source,event.id)` persisted; rules-path none). |
| B12 | GUI copy bug: severity helper says "Warning" recommended — not a severity level in this system (routine/priority/immediate). Same line still has stale `text-slate-600`. | Notifications.tsx:605 |

**Bearing on today's "only band conditions" mystery:** fires/traffic went out via the
toggle path on `fires`/`roads` toggles' `broadcast_channel` (default → 0 via
`_toggle_to_rule`'s `or 0`); band conditions went out on `rf_propagation.broadcast_channel`,
which (per B6) **must be non-zero or they'd have been dropped**. So band conditions and
fire/traffic provably left on **different channel indices**. If the node was only
listening where band conditions land, that's the whole mystery. The forensics query
(`select channel, source_event_table from mesh_broadcasts_out`) confirms in one pass.

---

## PART 3 — The revamp plan

**Principle: consolidate onto the toggle path.** It's the battle-tested one — guards,
persistence, audit, composer. We're not building a third system; we're deleting the
other four and fixing the toggle path's warts.

### A. Sinks — transports defined once
New `SinkConfig` dataclass + `sinks: dict[str, SinkConfig]` on NotificationsConfig:
```yaml
notifications:
  sinks:
    mesh-primary:  {type: mesh_broadcast, channel: 0}
    mesh-alerts:   {type: mesh_broadcast, channel: 2}
    dm-ops:        {type: mesh_dm, node_ids: ["!abcd1234"]}
    email-ops:     {type: email, smtp_host: ..., recipients: [...]}
```
- `channels.py` gains `create_channel_from_sink(sink, connector)`. Existing channel
  classes unchanged.
- All transport fields (`broadcast_channel`, `node_ids`, `smtp_*`, `webhook_*`)
  **removed** from NotificationToggle and NotificationRuleConfig (kept dataclass-side
  only during migration window).
- Channel index stored as `int`, validated `>= 0` — kills B6's falsy-zero class of bug.

### B. One severity mechanism — matrix only
- Delete `NotificationToggle.min_severity`. The `severity_channels` matrix becomes the
  only gate; values are **sink names**, not channel types:
  `severity_channels: {routine: [], priority: [mesh-primary], immediate: [mesh-primary, dm-ops]}`
- Empty list = that severity doesn't deliver. Threshold semantics are now expressible,
  visible, and conflict-free (kills B1).
- Remove the "digest" pseudo-channel from the matrix (kills B2). Digest membership
  stays `digest.include`; GUI gets a separate per-family "include in digest" checkbox
  wired to it honestly.
- Migration: for each existing toggle, blank matrix rows below old `min_severity`,
  map `mesh_broadcast`→auto-created sink from its `broadcast_channel`, etc. Write the
  migration as `scripts/migrate_config_routing.py` mirroring `migrate_config_v03.py`.

### C. Retire the rules system (both evaluators)
- Delete `Dispatcher._dispatch_rules` and the legacy `NotificationRouter.process_alert`
  delivery path (kills B3/B4/B5/B8/B10/B11).
- `main._dispatch_alerts` is replaced by: **alert_engine emits Events onto the bus**
  (`make_event(source="alert_engine", category=<existing category ids>, ...)`).
  Categories already map to the `mesh_health` family via `categories.py`, so
  mesh-health alerts inherit ToggleFilter, guards, composer, audit — everything.
- The `!subscribe` mesh command moves off dynamic rules: subscriptions table already
  exists; implement as **per-node DM sinks auto-managed by the command**, matched by
  family in the dispatcher (small `subscriber_index: {family: [node_ids]}` consulted
  after the matrix). `generate_report()` and the band-conditions compute helpers move
  out of router.py into `notifications/reports.py`; router.py is then deleted.
- `rule_stats.json` → replaced by routing_log (below).

### D. Routing trace — `routing_log` table
```
routing_log(id, ts, event_source, event_category, event_id, severity,
            family, stage, outcome, sink, detail)
```
- One row per decision: `stage` ∈ {toggle_filter, cold_start, staleness, cooldown,
  dedup, region, matrix, delivery}; `outcome` ∈ {pass, drop, sent, failed}.
- Dispatcher's four counters stay (cheap), but every drop now also writes its reason
  row. Retention: 14 days, pruned on insert like dispatcher_dedup.
- Surface: `GET /api/routing/log?source=&category=&since=` + a Carbon dashboard panel.
  "Why didn't I get the fire alert" becomes one query.

### E. Dry-run endpoint
`POST /api/routing/test` with `{source, category, severity, region, lat, lon}` →
synthesize an Event, run the FULL pipeline with delivery stubbed → return the
stage-by-stage trace (same shape as routing_log rows) without keying the radio.
Pairs with `central.<domain>.test.>` subjects for end-to-end with-radio tests.

### F. Scheduled broadcasts get their own sinks
- `band_conditions.sink`, `fires.digest_sink`, per-reminder `sink` in adapter_config
  (defaults: `mesh-primary`). Kills B6/B7 — fire digests no longer depend on the RF
  toggle, and channel 0 works.
- `dispatch_scheduled_broadcast(text, sink_name, ...)` — grace check unchanged,
  audit row unchanged.

### G. GUI rebuild (Notifications.tsx)
- New **Sinks** section (one-time setup, test button per sink — `test_connection()`
  already exists per channel class).
- Family cards shrink to: enabled, regions, freshness, cooldown, matrix of
  severity → sink multi-select, digest-include checkbox. Threshold dropdown, channel
  config block, SMTP block all deleted from the card.
- Routing log panel + dry-run form.
- Fix B12 copy + stale slate class while in there.

### Phasing for CC (each phase independently shippable)
1. **Phase A** — SinkConfig + sinks in config + `create_channel_from_sink` + migration
   script + tests. No behavior change yet (toggles still read inline config if sinks absent).
2. **Phase B** — matrix-to-sinks + min_severity removal + migration + dispatcher change
   + GUI family cards + tests.
3. **Phase C** — routing_log + dry-run endpoint + GUI panel.
4. **Phase D** — alert_engine → bus; delete _dispatch_rules, legacy router delivery,
   _dispatch_alerts; move reports/subscribe surfaces; delete rule_stats.
5. **Phase E** — scheduled-broadcast sinks (B6/B7 fix).

Order rationale: A is pure addition (zero risk); B delivers the conflict fix you hit
today; C delivers the observability that prevents the next debugging afternoon; D is
the big deletion and needs C's trace to validate; E is small and independent (can be
pulled forward if fire digests misbehave before then).

### Risks / blast radius
- **Config migration** is the dangerous part: live `/data/config/env_feeds.yaml` +
  notifications config carry real toggles. Migration must be idempotent, back up the
  file first, and refuse to run twice.
- **Tests:** 68 test files; `test_v052_dispatcher.py`, `test_notification_toggles.py`,
  `test_cold_start_grace.py`, `test_dispatcher_persistence.py`, `test_pipeline_*.py`
  directly assert current dual-path semantics. `test_rules_and_toggles_both_fire`
  inverts (must assert single delivery). Budget real time per phase for test rework.
- **`/api/config` PUT live-refresh** (`_refresh_toggle_filter`, config_routes.py:228+)
  must learn to refresh sinks too, or channel edits need restart.
- **Mesh `!subscribe`** is user-facing on-air behavior — Phase D must not break
  existing subscriber DMs; migrate the subscriptions table contents.
- Magic data-key contract (`_broadcast_audit`, `_on_broadcast_committed`,
  `_severity_override`, `_meshai_precomposed`, `_cooldown_suffix`) spans 7 central
  handlers — **unchanged by this plan**; the revamp is strictly downstream of the bus.

---

## PART 4 — Verification pass results

Claims re-checked against source after plan construction:

- Dual gate (B1): confirmed at dispatcher.py:420-422 (rank check) preceding 434
  (matrix read). The matrix is unreachable for severities below threshold.
- Matrix digest no-op (B2): the ONLY reader of `severity_channels` in the codebase is
  dispatcher.py:434 (grep verified); digest content driven solely by
  `DigestAccumulator(include_toggles=digest.include)`.
- Audit asymmetry (B3): `_post_broadcast_commit` called only at dispatcher.py:452
  (toggle path) and inline insert at :549 (scheduled). No insert in `_dispatch_rules`
  or router.py.
- Channel-0 falsy (B6): `not getattr(rf, "broadcast_channel", None)` — `not 0 == True`.
  Confirmed your live system must have rf channel ≠ 0 (band conditions are delivering),
  which also confirms the channel-split explanation for today's symptom.
- Double-delivery intended (B5): `dispatch()` awaits both paths unconditionally;
  covered by `test_rules_and_toggles_both_fire`.
- Composer is sole mesh-format authority on the toggle path (mesh.py `_format_one_line`
  passes message through verbatim, per the v0.5.7-regression note) — so sink
  consolidation does not change wire format. Legacy-path retirement (Phase D) removes
  the only competing formatter.
- Pipeline order: bus → inhibitor → grouper → toggle_filter → tee
  (pipeline/__init__.py:118-121) — sinks plan touches none of these stages.
- Scheduled paths all funnel through `dispatch_scheduled_broadcast` (band_conditions,
  fire_digest.py:255, reminders/__init__.py:315) — single choke point makes Phase E a
  one-function change plus three config keys.

---

## PART 5 — Implementation amendments

### Amendment A1: Multi-file config layout (Phase A discovery)

**Finding:** The live system uses a **multi-file config layout** where
`/data/config/notifications.yaml` IS the notifications config directly — it contains
`toggles:`, `rules:`, etc. at the root level with **no `notifications:` wrapper key**.

This differs from the monolithic layout shown in the plan examples, where notifications
config would be nested under a `notifications:` key in a larger config file.

**Impact on all phases:**
- Migration scripts must detect layout: if `"notifications" in config`, use nested
  access; if `"toggles" in config or "rules" in config`, treat file root as the
  notifications block.
- `load_notifications_config()` in `migrate_config_routing.py` implements this detection.
- GUI/API code reading `config.notifications.sinks` is unaffected — the `_dict_to_dataclass`
  conversion handles both layouts identically at runtime.
- **All later phases** should follow this pattern when accessing the raw YAML for migration.

**Code pattern (from Phase A migration script):**
```python
if "notifications" in config:
    notifications = config["notifications"]
elif "toggles" in config or "rules" in config:
    notifications = config  # Multi-file: file IS the notifications config
else:
    notifications = {}
```

*Recorded: 2026-06-10, during Phase A implementation.*
