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
    import importlib.util
    from pathlib import Path
    # Resolve the module's file path without importing it -- router.py
    # transitively imports optional LLM backend deps (openai/anthropic)
    # that may not be installed in every test environment, and this test
    # only needs the source text, not a working import.
    spec = importlib.util.find_spec("meshai.router")
    src = Path(spec.origin).read_text()
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
# Natural-language fire DMs route to the LLM (no ?status fallback)
# ===========================================================================


def test_natural_language_fire_question_routes_to_llm():
    """The LLM DM path is the sole interface for natural-language fire
    questions. Pre-revised commit there was a `?status` intent that
    rewrote the query in-router; this test confirms the rewrite is gone
    and that a plain English question is forwarded verbatim."""
    import asyncio
    from meshai.router import MessageRouter, RouteType
    from meshai.config_loader import load_config
    from meshai.history import ConversationHistory
    from meshai.commands.dispatcher import create_dispatcher

    cfg = load_config()
    history = ConversationHistory(cfg.history)

    async def _run():
        await history.initialize()
        dispatcher = create_dispatcher(
            prefix=cfg.commands.prefix,
            disabled_commands=cfg.commands.disabled_commands,
            custom_commands=cfg.commands.custom_commands,
        )

        class FakeConnector:
            my_node_id = "!THIS_BOT"

        class FakeMessage:
            text = "how's the cache peak fire?"
            sender_id = "!T"
            sender_name = "t"
            is_dm = True
            channel = 0

        router = MessageRouter(
            config=cfg, connector=FakeConnector(),
            history=history, dispatcher=dispatcher,
            llm_backend=None,  # we only inspect the route() decision
        )
        result = await router.route(FakeMessage())
        return result

    result = asyncio.run(_run())
    assert result.route_type == RouteType.LLM
    # Critical: the query must be the verbatim user text, not a rewrite
    # synthesized by an in-router intent helper.
    assert result.query == "how's the cache peak fire?"


def test_status_helpers_removed_from_router():
    """Hard guard against ?status helpers sneaking back in. If anyone
    adds a structured-command path to router.py for fires, this test
    fails and the author has to talk to Matt first."""
    import importlib.util
    from pathlib import Path
    # See test_router_scope_type_defined_before_env_check above for why
    # this resolves the path via find_spec rather than importing router.py
    # or hardcoding a deployment-container path.
    spec = importlib.util.find_spec("meshai.router")
    src = Path(spec.origin).read_text()
    assert "_maybe_rewrite_status_query" not in src
    assert "_lookup_fire_fuzzy" not in src
    assert "?status" not in src
