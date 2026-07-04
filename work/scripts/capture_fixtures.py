"""Read-only ephemeral fixture capture from NATS JetStream.

Captures real Central CloudEvents envelopes WITHOUT disturbing the live
durable consumers by using an ephemeral pull consumer (no durable name,
AckPolicy.none, short inactive_threshold for auto-deletion).

Run from inside the meshai container::

    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard earthquake_event \\
        --subject "central.usgs_quake.>" \\
        --mode all --max 20

    # Dry-run (count only, no file writes):
    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard earthquake_event \\
        --subject "central.usgs_quake.>" \\
        --mode all --max 20 --dry-run

    # Last-per-subject snapshot:
    docker exec meshai python /app/scripts/capture_fixtures.py \\
        --hazard nws \\
        --subject "central.nws.>" \\
        --mode last

Modes
-----
--mode last   DeliverPolicy.LAST_PER_SUBJECT — one message per subject key.
              Useful for a current-state snapshot.
--mode all    DeliverPolicy.ALL — bounded history.  REQUIRED: --max N cap
              to avoid pulling 330k+ traffic messages.

Output
------
Each captured envelope is written as::

    tests/fixtures/<hazard>/<n>.json
    {
        "envelope":       { ... },     # raw Central CloudEvents payload
        "subject":        "central.usgs_quake.us7000xyz",
        "captured_epoch": 1750000000
    }

Safety
------
The ephemeral consumer is created with AckPolicy.none and a 30-second
inactive_threshold.  It is never assigned a durable name, so it never
advances the live durable consumers' sequence pointers and is automatically
cleaned up by the NATS server after inactivity.
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


def _output_dir(hazard: str) -> pathlib.Path:
    """Resolve tests/fixtures/<hazard>/ relative to the repo root."""
    # Script lives at <repo>/scripts/capture_fixtures.py;
    # fixtures live at <repo>/tests/fixtures/<hazard>/.
    repo_root = pathlib.Path(__file__).parent.parent
    return repo_root / "tests" / "fixtures" / hazard


async def _run(
    *,
    nats_url: str,
    stream: str,
    subject: str,
    hazard: str,
    mode: str,
    max_msgs: int,
    dry_run: bool,
) -> int:
    """Connect, create ephemeral consumer, pull messages, write fixtures.

    Returns the count of messages captured (or counted, for --dry-run).
    """
    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()

        # Build an ephemeral consumer config (no durable_name = ephemeral).
        # AckPolicy.none avoids needing to ack — purely read-only.
        # inactive_threshold of 30 s ensures the NATS server auto-deletes it.
        deliver_policy = (
            DeliverPolicy.LAST_PER_SUBJECT
            if mode == "last"
            else DeliverPolicy.ALL
        )
        cfg = ConsumerConfig(
            # durable_name intentionally omitted → ephemeral consumer
            filter_subject=subject,
            deliver_policy=deliver_policy,
            ack_policy=AckPolicy.NONE,
            inactive_threshold=30.0,  # seconds → server auto-deletes after idle
        )

        # Create ephemeral pull consumer (server-side, no local binding name).
        consumer_info = await js.add_consumer(stream, cfg)
        consumer_name = consumer_info.name

        out_dir = _output_dir(hazard)
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)

        captured = 0
        fetch_batch = min(max_msgs, 50)  # pull in bounded batches

        while captured < max_msgs:
            batch = min(fetch_batch, max_msgs - captured)
            try:
                msgs = await js.pull_subscribe_bind(
                    stream, consumer_name
                ).fetch(batch, timeout=5.0)
            except nats.errors.TimeoutError:
                break  # no more messages within timeout

            if not msgs:
                break

            for msg in msgs:
                try:
                    envelope = json.loads(msg.data)
                except Exception:
                    continue  # skip unparseable messages

                if dry_run:
                    captured += 1
                    print(
                        f"  [dry-run] #{captured} subject={msg.subject!r}",
                        file=sys.stderr,
                    )
                else:
                    record = {
                        "envelope": envelope,
                        "subject": msg.subject,
                        "captured_epoch": int(time.time()),
                    }
                    out_path = out_dir / f"{captured:04d}.json"
                    out_path.write_text(
                        json.dumps(record, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    captured += 1
                    print(
                        f"  wrote {out_path.relative_to(pathlib.Path.cwd())} "
                        f"subject={msg.subject!r}",
                        file=sys.stderr,
                    )

                if captured >= max_msgs:
                    break

            # For last-per-subject: a single fetch is sufficient.
            if mode == "last":
                break

        # Delete the ephemeral consumer explicitly (belt-and-suspenders).
        try:
            await js.delete_consumer(stream, consumer_name)
        except Exception:
            pass  # server already cleaned up, or error is non-fatal

        return captured

    finally:
        await nc.drain()
        await nc.close()


def _load_nats_url() -> str:
    """Read the NATS URL from meshai config or env override."""
    # Allow an explicit env override for CI / ad-hoc use.
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
                        help="NATS subject filter, e.g. 'central.usgs_quake.>'.")
    parser.add_argument("--stream", default="CENTRAL",
                        help="JetStream stream name (default: CENTRAL).")
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
        f"capture_fixtures: url={nats_url!r} stream={args.stream!r} "
        f"subject={args.subject!r} hazard={args.hazard!r} "
        f"mode={args.mode!r} max={args.max_msgs} dry_run={args.dry_run}",
        file=sys.stderr,
    )

    count = asyncio.run(
        _run(
            nats_url=nats_url,
            stream=args.stream,
            subject=args.subject,
            hazard=args.hazard,
            mode=args.mode,
            max_msgs=args.max_msgs,
            dry_run=args.dry_run,
        )
    )

    verb = "counted" if args.dry_run else "captured"
    print(f"{verb} {count} envelope(s) for hazard={args.hazard!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
