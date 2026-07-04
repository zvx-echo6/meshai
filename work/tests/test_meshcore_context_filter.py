"""Unit tests for the MeshCore passive-context / bot-behavior filter.

Covers the pure module-level helper ``mc_context_allows`` — no hardware,
no meshcore lib, no event loop.  MeshMessage and MeshCoreContextConfig are
constructed directly.
"""

from meshai.config import MeshCoreContextConfig
from meshai.connector import MeshMessage
from meshai.transport.meshcore_transport import mc_context_allows


def _dm(sender_id="a1b2", sender_name="Alice", text="hi"):
    return MeshMessage(
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        channel=0,
        is_dm=True,
        transport="meshcore",
    )


def _chan(channel=1, text="hello", sender_id=None, sender_name=None):
    marker = f"chan:{channel}"
    return MeshMessage(
        sender_id=sender_id or marker,
        sender_name=sender_name or marker,
        text=text,
        channel=channel,
        is_dm=False,
        transport="meshcore",
    )


def test_cfg_none_always_passes():
    assert mc_context_allows(None, _dm(), {}) is True
    assert mc_context_allows(None, _chan(), {}) is True


def test_defaults_pass_through():
    # With opt-in semantics: empty observe_channels = observe NONE for channels.
    # DMs still pass (respond_to_dms=True by default).
    cfg = MeshCoreContextConfig()  # empty lists, passive on, respond_to_dms on
    idx_to_name = {1: "#general"}
    # Channel: empty observe_channels now means NONE are observed (opt-in)
    assert mc_context_allows(cfg, _chan(channel=1), idx_to_name) is False
    assert mc_context_allows(cfg, _dm(), idx_to_name) is True


def test_observe_channels_filters_by_name():
    cfg = MeshCoreContextConfig(observe_channels=["#aida"])
    idx_to_name = {1: "#general", 2: "#aida"}
    # idx 1 -> #general -> not in observe list -> DROPPED
    assert mc_context_allows(cfg, _chan(channel=1), idx_to_name) is False
    # idx 2 -> #aida -> in observe list -> PASSES
    assert mc_context_allows(cfg, _chan(channel=2), idx_to_name) is True
    # idx 3 -> no name mapping -> DROPPED
    assert mc_context_allows(cfg, _chan(channel=3), idx_to_name) is False


def test_ignore_contacts_matches_id_or_name():
    cfg = MeshCoreContextConfig(ignore_contacts=["a1b2"])
    # matched by sender_id
    assert mc_context_allows(cfg, _dm(sender_id="a1b2", sender_name="Alice"), {}) is False
    # matched by sender_name
    assert mc_context_allows(cfg, _dm(sender_id="ffff", sender_name="a1b2"), {}) is False
    # unrelated DM passes
    assert mc_context_allows(cfg, _dm(sender_id="c3d4", sender_name="Bob"), {}) is True


def test_respond_to_dms_false_drops_dms_only():
    # observe_channels must be non-empty for channel msgs to be forwarded
    # (opt-in: empty = none).  Only DM behavior is being tested here.
    cfg = MeshCoreContextConfig(respond_to_dms=False, observe_channels=["#general"])
    idx_to_name = {1: "#general"}
    # any DM dropped
    assert mc_context_allows(cfg, _dm(), idx_to_name) is False
    assert mc_context_allows(cfg, _dm(sender_id="zzzz", sender_name="Zed"), idx_to_name) is False
    # channel msgs unaffected (passive still on, channel in observe list)
    assert mc_context_allows(cfg, _chan(channel=1), idx_to_name) is True


def test_passive_disabled_drops_channel_but_dm_still_respected():
    cfg = MeshCoreContextConfig(enable_passive_context=False)
    idx_to_name = {1: "#general"}
    # channel msg dropped
    assert mc_context_allows(cfg, _chan(channel=1), idx_to_name) is False
    # DM still respects respond_to_dms (True by default) -> passes
    assert mc_context_allows(cfg, _dm(), idx_to_name) is True
