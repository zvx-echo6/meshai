-- v0.6-3a adapter_config foundation (audit doc Section A keystone).
--
-- Two tables collapse every per-adapter magic number scattered across
-- meshai/central/* handlers + meshai/notifications/* into a single
-- GUI-editable surface, satisfying Rule 17 ("GUI-editable config").
--
-- adapter_config holds typed per-(adapter, key) values. The seed routine
-- in meshai/adapter_config/__init__.py:seed_defaults() populates one row
-- per entry in meshai/adapter_config/defaults.py:REGISTRY, copying
-- value_json = default_json so first-deploy behavior matches every
-- existing module-level constant exactly (per Matt's v0.6 Phase 1
-- refinement: hardcoded values become GUI defaults, no behavior change
-- on first deploy).
--
-- adapter_meta carries per-adapter metadata: a human display_name + the
-- include_in_llm_context toggle (per Matt's refinement #5: user picks
-- which adapters' tables the LLM may read in DMs).
--
-- value_json + default_json are TEXT-encoded JSON so int/float/str/bool/
-- list/dict all flow through one column uniformly. The accessor decodes
-- to the type tagged in `type`; a CHECK keeps that vocabulary closed so
-- a stray 'integer' or 'string' never sneaks in.

CREATE TABLE IF NOT EXISTS adapter_config (
    adapter       TEXT NOT NULL,
    key           TEXT NOT NULL,
    value_json    TEXT NOT NULL,
    default_json  TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('int','float','str','bool','json')),
    description   TEXT,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (adapter, key)
);
CREATE INDEX IF NOT EXISTS idx_adapter_config_adapter ON adapter_config(adapter);

CREATE TABLE IF NOT EXISTS adapter_meta (
    adapter                 TEXT PRIMARY KEY,
    display_name            TEXT,
    include_in_llm_context  INTEGER NOT NULL DEFAULT 1,
    description             TEXT,
    updated_at              REAL NOT NULL
);
