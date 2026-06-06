"""v0.7-fire-tracker-4 tests."""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


def _seed_fire(*, irwin_id, name, lat, lon, acres=None,
                contained=None, county="Test", state="ID"):
    from meshai.persistence import get_db
    get_db().execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, county, state, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, acres, contained, lat, lon, county, state,
         int(time.time())),
    )


# ===========================================================================
# Bug A regression: scope_type defined before use
# ===========================================================================


def test_router_scope_type_defined_before_env_check():
    """The env_reporter check at the top of generate_llm_response reads
    scope_type. Pre-fix it was UnboundLocalError on every env query."""
    import re
    from pathlib import Path
    src = Path("/opt/meshai/meshai/router.py").read_text()
    # Find the "if should_inject_mesh and scope_type" line + the
    # nearest preceding `scope_type, scope_value = ` assignment.
    env_use_line = None
    for i, line in enumerate(src.splitlines(), start=1):
        if "should_inject_mesh and scope_type" in line:
            env_use_line = i
            break
    assert env_use_line is not None
    # There must be an assignment on or before this line.
    preceding = "\n".join(src.splitlines()[: env_use_line - 1])
    assert re.search(r"scope_type[, ]+scope_value\s*=", preceding) \
        or "scope_type:" in preceding


# ===========================================================================
# adapter_config seed + categories registration
# ===========================================================================


def test_adapter_config_seeds_digest_keys():
    from meshai.persistence import get_db
    rows = {
        (r["adapter"], r["key"]): r["default_json"]
        for r in get_db().execute(
            "SELECT adapter, key, default_json FROM adapter_config "
            "WHERE adapter='fires' AND key LIKE 'digest%'"
        )
    }
    assert rows[("fires", "digest_enabled")] == "true"
    assert rows[("fires", "digest_schedule")] == '["06:00", "18:00"]'
    assert rows[("fires", "digest_timezone")] == '"America/Boise"'
    assert rows[("fires", "digest_max_chars")] == "200"


# ===========================================================================
# Digest renderer
# ===========================================================================


def test_render_digest_returns_no_fires_when_table_empty():
    from meshai.notifications.scheduled.fire_digest import render_digest

    async def _run():
        return await render_digest(llm_backend=None, max_chars=200)
    wire, source = asyncio.run(_run())
    assert wire == ""
    assert source == "no_fires"


def test_render_digest_terse_fallback_when_no_llm():
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847, contained=23)
    _seed_fire(irwin_id="ID-B", name="Twin Peaks",
                 lat=43.0, lon=-115.0, acres=320, contained=5)
    from meshai.notifications.scheduled.fire_digest import render_digest

    async def _run():
        return await render_digest(llm_backend=None, max_chars=200)
    wire, source = asyncio.run(_run())
    assert source == "fallback_terse"
    assert wire
    assert "Cache Peak" in wire
    assert len(wire) <= 200


def test_render_digest_uses_llm_when_available():
    """When the LLM backend returns a string, that string IS the wire."""
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847)

    class StubLLM:
        async def generate(self, *, messages, system_prompt, max_tokens):
            # The renderer must give us a single-line wire derived from
            # the LLM output, with markdown stripped + cap applied.
            return "Cache Peak 1847 ac stable; no spotting today."

    from meshai.notifications.scheduled.fire_digest import render_digest

    async def _run():
        return await render_digest(llm_backend=StubLLM(), max_chars=200)
    wire, source = asyncio.run(_run())
    assert source == "llm"
    assert wire == "Cache Peak 1847 ac stable; no spotting today."


# ===========================================================================
# Fuzzy fire lookup for ?status
# ===========================================================================


def test_status_lookup_exact_name():
    from meshai.router import _lookup_fire_fuzzy
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847)
    f = _lookup_fire_fuzzy("Cache Peak")
    assert f is not None
    assert f["incident_name"] == "Cache Peak"


def test_status_lookup_trims_trailing_fire_word():
    from meshai.router import _lookup_fire_fuzzy
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847)
    f = _lookup_fire_fuzzy("cache peak fire")
    assert f is not None
    assert f["incident_name"] == "Cache Peak"


def test_status_lookup_word_overlap_fallback():
    from meshai.router import _lookup_fire_fuzzy
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847)
    f = _lookup_fire_fuzzy("how is peak doing")
    assert f is not None
    assert f["incident_name"] == "Cache Peak"


def test_status_lookup_returns_none_on_no_match():
    from meshai.router import _lookup_fire_fuzzy
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847)
    assert _lookup_fire_fuzzy("nonexistent ranger station") is None


def test_status_query_rewrite_includes_fire_context():
    from meshai.router import _maybe_rewrite_status_query
    _seed_fire(irwin_id="ID-A", name="Cache Peak",
                 lat=42.0, lon=-114.0, acres=1847, contained=23)
    out = _maybe_rewrite_status_query("?status Cache Peak", router=None)
    assert out is not None
    assert "Cache Peak" in out
    assert "1847" in out
    # Must instruct the LLM to be terse mesh format.
    assert "mesh" in out.lower()


def test_status_query_rewrite_returns_none_when_not_status():
    from meshai.router import _maybe_rewrite_status_query
    out = _maybe_rewrite_status_query("how's the weather?", router=None)
    assert out is None
