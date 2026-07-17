"""Central connector package (v0.4) — historically consumed Central's NATS
JetStream firehose and normalized it into meshai pipeline Events. The NATS
consumer is retired (Central is gone); this package now only holds the
split-file modules still used by the native adapter paths (firms_handler,
satpass_handler, tle_handler, wfigs_handler _render, budget,
idaho_gauge_sites, pass_predictor)."""
