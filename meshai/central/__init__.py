"""Central connector package (v0.4) — consumes Central's NATS JetStream
firehose and normalizes it into meshai pipeline Events."""

from meshai.central.consumer import CentralConsumer

__all__ = ["CentralConsumer"]
