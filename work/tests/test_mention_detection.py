"""Tests for router.py's channel-@mention detection (mention_present /
strip_mention), used by should_respond()'s opt-in channel-reply gate and by
route() to strip the mention token before the query reaches the LLM.

Case-insensitive, word-boundary aware:
  Meshtastic: "@AIDA" (any configured name) or "@!a1daa1da" / "@a1daa1da"
              (the bot's own node id, parsed from bot.mt_node).
  MeshCore:   "@AIDA" or the bracket form "@[AIDA]". No node-id form.
"""
import sys
import types

import pytest

try:
    import pydantic  # noqa: F401
    _NEEDS_SDK_STUBS = False
except Exception:
    _NEEDS_SDK_STUBS = True

if _NEEDS_SDK_STUBS:
    # Same gap as test_openai_backend.py / test_router_thinking_notice.py:
    # this dev environment lacks `pydantic`, a transitive dependency of the
    # real openai/anthropic/google-genai SDKs, so importing meshai.router
    # (which imports meshai.backends, which eagerly imports all three
    # backend modules) fails before reaching the module under test. Nothing
    # here touches a real SDK client, so minimal stubs are enough.
    if "openai" not in sys.modules:
        _openai_stub = types.ModuleType("openai")

        class _StubAsyncOpenAI:
            def __init__(self, api_key=None, base_url=None):
                self.api_key = api_key
                self.base_url = base_url
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=None)
                )

            async def close(self):
                pass

        _openai_stub.AsyncOpenAI = _StubAsyncOpenAI
        sys.modules["openai"] = _openai_stub

    if "anthropic" not in sys.modules:
        _anthropic_stub = types.ModuleType("anthropic")
        _anthropic_stub.AsyncAnthropic = object
        sys.modules["anthropic"] = _anthropic_stub

    if "google.genai" not in sys.modules:
        import google  # real namespace package; importable without pydantic

        _genai_stub = types.ModuleType("google.genai")
        _genai_stub.types = types.ModuleType("google.genai.types")
        _genai_stub.Client = object
        sys.modules["google.genai"] = _genai_stub
        google.genai = _genai_stub

    if "httpx" not in sys.modules:
        _httpx_stub = types.ModuleType("httpx")

        class _StubAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _ConnectError(Exception):
            pass

        class _TimeoutException(Exception):
            pass

        _httpx_stub.AsyncClient = _StubAsyncClient
        _httpx_stub.ConnectError = _ConnectError
        _httpx_stub.TimeoutException = _TimeoutException
        sys.modules["httpx"] = _httpx_stub

from meshai.router import mention_present, strip_mention

NAMES = ["AIDA"]
NODE_ID = "!a1daa1da"


# ---------------------------------------------------------------------------
# Meshtastic mention_present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@AIDA what's the weather", True),
        ("@aida lowercase works too", True),
        ("hey @AIDA", True),
        ("@!a1daa1da status", True),
        ("@a1daa1da status", True),
        ("@AIDAN is a person's name", False),  # word-boundary negative
        ("AIDA with no at sign", False),
        ("@AID incomplete", False),
        ("", False),
        ("@bob not aida", False),
    ],
)
def test_meshtastic_mention_present(text, expected):
    assert mention_present(text, NAMES, "meshtastic", NODE_ID) is expected


def test_meshtastic_mention_multiple_configured_names():
    names = ["AIDA", "Bot"]
    assert mention_present("@Bot are you there", names, "meshtastic", NODE_ID) is True
    assert mention_present("@AIDA are you there", names, "meshtastic", NODE_ID) is True
    assert mention_present("@Botanist no", names, "meshtastic", NODE_ID) is False


def test_meshtastic_mention_without_node_id_still_matches_name():
    assert mention_present("@AIDA hi", NAMES, "meshtastic", node_id=None) is True
    assert mention_present("@!a1daa1da hi", NAMES, "meshtastic", node_id=None) is False


# ---------------------------------------------------------------------------
# MeshCore mention_present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@AIDA what's up", True),
        ("@[AIDA] what's up", True),
        ("@[aida] lowercase bracket", True),
        ("@AIDAN is a person's name", False),
        ("@[AIDAN] is a person's name", False),
        ("no mention here", False),
    ],
)
def test_meshcore_mention_present(text, expected):
    assert mention_present(text, NAMES, "meshcore") is expected


def test_meshcore_ignores_node_id_form():
    """MeshCore identity is name-based; the Meshtastic node-id mention form
    must never match on the meshcore transport even if a node_id is passed."""
    assert mention_present("@!a1daa1da hi", NAMES, "meshcore", node_id=NODE_ID) is False


def test_no_names_or_node_id_never_matches():
    assert mention_present("@AIDA hi", [], "meshtastic", node_id=None) is False
    assert mention_present("@AIDA hi", [], "meshcore") is False


# ---------------------------------------------------------------------------
# strip_mention
# ---------------------------------------------------------------------------


def test_strip_mention_meshtastic_leading():
    assert strip_mention("@AIDA what's the weather", NAMES, "meshtastic", NODE_ID) == (
        "what's the weather"
    )


def test_strip_mention_meshtastic_mid_sentence():
    assert strip_mention("weather @AIDA please", NAMES, "meshtastic", NODE_ID) == (
        "weather please"
    )


def test_strip_mention_meshtastic_node_id_form():
    assert strip_mention("@!a1daa1da status?", NAMES, "meshtastic", NODE_ID) == "status?"


def test_strip_mention_meshcore_bracket_form():
    assert strip_mention("@[AIDA] status?", NAMES, "meshcore") == "status?"


def test_strip_mention_meshcore_bare_form():
    assert strip_mention("@AIDA status?", NAMES, "meshcore") == "status?"


def test_strip_mention_no_match_returns_unchanged_text():
    assert strip_mention("just chatting", NAMES, "meshtastic", NODE_ID) == "just chatting"


def test_strip_mention_only_first_occurrence():
    """Only the mention actually present is stripped; a name mentioned
    again inside the message body is left alone."""
    text = "@AIDA is AIDA still online?"
    result = strip_mention(text, NAMES, "meshtastic", NODE_ID)
    assert result == "is AIDA still online?"
