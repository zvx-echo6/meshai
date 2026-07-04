"""Read-only ephemeral fixture capture from NATS JetStream.

Captures real Central CloudEvents envelopes WITHOUT disturbing the live
durable consumers by using an ephemeral push consumer (no durable name,
AckPolicy.none).  The consumer is subject-based, so it auto-discovers the
correct stream (CENTRAL_QUAKE, CENTRAL_SPACE, …) exactly as the live
CentralConsumer does in meshai/central/consumer.py.

Run from inside the meshai container::

    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard quake \\
        --subject "central.quake.event.>" \\
        --mode all --max 25

    # Dry-run (count only, no file writes):
    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard quake \\
        --subject "central.quake.event.>" \\
        --mode all --max 25 --dry-run

    # Last-per-subject snapshot:
    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard swpc \\
        --subject "central.space.>" \\
        --mode last

Modes
-----
--mode last   DeliverPolicy.LAST_PER_SUBJECT — one message per subject key.
              Useful for a current-state snapshot.  Script stops after a
              short idle period (no new messages arriving).
--mode all    DeliverPolicy.ALL — bounded history.  REQUIRED: --max N cap
              to avoid pulling 330k+ traffic messages.

Output
------
Each captured envelope is written as::

    tests/fixtures/<hazard>/<n>.json
    {
        "envelope":       { ... },     # raw Central CloudEvents payload
        "subject":        "central.quake.event.minor.unknown",
        "captured_epoch": 1750000000
    }

Safety
------
The ephemeral consumer is created with AckPolicy.none and no durable name,
so it never advances the live durable consumers' sequence pointers and is
automatically cleaned up by the NATS server after inactivity.  No config,
no deploy, no restart changes are made.

Bug fix (2026-07-04)
--------------------
The previous version called js.add_consumer(stream, cfg) with a hardcoded
stream name "CENTRAL" that does not exist — Central partitions streams by
domain (CENTRAL_QUAKE, CENTRAL_SPACE, CENTRAL_WX, …).  It then called
pull_subscribe_bind() without await, making it a no-op coroutine object
instead of an actual subscription, and the subsequent .fetch() raised
AttributeError / NotFoundError.

Fix: mirror the proven pattern from meshai/central/consumer.py — use
js.subscribe(subject, cb=..., config=ConsumerConfig(...)) with no durable
name.  The subject-based subscribe call auto-discovers the correct stream
server-side, identical to how the live CentralConsumer binds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

# --------------------------------------------------------------------------
# All network + config access is deferred to main() so this module is safely
# importable in unit-test environments without a running NATS server.
# --------------------------------------------------------------------------

# Seconds with no incoming message before the capture loop stops.
# Sufficient for both LAST_PER_SUBJECT (snapshot drains quickly) and ALL
# (history replay has no inter-message gaps larger than this in practice).
_IDLE_TIMEOUT = 4.0


def _output_dir(hazard: str) -> pathlib.Path:
    """Resolve tests/fixtures/<hazard>/ relative to the repo root."""
    # Script lives at <repo>/scripts/capture_fixtures.py;
    # fixtures live at <repo>/tests/fixtures/<hazard>/.
    repo_root = pathlib.Path(__file__).parent.parent
    return repo_root / "tests" / "fixtures" / hazard


async def _run(
    *,
    nats_url: str,
    subject: str,
    hazard: str,
    mode: str,
    max_msgs: int,
    dry_run: bool,
) -> int:
    """Connect, create ephemeral push consumer, collect messages, write fixtures.

    Returns the count of messages captured (or counted, for --dry-run).
    """
    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()

        deliver_policy = (
            DeliverPolicy.LAST_PER_SUBJECT
            if mode == "last"
            else DeliverPolicy.ALL
        )

        # Funnel incoming messages into an asyncio Queue so the main loop
        # can apply the max-msgs cap and idle-timeout without threads.
        msg_q: asyncio.Queue = asyncio.Queue()

        async def _on_msg(msg):
            await msg_q.put(msg)

        # Ephemeral push subscribe — NO durable_name → server assigns a
        # transient consumer name and auto-deletes it after inactivity.
        # AckPolicy.NONE means we never ack, so no sequence cursor is
        # advanced on any durable consumer.  The subject-based call
        # auto-discovers the correct NATS stream (CENTRAL_QUAKE,
        # CENTRAL_SPACE, etc.) — identical to CentralConsumer.start().
        sub = await js.subscribe(
            subject,
            cb=_on_msg,
            config=ConsumerConfig(
                deliver_policy=deliver_policy,
                ack_policy=AckPolicy.NONE,
            ),
        )

        out_dir = _output_dir(hazard)
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)

        captured = 0

        while captured < max_msgs:
            try:
                msg = await asyncio.wait_for(msg_q.get(), timeout=_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # No new messages within idle window — snapshot is drained
                # (LAST_PER_SUBJECT) or history is exhausted (ALL).
                break

            try:
                envelope = json.loads(msg.data)
            except Exception:
                continue  # skip unparseable frames

            if dry_run:
                captured += 1
                print(
                    "  [dry-run] #%d subject=%r" % (captured, msg.subject),
                    file=sys.stderr,
                )
            else:
                record = {
                    "envelope": envelope,
                    "subject": msg.subject,
                    "captured_epoch": int(time.time()),
                }
                out_path = out_dir / ("%04d.json" % captured)
                out_path.write_text(
                    json.dumps(record, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                captured += 1
                print(
                    "  wrote %s  subject=%r" % (out_path, msg.subject),
                    file=sys.stderr,
                )

        # Unsubscribe: signals the server to clean up the ephemeral consumer.
        try:
            await sub.unsubscribe()
        except Exception:
            pass

        return captured

    finally:
        await nc.drain()
        await nc.close()


def _load_nats_url() -> str:
    """Read the NATS URL from meshai config or env override."""
    if "MESHAI_NATS_URL" in os.environ:
        return os.environ["MESHAI_NATS_URL"]
    try:
        from meshai.config_loader import load_config
        cfg = load_config()
        return cfg.environmental.central.url  # type: ignore[attr-defined]
    except Exception:
        return "nats://localhost:4222"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture Central NATS envelopes as fixture files (read-only)."
    )
    parser.add_argument("--hazard", required=True,
                        help="Hazard category label (used as fixture sub-dir).")
    parser.add_argument("--subject", required=True,
                        help="NATS subject filter, e.g. 'central.quake.event.>'.")
    parser.add_argument("--mode", choices=["last", "all"], default="all",
                        help="DeliverPolicy: last=LAST_PER_SUBJECT, all=ALL (default: all).")
    parser.add_argument("--max", type=int, default=50, dest="max_msgs",
                        help="Maximum messages to capture (required cap; default: 50).")
    parser.add_argument("--nats-url", default=None,
                        help="Override the NATS URL (default: read from meshai config).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count messages only; do not write fixture files.")
    args = parser.parse_args(argv)

    nats_url = args.nats_url or _load_nats_url()
    print(
        "capture_fixtures: url=%r subject=%r hazard=%r mode=%r max=%d dry_run=%s"
        % (nats_url, args.subject, args.hazard, args.mode, args.max_msgs, args.dry_run),
        file=sys.stderr,
    )

    count = asyncio.run(
        _run(
            nats_url=nats_url,
            subject=args.subject,
            hazard=args.hazard,
            mode=args.mode,
            max_msgs=args.max_msgs,
            dry_run=args.dry_run,
        )
    )

    verb = "counted" if args.dry_run else "captured"
    print("%s %d envelope(s) for hazard=%r" % (verb, count, args.hazard), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
