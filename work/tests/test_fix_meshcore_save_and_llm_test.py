"""Regression tests for two bug fixes:

Bug 1 — save_section raises ValueError for 'meshcore_context'
  Covered by test_meshcore_context_save_section_no_error (uses tmp config dir).

Bug 2 — POST /api/config/test-llm "string indices must be integers, not 'str'"
  The handler was calling backend.generate(str, list) instead of
  generate(list[dict], str).  The google backend then iterated over the string
  char-by-char and blew up on `msg["role"]`.  After the fix the handler calls
  generate([{"role": "user", "content": "..."}], ""), which returns a plain
  string — the correct return type.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# -----------------------------------------------------------------------
# Stub heavy optional deps so config_routes can be imported without them.
# -----------------------------------------------------------------------
for _mod in ("openai", "aiosqlite", "anthropic", "google", "google.genai"):
    sys.modules.setdefault(_mod, MagicMock())


# ==========================================================================
# Bug 1 — meshcore_context in SECTION_TO_FILE
# ==========================================================================


def test_meshcore_context_in_section_to_file():
    """'meshcore_context' must be present in SECTION_TO_FILE mapping to
    config.yaml, otherwise save_section raises ValueError (HTTP 422)."""
    from meshai.config_loader import SECTION_TO_FILE

    assert "meshcore_context" in SECTION_TO_FILE, (
        "meshcore_context missing from SECTION_TO_FILE"
    )
    assert SECTION_TO_FILE["meshcore_context"] == "config.yaml"


def test_meshcore_context_save_section_no_error(tmp_path):
    """save_section('meshcore_context', ...) must not raise ValueError.

    Uses a minimal on-disk config.yaml so the saver has something to
    round-trip without needing !include or live config infrastructure.
    """
    import yaml
    from meshai.config_loader import save_section

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # Minimal seed so _load_yaml_preserve finds the file.
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"timezone": "UTC"})
    )

    # Should not raise
    result = save_section(
        "meshcore_context",
        {
            "enable_passive_context": True,
            "observe_channels": ["#general"],
            "ignore_contacts": [],
            "respond_to_dms": False,
        },
        cfg_dir,
    )
    assert result["saved"] is True
    assert any("config.yaml" in f for f in result["files_written"])

    # Verify the value was actually written to config.yaml
    on_disk = yaml.safe_load((cfg_dir / "config.yaml").read_text())
    assert "meshcore_context" in on_disk
    assert on_disk["meshcore_context"]["observe_channels"] == ["#general"]


# ==========================================================================
# Bug 2 — POST /api/config/test-llm
# ==========================================================================


def _build_test_app():
    """Build a minimal FastAPI app wired to config_routes with a google config."""
    from fastapi import FastAPI
    from meshai.dashboard.api.config_routes import router
    from meshai.config import Config, LLMConfig

    app = FastAPI()
    app.include_router(router, prefix="/api")

    cfg = Config()
    cfg.llm = LLMConfig(backend="google", model="gemini-2.5-flash", api_key="fake-key")
    app.state.config = cfg
    app.state.config_path = None  # not needed for this route
    return app


def test_test_llm_google_backend_returns_success():
    """POST /api/config/test-llm with google backend must return success:true.

    The GoogleBackend.generate() returns a plain string.  The old handler
    passed (str, list) to generate() and crashed; the fixed handler passes
    ([{"role": "user", "content": "..."}], ""), which the mocked backend
    receives as a proper list and returns the string reply.
    """
    from fastapi.testclient import TestClient

    app = _build_test_app()

    # resolve_api_key() must return a non-empty string so the handler proceeds.
    # GoogleBackend is mocked so no real HTTP call is made.
    with (
        patch.object(
            app.state.config.__class__,
            "resolve_api_key",
            return_value="fake-key",
        ),
        patch(
            "meshai.backends.GoogleBackend",
        ) as MockGoogleBackend,
    ):
        mock_instance = MagicMock()
        # generate() returns a plain string — that is the real return type.
        mock_instance.generate = AsyncMock(return_value="OK")
        mock_instance.close = AsyncMock()
        MockGoogleBackend.return_value = mock_instance

        client = TestClient(app)
        r = client.post("/api/config/test-llm")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["response"] == "OK"

    # Confirm generate was called with a list as first arg (not a string)
    call_args = mock_instance.generate.call_args
    messages_arg = call_args[0][0] if call_args[0] else call_args[1].get("messages")
    assert isinstance(messages_arg, list), (
        f"generate() first arg must be list[dict], got {type(messages_arg)}"
    )
    assert messages_arg[0]["role"] == "user"
