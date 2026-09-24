"""Tests for the registered !addme CommandHandler (registry/help visibility
only -- the real MeshCore flow is tested in tests/test_meshcore_addme.py).

Confirms the "do nothing on Meshtastic" requirement: !addme reached via the
normal command dispatcher (as would happen for a Meshtastic message, or a
MeshCore DM) returns "" so no reply is sent.
"""
import asyncio
from unittest.mock import MagicMock

from meshai.commands.addme import AddMeCommand
from meshai.commands.dispatcher import create_dispatcher


def _make_context(transport_hint="meshtastic"):
    ctx = MagicMock()
    ctx.sender_id = "!abcd1234"
    ctx.sender_name = "TestNode"
    ctx.channel = 0
    ctx.is_dm = False
    ctx.position = None
    ctx.config = MagicMock()
    ctx.connector = MagicMock()
    ctx.history = MagicMock()
    return ctx


def test_addme_command_does_nothing_on_meshtastic():
    cmd = AddMeCommand()
    ctx = _make_context()
    result = asyncio.run(cmd.execute("", ctx))
    assert result == ""


def test_addme_command_does_nothing_with_args():
    cmd = AddMeCommand()
    ctx = _make_context()
    result = asyncio.run(cmd.execute("some trailing text", ctx))
    assert result == ""


def test_addme_registered_in_dispatcher():
    dispatcher = create_dispatcher()
    names = {c.name.lower() for c in dispatcher.get_commands()}
    assert "addme" in names


def test_addme_visible_in_help_list():
    from meshai.commands.help import HelpCommand
    dispatcher = create_dispatcher()
    help_cmd = HelpCommand(dispatcher)
    ctx = _make_context()
    result = asyncio.run(help_cmd.execute("", ctx))
    assert "!addme" in result


def test_addme_help_detail():
    from meshai.commands.help import HelpCommand
    dispatcher = create_dispatcher()
    help_cmd = HelpCommand(dispatcher)
    ctx = _make_context()
    result = asyncio.run(help_cmd.execute("addme", ctx))
    assert "MeshCore only" in result
    assert "does nothing" in result.lower() or "#aida" in result
