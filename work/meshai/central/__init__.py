"""Central connector package (v0.4) — historically consumed Central's NATS
JetStream firehose and normalized it into meshai pipeline Events. The NATS
consumer is retired (Central is gone); this package now only holds the
split-file modules still used by the native adapter paths (firms_handler,
wfigs_handler _render, idaho_gauge_sites). The satellite pieces
(satpass_handler, tle_handler, pass_predictor) have moved to
meshai.env.satellite — they're feed-adapter support code, not consumer
leftovers."""
