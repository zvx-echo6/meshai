"""Tests for per-mesh LLM scoping changes.

Covers:
  (a) should_respond DM gate decoupling (MeshCore vs Meshtastic)
  (b) get_context_block(transport=...) per-mesh filtering
  (c) Meshtastic channel-index filter is skipped for MeshCore observations
  (d) ContextConfig.max_age default is 14 days (1_209_600 seconds)
"""

import importlib
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub optional heavy deps so meshai.router can be imported in this env.
#
# Try the real import first -- sys.modules.setdefault() alone is only a
# no-op "in production where the real packages are installed" if some
# earlier-run test has already imported the real module. When this file
# collects first (test order is alphabetical, not guaranteed), setdefault()
# permanently replaces a genuinely-installed module (e.g. aiosqlite) with a
# bare MagicMock for the rest of the pytest session -- every later test's
# `await aiosqlite.connect(...)` then breaks with "MagicMock can't be used
# in 'await' expression". Only fall back to the mock when the real package
# truly isn't importable.
# ---------------------------------------------------------------------------
for _mod in ("openai", "aiosqlite", "anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        try:
            importlib.import_module(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

from meshai.config import (  # noqa: E402
    BotConfig,
    Config,
    ContextConfig,
    MeshCoreContextConfig,
)
from meshai.connector import MeshMessage  # noqa: E402
from meshai.context import MeshContext  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(transport="meshtastic", is_dm=True, text="hello"):
    return MeshMessage(
        sender_id="abc1",
        sender_name="Alice",
        text=text,
        channel=0,
        is_dm=is_dm,
        transport=transport,
    )


def _make_router(respond_to_dms: bool, meshcore_respond_to_dms: bool = True):
    """Build a minimal Router with mocked dependencies."""
    from meshai.router import MessageRouter  # noqa: PLC0415

    config = Config()
    config.bot = BotConfig(respond_to_dms=respond_to_dms)
    config.meshcore_context = MeshCoreContextConfig(
        respond_to_dms=meshcore_respond_to_dms
    )

    connector = MagicMock()
    connector.my_node_id = "zzzz"

    router = MessageRouter.__new__(MessageRouter)
    router.config = config
    router.connector = connector
    router.meshmonitor_sync = None
    router.continuations = MagicMock()
    router.continuations.has_pending.return_value = False
    return router


# ---------------------------------------------------------------------------
# (a) should_respond — DM gate decoupling
# ---------------------------------------------------------------------------

def test_meshcore_dm_passes_when_bot_respond_to_dms_false():
    """MeshCore DM should reach should_respond=True even if bot.respond_to_dms=False.

    The global bot.respond_to_dms toggle is Meshtastic-only; MeshCore DMs
    are pre-filtered at the transport by meshcore_context.respond_to_dms.
    """
    router = _make_router(respond_to_dms=False, meshcore_respond_to_dms=True)
    msg = _make_message(transport="meshcore", is_dm=True)
    assert router.should_respond(msg) is True


def test_meshtastic_dm_blocked_when_bot_respond_to_dms_false():
    """Meshtastic DM must be blocked when bot.respond_to_dms=False."""
    router = _make_router(respond_to_dms=False)
    msg = _make_message(transport="meshtastic", is_dm=True)
    assert router.should_respond(msg) is False


def test_meshtastic_dm_passes_when_bot_respond_to_dms_true():
    """Meshtastic DM passes when bot.respond_to_dms=True."""
    router = _make_router(respond_to_dms=True)
    msg = _make_message(transport="meshtastic", is_dm=True)
    assert router.should_respond(msg) is True


# ---------------------------------------------------------------------------
# (b) get_context_block(transport=...) — per-mesh filtering
# ---------------------------------------------------------------------------

def test_get_context_block_no_transport_filter_returns_all():
    ctx = MeshContext()
    ctx.observe("Alice", "a1", "hello from MT", channel=0, is_dm=False, transport="meshtastic")
    ctx.observe("Bob", "b1", "hello from MC", channel=1, is_dm=False, transport="meshcore")
    block = ctx.get_context_block(transport=None)
    assert "hello from MT" in block
    assert "hello from MC" in block


def test_get_context_block_transport_meshtastic_only():
    ctx = MeshContext()
    ctx.observe("Alice", "a1", "hello from MT", channel=0, is_dm=False, transport="meshtastic")
    ctx.observe("Bob", "b1", "hello from MC", channel=1, is_dm=False, transport="meshcore")
    block = ctx.get_context_block(transport="meshtastic")
    assert "hello from MT" in block
    assert "hello from MC" not in block


def test_get_context_block_transport_meshcore_only():
    ctx = MeshContext()
    ctx.observe("Alice", "a1", "hello from MT", channel=0, is_dm=False, transport="meshtastic")
    ctx.observe("Bob", "b1", "hello from MC", channel=1, is_dm=False, transport="meshcore")
    block = ctx.get_context_block(transport="meshcore")
    assert "hello from MC" in block
    assert "hello from MT" not in block


def test_get_context_block_empty_when_no_matching_transport():
    ctx = MeshContext()
    ctx.observe("Alice", "a1", "hello from MT", channel=0, is_dm=False, transport="meshtastic")
    block = ctx.get_context_block(transport="meshcore")
    assert block == ""


# ---------------------------------------------------------------------------
# (c) Meshtastic channel-index filter is skipped for MeshCore observations
# ---------------------------------------------------------------------------

def test_meshtastic_channel_filter_skipped_for_meshcore():
    """MeshCore observations bypass the Meshtastic _observe_channels index filter.

    observe_channels=[0] would normally block channel=5 for Meshtastic, but a
    MeshCore observation with channel=5 must still be recorded.
    """
    ctx = MeshContext(observe_channels=[0])  # only Meshtastic ch0 allowed
    # MeshCore channel slot 5: should NOT be filtered by Meshtastic indices
    ctx.observe("MCNode", "mc1", "meshcore msg", channel=5, is_dm=False, transport="meshcore")
    # Meshtastic ch5: should be filtered out
    ctx.observe("MTNode", "mt1", "meshtastic msg", channel=5, is_dm=False, transport="meshtastic")

    assert ctx.count == 1
    block = ctx.get_context_block()
    assert "meshcore msg" in block
    assert "meshtastic msg" not in block


def test_meshtastic_channel_filter_still_applies_to_meshtastic():
    """Meshtastic channel-index filter still blocks unlisted Meshtastic channels."""
    ctx = MeshContext(observe_channels=[0])
    ctx.observe("MTNode", "mt1", "ch0 msg", channel=0, is_dm=False, transport="meshtastic")
    ctx.observe("MTNode", "mt1", "ch1 msg", channel=1, is_dm=False, transport="meshtastic")
    assert ctx.count == 1
    block = ctx.get_context_block()
    assert "ch0 msg" in block
    assert "ch1 msg" not in block


# ---------------------------------------------------------------------------
# (d) ContextConfig.max_age default is 14 days
# ---------------------------------------------------------------------------

def test_context_config_max_age_default_is_14_days():
    cfg = ContextConfig()
    assert cfg.max_age == 1_209_600, (
        f"Expected 1_209_600 (14 days) but got {cfg.max_age}"
    )
