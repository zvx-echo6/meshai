"""Tests for MeshCoreTransport._normalize_channel_event's "Name: message"
sender-identity parsing (aurora-openwebui-backend channel-mention feature).

MeshCore channel text arrives as "SenderName: message" when a sender is
known; this parses that prefix so a channel turn can be attributed to WHO
sent it (resolved to a stable pubkey-prefix id via the contact list when
possible, else a namespaced "mcname:<name>" id), instead of collapsing
every sender on a channel into one shared "chan:N" identity. Text with no
such prefix keeps the legacy chan:N marker identity (fallback only).

No real socket, no meshcore lib required -- MeshCoreTransport.__init__ and
_normalize_channel_event/_resolve_sender_id_by_name never lazy-import the
meshcore package, so these tests construct the transport directly and poke
at ``_mc``/``_chan_name_to_idx`` by hand.
"""
from unittest.mock import MagicMock

from meshai.config import ConnectionConfig
from meshai.transport.meshcore_transport import MeshCoreTransport


def _mc_config(**overrides) -> ConnectionConfig:
    cfg = ConnectionConfig(meshcore_host="127.0.0.1", meshcore_port=5050)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_channel_event(text="chan msg", channel_idx=2, **extra):
    e = MagicMock()
    e.payload = {"type": "CHAN", "channel_idx": channel_idx, "text": text, **extra}
    return e


def _transport(contacts=None, chan_map=None) -> MeshCoreTransport:
    """A MeshCoreTransport with a fake ``_mc.contacts`` and a fake
    ``_chan_name_to_idx`` (channel NAME -> slot), no real connection."""
    t = MeshCoreTransport(_mc_config())
    if contacts is not None:
        mc = MagicMock()
        mc.contacts = contacts
        t._mc = mc
    if chan_map is not None:
        t._chan_name_to_idx = dict(chan_map)
    return t


# ---------------------------------------------------------------------------
# No "Name: " prefix -> legacy chan:N fallback identity, text untouched
# ---------------------------------------------------------------------------


def test_no_prefix_falls_back_to_chan_marker():
    t = _transport()
    msg = t._normalize_channel_event(_make_channel_event(text="just some traffic", channel_idx=4))
    assert msg is not None
    assert msg.sender_id == "chan:4"
    assert msg.sender_name == "chan:4"
    assert msg.text == "just some traffic"


def test_leading_bare_colon_falls_back_to_chan_marker():
    """":" with an empty name before it is not a valid "Name: " prefix --
    the whole original text is kept and identity falls back to chan:N."""
    t = _transport()
    msg = t._normalize_channel_event(_make_channel_event(text=": hello", channel_idx=0))
    assert msg.sender_id == "chan:0"
    assert msg.text == ": hello"


def test_name_with_no_body_falls_back():
    """"Name: " with nothing after it (empty remainder) is not accepted as
    a prefix either -- there's no message body to attribute."""
    t = _transport()
    msg = t._normalize_channel_event(_make_channel_event(text="Bob: ", channel_idx=0))
    assert msg.sender_id == "chan:0"
    assert msg.text == "Bob: "


# ---------------------------------------------------------------------------
# "Name: text" prefix parsing
# ---------------------------------------------------------------------------


def test_prefix_parsed_unresolved_contact_uses_mcname_id():
    """A "Name: " prefix with no matching contact resolves to a namespaced
    mcname:<name> id, per the task's fallback rule."""
    t = _transport(contacts={})
    msg = t._normalize_channel_event(_make_channel_event(text="Bob: hello there", channel_idx=0))
    assert msg.sender_name == "Bob"
    assert msg.sender_id == "mcname:Bob"
    assert msg.text == "hello there"


def test_prefix_parsed_resolved_contact_uses_pubkey_prefix():
    """A "Name: " prefix matching a known contact resolves sender_id to
    that contact's 12-hex-char pubkey prefix (the same convention used
    elsewhere in this module -- get_contacts()'s "prefix" field, etc.)."""
    contacts = {
        "deadbeefcafefeedfacefeed00000000": {
            "adv_name": "Alice",
            "public_key": "deadbeefcafefeedfacefeed00000000",
        }
    }
    t = _transport(contacts=contacts)
    msg = t._normalize_channel_event(_make_channel_event(text="Alice: on my way", channel_idx=0))
    assert msg.sender_name == "Alice"
    assert msg.sender_id == "deadbeefcafe"  # first 12 hex chars of public_key
    assert msg.text == "on my way"


def test_prefix_name_match_is_case_insensitive():
    contacts = {
        "aabbccddeeff00112233445566778899": {
            "adv_name": "Alice",
            "public_key": "aabbccddeeff00112233445566778899",
        }
    }
    t = _transport(contacts=contacts)
    msg = t._normalize_channel_event(_make_channel_event(text="ALICE: hi", channel_idx=0))
    assert msg.sender_id == "aabbccddeeff"


def test_colon_later_in_body_does_not_confuse_the_split():
    """The message body may itself contain a later ": " -- only the FIRST
    ": " is the sender-name delimiter."""
    t = _transport(contacts={})
    msg = t._normalize_channel_event(
        _make_channel_event(text="Bob: check this: really", channel_idx=0)
    )
    assert msg.sender_name == "Bob"
    assert msg.sender_id == "mcname:Bob"
    assert msg.text == "check this: really"


def test_emoji_name_parses_like_any_other_name():
    t = _transport(contacts={})
    msg = t._normalize_channel_event(_make_channel_event(text="🔥Bob: fire nearby", channel_idx=0))
    assert msg.sender_name == "🔥Bob"
    assert msg.sender_id == "mcname:🔥Bob"
    assert msg.text == "fire nearby"


def test_prefix_parsing_never_raises_when_mc_is_none():
    """No self._mc (never connected / unit test double) -> resolution just
    falls back to the mcname: id, never raises."""
    t = MeshCoreTransport(_mc_config())
    assert t._mc is None
    msg = t._normalize_channel_event(_make_channel_event(text="Bob: hi", channel_idx=0))
    assert msg.sender_id == "mcname:Bob"


# ---------------------------------------------------------------------------
# channel_name resolution (idx -> configured channel NAME, for the
# mention-channels gate in router.should_respond)
# ---------------------------------------------------------------------------


def test_channel_name_resolved_from_chan_name_to_idx():
    t = _transport(chan_map={"#aida": 3, "general": 0})
    msg = t._normalize_channel_event(_make_channel_event(text="hi", channel_idx=3))
    assert msg.channel_name == "#aida"
    assert msg.channel == 3


def test_channel_name_none_when_idx_unknown():
    t = _transport(chan_map={"#aida": 3})
    msg = t._normalize_channel_event(_make_channel_event(text="hi", channel_idx=9))
    assert msg.channel_name is None


def test_channel_name_none_when_no_channel_table():
    t = _transport()
    msg = t._normalize_channel_event(_make_channel_event(text="hi", channel_idx=0))
    assert msg.channel_name is None
