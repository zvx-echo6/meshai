# MeshAI Routing Simplification

**Goal:** An alert family should require ONE place to configure, not four interacting
settings (toggle enabled + min_severity threshold + severity_channels matrix +
per-family transport config).

**Reviewed at:** main @ 98e3fcf (2026-06-10)

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

Every item re-checked against source.

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
| B13 | **Guard-ordering trap.** In `_dispatch_toggles`, cooldown is armed (Section 2) and dedup recorded (Section 3) BEFORE the region filter, `min_severity` gate, and `severity_channels` matrix lookup. A below-threshold or wrong-region event consumes the cooldown window and writes a 7-day persisted dedup row, suppressing later events that WOULD deliver — including after the operator fixes config. | dispatcher.py Sections 2-3 vs region/severity/matrix checks |

---

## PART 3 — The simplification

Two changes that together reduce per-family config from four places to one.

### Sinks — destinations defined once

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
- **Status:** SinkConfig dataclass, `create_channel_from_sink()` factory, and migration
  script (`scripts/migrate_config_routing.py`) are implemented on this branch.

### Matrix-only severity — one panel per family

- Delete `NotificationToggle.min_severity`. The `severity_channels` matrix becomes the
  only gate; values are **sink names**, not channel types:
  `severity_channels: {routine: [], priority: [mesh-primary], immediate: [mesh-primary, dm-ops]}`
- Empty list = that severity doesn't deliver. Threshold semantics are now expressible,
  visible, and conflict-free (kills B1).
- Remove the "digest" pseudo-channel from the matrix (kills B2). Digest membership
  stays `digest.include`; GUI gets a separate per-family "include in digest" checkbox
  wired to it honestly.
- Migration: for each existing toggle, blank matrix rows below old `min_severity`,
  map `mesh_broadcast` → auto-created sink from its `broadcast_channel`, etc.
- **Session 2 MUST** move the cooldown commit and dedup record to after the delivery
  decision (region + matrix resolution). Matrix-only semantics inherit the suppression
  trap otherwise: an event hitting an empty matrix row must not burn its dedup slot
  or arm a cooldown. (See B13.)
- **Status:** Not yet implemented.

### GUI changes (when both halves are complete)

- New **Sinks** section (one-time setup, test button per sink — `test_connection()`
  already exists per channel class).
- Family cards shrink to: enabled, regions, freshness, cooldown, matrix of
  severity → sink multi-select, digest-include checkbox. Threshold dropdown, channel
  config block, SMTP block all deleted from the card.
- Fix B12 copy + stale slate class while in there.

---

## Amendments

### Amendment A1: Multi-file config layout

**Finding:** The live system uses a **multi-file config layout** where
`/data/config/notifications.yaml` IS the notifications config directly — it contains
`toggles:`, `rules:`, etc. at the root level with **no `notifications:` wrapper key**.

This differs from the monolithic layout shown in the plan examples, where notifications
config would be nested under a `notifications:` key in a larger config file.

**Impact:**
- Migration scripts must detect layout: if `"notifications" in config`, use nested
  access; if `"toggles" in config or "rules" in config`, treat file root as the
  notifications block.
- `load_notifications_config()` in `migrate_config_routing.py` implements this detection.
- GUI/API code reading `config.notifications.sinks` is unaffected — the `_dict_to_dataclass`
  conversion handles both layouts identically at runtime.

**Code pattern:**
```python
if "notifications" in config:
    notifications = config["notifications"]
elif "toggles" in config or "rules" in config:
    notifications = config  # Multi-file: file IS the notifications config
else:
    notifications = {}
```

*Recorded: 2026-06-10*

### Amendment A2: Layout-aware append-only sinks write

**Finding:** The original `write_sinks_to_config()` violated Amendment A1 — it
unconditionally wrote `config["notifications"]["sinks"]`, creating a bogus
`notifications:` wrapper in multi-file configs where the file root IS the
notifications block. Additionally, the `yaml.safe_load` → `yaml.dump` round-trip
destroyed comments and key ordering.

**Fix (implemented on this branch):**
1. **Shared layout detection.** `detect_config_layout(config)` returns `"monolithic"`,
   `"multifile"`, or `"empty"`. Both `load_notifications_config()` and
   `write_sinks_to_config()` use this helper — they cannot diverge.
2. **Append-only write.** `render_sinks_yaml(sinks, layout)` produces the exact text
   to append (indented for monolithic, root-level for multifile). The original file
   content is preserved byte-for-byte; only the sinks block is appended.
3. **Post-write verification.** After writing, `verify_sinks_written()` re-parses
   the file and asserts: (a) valid YAML, (b) sinks accessible at expected path,
   (c) original top-level keys intact. On failure, the backup is restored
   automatically.
4. **Dry-run transparency.** `--dry-run` now prints the detected layout, the path
   where sinks will live (`root-level sinks:` vs `notifications.sinks`), and the
   exact text that would be appended.

**Code pattern:**
```python
layout = detect_config_layout(config)
sinks_yaml = render_sinks_yaml(sinks, layout)
new_content = original_content + "\n" + sinks_yaml
# ... write, then verify_sinks_written() ...
```

*Recorded: 2026-06-11*
