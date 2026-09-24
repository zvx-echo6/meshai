"""Tests for the MeshCore !addme feature (meshai/meshcore_addme.py +
MeshCoreTransport detection hook).

All tests are fully mocked: no real socket, no meshcore lib, no network.
CoreScope HTTP calls are faked by injecting a stub `httpx` module into
sys.modules (mirroring tests/test_mention_detection.py's shim), so these
tests pass whether or not the real `httpx` package is installed.
"""
import asyncio
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from meshai.config import ConnectionConfig, MeshCoreContextConfig
from meshai.meshcore_addme import (
    AddmeResolution,
    build_addme_reply,
    handle_addme_trigger,
    is_addme_trigger,
    resolve_addme_pubkey,
    resolve_from_contacts,
)


# ---------------------------------------------------------------------------
# is_addme_trigger (pure regex)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("!addme", True),
    ("!ADDME", True),
    ("!AddMe please", True),
    ("!addme now please", True),
    ("  !addme  ", True),
    ("!addmenot", False),
    ("hello !addme", False),
    ("", False),
    (None, False),
    ("!add me", False),
])
def test_is_addme_trigger(text, expected):
    assert is_addme_trigger(text) is expected


# ---------------------------------------------------------------------------
# resolve_from_contacts
# ---------------------------------------------------------------------------

def test_resolve_from_contacts_exact_match():
    contacts = [{"name": "Bob", "pubkey": "aa" * 32}, {"name": "Alice", "pubkey": "bb" * 32}]
    result = resolve_from_contacts("Bob", contacts)
    assert result.status == "ok"
    assert result.pubkey == "aa" * 32
    assert result.source == "contacts"


def test_resolve_from_contacts_case_insensitive_fallback():
    contacts = [{"name": "bob", "pubkey": "aa" * 32}]
    result = resolve_from_contacts("Bob", contacts)
    assert result.status == "ok"
    assert result.pubkey == "aa" * 32


def test_resolve_from_contacts_no_match():
    contacts = [{"name": "Alice", "pubkey": "bb" * 32}]
    result = resolve_from_contacts("Bob", contacts)
    assert result.status == "none"


def test_resolve_from_contacts_ambiguous():
    contacts = [
        {"name": "Bob", "pubkey": "aa" * 32},
        {"name": "Bob", "pubkey": "cc" * 32},
    ]
    result = resolve_from_contacts("Bob", contacts)
    assert result.status == "ambiguous"


def test_resolve_from_contacts_same_pubkey_twice_not_ambiguous():
    contacts = [
        {"name": "Bob", "pubkey": "aa" * 32},
        {"name": "bob", "pubkey": "AA" * 32},  # same pubkey, different case
    ]
    result = resolve_from_contacts("Bob", contacts)
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# CoreScope resolution (via stubbed httpx)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _install_fake_httpx(monkeypatch, responses: dict):
    """Install a fake httpx module. `responses` maps a URL substring (e.g.
    "/api/nodes") to the JSON payload (or list) returned for a GET matching it."""

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            for key, payload in responses.items():
                if key in url:
                    return _FakeResponse(payload)
            return _FakeResponse([])

    fake_module = types.ModuleType("httpx")
    fake_module.AsyncClient = _FakeAsyncClient
    monkeypatch.setitem(sys.modules, "httpx", fake_module)


def test_resolve_from_corescope_single_match(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [
            {"pubkey": "dd" * 32, "name": "Carol", "role": "companion", "last_seen": 100},
            {"pubkey": "ee" * 32, "name": "Dave", "role": "companion", "last_seen": 200},
        ],
        "/api/packets": [
            {"pubkey": "dd" * 32, "raw_hex": "deadbeef", "signatureValid": True, "last_seen": 100},
        ],
    })
    result = asyncio.run(resolve_addme_pubkey("Carol", contacts=[]))
    assert result.status == "ok"
    assert result.pubkey == "dd" * 32
    assert result.source == "corescope"
    assert result.raw_advert_hex == "deadbeef"


def test_resolve_from_corescope_ignores_noncompanion_role(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [
            {"pubkey": "dd" * 32, "name": "Carol", "role": "repeater", "last_seen": 100},
        ],
        "/api/packets": [],
    })
    result = asyncio.run(resolve_addme_pubkey("Carol", contacts=[]))
    assert result.status == "none"


def test_resolve_from_corescope_zero_match(monkeypatch):
    _install_fake_httpx(monkeypatch, {"/api/nodes": [], "/api/packets": []})
    result = asyncio.run(resolve_addme_pubkey("Nobody", contacts=[]))
    assert result.status == "none"


def test_resolve_from_corescope_ambiguous(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [
            {"pubkey": "dd" * 32, "name": "Carol", "role": "companion", "last_seen": 100},
            {"pubkey": "ff" * 32, "name": "Carol", "role": "companion", "last_seen": 200},
        ],
        "/api/packets": [],
    })
    result = asyncio.run(resolve_addme_pubkey("Carol", contacts=[]))
    assert result.status == "ambiguous"


def test_resolve_from_corescope_no_signed_advert(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [
            {"pubkey": "dd" * 32, "name": "Carol", "role": "companion", "last_seen": 100},
        ],
        "/api/packets": [
            {"pubkey": "dd" * 32, "raw_hex": "abc", "signatureValid": False},
        ],
    })
    result = asyncio.run(resolve_addme_pubkey("Carol", contacts=[]))
    assert result.status == "ok"
    assert result.raw_advert_hex is None


def test_resolve_addme_pubkey_prefers_contacts_over_corescope(monkeypatch):
    """A contacts-list match must short-circuit before any CoreScope call."""
    def _boom(*a, **kw):
        raise AssertionError("CoreScope should not be queried when contacts resolve")

    fake_module = types.ModuleType("httpx")

    class _ExplodingClient:
        def __init__(self, *a, **kw):
            _boom()

    fake_module.AsyncClient = _ExplodingClient
    monkeypatch.setitem(sys.modules, "httpx", fake_module)

    contacts = [{"name": "Bob", "pubkey": "aa" * 32}]
    result = asyncio.run(resolve_addme_pubkey("Bob", contacts))
    assert result.status == "ok"
    assert result.source == "contacts"


# ---------------------------------------------------------------------------
# build_addme_reply
# ---------------------------------------------------------------------------

def test_build_addme_reply_success():
    text = build_addme_reply("Bob", "success")
    assert text == "@[Bob] added you and sent you a DM. Keep the AIDA contact starting 4b54."


def test_build_addme_reply_success_with_cooldown_note():
    text = build_addme_reply("Bob", "success", advert_skipped_minutes=42)
    assert text.startswith("@[Bob] added you and sent you a DM.")
    assert "I advertised 42 min ago." in text


def test_build_addme_reply_none():
    text = build_addme_reply("Bob", "none")
    assert text == (
        "@[Bob] I can't find your node yet. Send a flood advert from your "
        "app, wait a minute, then !addme again."
    )


def test_build_addme_reply_ambiguous():
    text = build_addme_reply("Bob", "ambiguous")
    assert text.startswith("@[Bob] more than one node is named Bob")
    assert "4b54" in text


# ---------------------------------------------------------------------------
# handle_addme_trigger orchestration, against a fake duck-typed transport
# ---------------------------------------------------------------------------

@dataclass
class _FakeMsg:
    sender_name: str
    channel_name: str
    text: str = "!addme"
    is_dm: bool = False


class _FakeTransport:
    def __init__(self, contacts=None, addme_config=None, max_chars=140):
        self._mc_context = addme_config or MeshCoreContextConfig()
        self._addme_last_advert = None
        self._addme_user_cooldowns: dict = {}
        self.max_chars = max_chars
        self._contacts = contacts or []
        self.advert_calls = 0
        self.advert_result = True
        self.import_calls = []
        self.import_signed_calls = []
        self.import_signed_result = True
        self.sent = []  # list of dicts: {text, destination, meshcore_channel}

    def get_contacts(self):
        return list(self._contacts)

    async def send_advert_async(self):
        self.advert_calls += 1
        return self.advert_result

    def import_contact(self, record):
        self.import_calls.append(record)

    def import_contact_signed_advert(self, raw_bytes):
        self.import_signed_calls.append(raw_bytes)
        return self.import_signed_result

    async def send_message_async(self, text, destination=None, meshcore_channel=None, **kw):
        self.sent.append({"text": text, "destination": destination, "meshcore_channel": meshcore_channel})
        return True


def _run(coro):
    return asyncio.run(coro)


async def _drain():
    """Let any scheduled fire-and-forget tasks (the delayed DM) run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_handle_addme_trigger_success_sends_advert_add_and_dm():
    t = _FakeTransport(
        contacts=[{"name": "Bob", "pubkey": "aa" * 32}],
        addme_config=MeshCoreContextConfig(addme_dm_delay_seconds=0),
    )
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()

    _run(scenario())

    assert t.advert_calls == 1
    assert t._addme_last_advert is not None
    # already a contact -> no (re)import
    assert t.import_calls == []
    assert t.import_signed_calls == []
    # channel reply
    channel_msgs = [m for m in t.sent if m["meshcore_channel"] == "#aida"]
    assert len(channel_msgs) == 1
    assert channel_msgs[0]["text"] == "@[Bob] added you and sent you a DM. Keep the AIDA contact starting 4b54."
    # DM
    dm_msgs = [m for m in t.sent if m["destination"] == "aa" * 32]
    assert len(dm_msgs) == 1
    assert "Bob" in dm_msgs[0]["text"]
    assert dm_msgs[0]["text"].startswith("Hi Bob, AIDA here.")


def test_handle_addme_trigger_no_match_sends_no_advert_no_dm(monkeypatch):
    _install_fake_httpx(monkeypatch, {"/api/nodes": [], "/api/packets": []})
    t = _FakeTransport(contacts=[])
    msg = _FakeMsg(sender_name="Ghost", channel_name="#aida")

    _run(handle_addme_trigger(t, msg))

    assert t.advert_calls == 0
    assert t.import_calls == []
    assert t.sent == [{
        "text": "@[Ghost] I can't find your node yet. Send a flood advert from your "
                "app, wait a minute, then !addme again.",
        "destination": None,
        "meshcore_channel": "#aida",
    }]


def test_handle_addme_trigger_ambiguous_sends_no_advert_no_dm():
    t = _FakeTransport(contacts=[
        {"name": "Bob", "pubkey": "aa" * 32},
        {"name": "Bob", "pubkey": "cc" * 32},
    ])
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    _run(handle_addme_trigger(t, msg))

    assert t.advert_calls == 0
    assert t.import_calls == []
    assert len(t.sent) == 1
    assert "more than one node is named Bob" in t.sent[0]["text"]


def test_handle_addme_trigger_advert_cooldown_skipped_and_noted():
    import time as _time
    t = _FakeTransport(contacts=[{"name": "Bob", "pubkey": "aa" * 32}])
    t._addme_last_advert = _time.time() - 120  # 2 min ago
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()

    _run(scenario())

    assert t.advert_calls == 0  # cooldown active (default 3600s), advert skipped
    channel_msgs = [m for m in t.sent if m["meshcore_channel"] == "#aida"]
    assert "I advertised 2 min ago." in channel_msgs[0]["text"]


def test_handle_addme_trigger_advert_sent_once_then_skipped_second_time():
    t = _FakeTransport(contacts=[
        {"name": "Bob", "pubkey": "aa" * 32},
        {"name": "Alice", "pubkey": "bb" * 32},
    ])

    async def scenario():
        await handle_addme_trigger(t, _FakeMsg(sender_name="Bob", channel_name="#aida"))
        await _drain()
        await handle_addme_trigger(t, _FakeMsg(sender_name="Alice", channel_name="#aida"))
        await _drain()

    _run(scenario())

    assert t.advert_calls == 1  # second request (different asker) skipped the advert


def test_handle_addme_trigger_per_user_cooldown_silently_ignored():
    t = _FakeTransport(
        contacts=[{"name": "Bob", "pubkey": "aa" * 32}],
        addme_config=MeshCoreContextConfig(addme_per_user_cooldown_seconds=300),
    )
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()
        t.sent.clear()
        t.advert_calls = 0
        await handle_addme_trigger(t, msg)  # repeat, still within cooldown
        await _drain()

    _run(scenario())

    assert t.sent == []  # silently ignored: no reply at all
    assert t.advert_calls == 0


def test_handle_addme_trigger_signed_import_used_when_available(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [{"pubkey": "dd" * 32, "name": "Carol", "role": "companion", "last_seen": 1}],
        "/api/packets": [{"pubkey": "dd" * 32, "raw_hex": "deadbeef", "signatureValid": True, "last_seen": 1}],
    })
    t = _FakeTransport(contacts=[])  # not already a contact
    msg = _FakeMsg(sender_name="Carol", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()

    _run(scenario())

    assert t.import_signed_calls == [bytes.fromhex("deadbeef")]
    assert t.import_calls == []  # unsigned path not used


def test_handle_addme_trigger_falls_back_to_unsigned_when_signed_fails(monkeypatch):
    _install_fake_httpx(monkeypatch, {
        "/api/nodes": [{"pubkey": "dd" * 32, "name": "Carol", "role": "companion", "last_seen": 1}],
        "/api/packets": [{"pubkey": "dd" * 32, "raw_hex": "deadbeef", "signatureValid": True, "last_seen": 1}],
    })
    t = _FakeTransport(contacts=[])
    t.import_signed_result = False  # signed CMD 0x12 rejects
    msg = _FakeMsg(sender_name="Carol", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()

    _run(scenario())

    assert t.import_signed_calls == [bytes.fromhex("deadbeef")]
    assert len(t.import_calls) == 1
    assert t.import_calls[0]["pubkey"] == "dd" * 32


def test_handle_addme_trigger_unsigned_when_no_raw_advert():
    t = _FakeTransport(contacts=[])
    # Resolve via contacts requires the pubkey to already be listed, but we
    # want "not yet a contact" -- simulate resolution via a name that IS in
    # contacts (so no CoreScope roundtrip) but with a distinct "new" pubkey
    # not present under get_contacts()'s dedupe set. Simplest: patch
    # resolve to bypass contacts entirely by using an unmatched contacts
    # list plus a direct AddmeResolution via monkeypatched resolver.
    import meshai.meshcore_addme as addme_mod

    async def fake_resolve(name, contacts, base_url=addme_mod.CORESCOPE_BASE_URL):
        return AddmeResolution(status="ok", pubkey="ee" * 32, source="contacts", raw_advert_hex=None)

    orig = addme_mod.resolve_addme_pubkey
    addme_mod.resolve_addme_pubkey = fake_resolve
    try:
        msg = _FakeMsg(sender_name="Dave", channel_name="#aida")

        async def scenario():
            await handle_addme_trigger(t, msg)
            await _drain()

        _run(scenario())
    finally:
        addme_mod.resolve_addme_pubkey = orig

    assert t.import_signed_calls == []
    assert len(t.import_calls) == 1
    assert t.import_calls[0]["pubkey"] == "ee" * 32


def test_handle_addme_trigger_dm_scheduled_after_delay():
    t = _FakeTransport(
        contacts=[{"name": "Bob", "pubkey": "aa" * 32}],
        addme_config=MeshCoreContextConfig(addme_dm_delay_seconds=0.05),
    )
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        # Immediately after handle_addme_trigger returns, the DM has NOT
        # been sent yet -- only the channel reply.
        assert not any(m["destination"] == "aa" * 32 for m in t.sent)
        await asyncio.sleep(0.15)

    _run(scenario())

    dm_msgs = [m for m in t.sent if m["destination"] == "aa" * 32]
    assert len(dm_msgs) == 1


def test_handle_addme_trigger_dm_text_uses_configured_template():
    t = _FakeTransport(
        contacts=[{"name": "Bob", "pubkey": "aa" * 32}],
        addme_config=MeshCoreContextConfig(
            addme_dm_delay_seconds=0, addme_dm_text="Hey {name}, custom text.",
        ),
    )
    msg = _FakeMsg(sender_name="Bob", channel_name="#aida")

    async def scenario():
        await handle_addme_trigger(t, msg)
        await _drain()

    _run(scenario())

    dm_msgs = [m for m in t.sent if m["destination"] == "aa" * 32]
    assert dm_msgs[0]["text"] == "Hey Bob, custom text."


# ---------------------------------------------------------------------------
# Transport-level detection: MeshCoreTransport._is_addme_trigger /
# _on_channel_event bypass of respond_to_channel_mentions / observe_channels
# ---------------------------------------------------------------------------

def _mc_config(**overrides) -> ConnectionConfig:
    cfg = ConnectionConfig(meshcore_host="127.0.0.1", meshcore_port=5050)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_channel_event(text="chan msg", channel_idx=2, **extra):
    e = MagicMock()
    e.payload = {"type": "CHAN", "channel_idx": channel_idx, "text": text, **extra}
    return e


def _make_transport(addme_config=None, chan_map=None, self_info=None):
    from meshai.transport.meshcore_transport import MeshCoreTransport
    t = MeshCoreTransport(_mc_config(), meshcore_context=addme_config or MeshCoreContextConfig())
    t._chan_name_to_idx = dict(chan_map or {"#aida": 2})
    if self_info is not None:
        t._self_info = self_info
    return t


def test_addme_trigger_detected_and_dispatched_in_allowed_channel(monkeypatch):
    calls = []

    async def fake_handle(transport, msg):
        calls.append(msg)

    monkeypatch.setattr(
        "meshai.transport.meshcore_transport.handle_addme_trigger", fake_handle
    )
    t = _make_transport()

    async def scenario():
        t._on_channel_event(_make_channel_event(text="Bob: !addme", channel_idx=2))
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(calls) == 1
    assert calls[0].sender_name == "Bob"
    assert calls[0].text == "!addme"


def test_addme_ignored_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "meshai.transport.meshcore_transport.handle_addme_trigger",
        lambda *a, **kw: calls.append(1),
    )
    t = _make_transport(addme_config=MeshCoreContextConfig(addme_enabled=False))

    dispatched = []
    t._dispatch_message = lambda msg: dispatched.append(msg)

    async def scenario():
        t._on_channel_event(_make_channel_event(text="Bob: !addme", channel_idx=2))
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert calls == []


def test_addme_ignored_in_other_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "meshai.transport.meshcore_transport.handle_addme_trigger",
        lambda *a, **kw: calls.append(1),
    )
    t = _make_transport(chan_map={"#other": 5})
    t._dispatch_message = lambda msg: None

    async def scenario():
        t._on_channel_event(_make_channel_event(text="Bob: !addme", channel_idx=5))
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert calls == []


def test_addme_ignored_for_own_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "meshai.transport.meshcore_transport.handle_addme_trigger",
        lambda *a, **kw: calls.append(1),
    )
    t = _make_transport(self_info={"name": "AIDA"})
    t._dispatch_message = lambda msg: None

    async def scenario():
        t._on_channel_event(_make_channel_event(text="AIDA: !addme", channel_idx=2))
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert calls == []


def test_addme_not_triggered_without_bang_prefix():
    t = _make_transport()
    msg = t._normalize_channel_event(_make_channel_event(text="Bob: hello there", channel_idx=2))
    assert t._is_addme_trigger(msg) is False


def test_non_addme_channel_message_still_reaches_normal_dispatch(monkeypatch):
    """A plain (non-!addme) channel message must still flow through the
    ordinary mc_context_allows -> _dispatch_message path unaffected."""
    dispatched = []
    t = _make_transport(
        addme_config=MeshCoreContextConfig(enable_passive_context=True, observe_channels=["#aida"])
    )
    t._dispatch_message = lambda msg: dispatched.append(msg)

    t._on_channel_event(_make_channel_event(text="Bob: hello there", channel_idx=2))

    assert len(dispatched) == 1
    assert dispatched[0].text == "hello there"
