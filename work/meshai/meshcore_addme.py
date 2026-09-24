"""!addme: MeshCore self-service contact add, triggered from a channel.

When someone sends "!addme" (case-insensitive, trailing text allowed) in a
configured MeshCore channel (default ``#aida``), AIDA:

1. Sends a FLOOD self-advert (unless one was already sent within
   ``addme_advert_cooldown_seconds``).
2. Resolves the sender's pubkey -- first against AIDA's own contact list,
   then against the CoreScope public API (https://live.mwmesh.com) -- and
   adds them as a contact if not already one.
3. DMs the sender (after ``addme_dm_delay_seconds``, so the advert has time
   to propagate and their app can auto-add AIDA first).

This module is deliberately free of the ``meshcore`` lib itself: the
orchestration function takes a duck-typed *transport* (anything exposing the
same methods as ``MeshCoreTransport``) so the flow can be unit-tested with a
fake, mirroring ``meshai/meshcore_roster.py``.

Detection (``is_addme_trigger``) is intentionally a SEPARATE gate from
``bot.respond_to_channel_mentions`` / ``MeshCoreContextConfig.observe_channels``
-- see ``MeshCoreTransport._on_channel_event``, which calls into this module
BEFORE the passive-context filter, so !addme works even when both of those
are off.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CORESCOPE_BASE_URL = "https://live.mwmesh.com"
_CORESCOPE_TIMEOUT_SECONDS = 8.0

# Case-insensitive "!addme" with optional trailing text (a bare "!addmenot"
# or similar must NOT match -- requires a word boundary or end-of-string).
_ADDME_TRIGGER_RE = re.compile(r"(?i)^!addme(?:\s.*)?$")

# Kept short enough that a typical formatted message clears the MeshCore DM
# frame budget (MESHCORE_DM_MAX_TEXT_BYTES, meshcore_transport.py) in ONE
# frame without the byte-length guard needing to split it -- even with a
# long, multibyte {name} (e.g. a 20-char name plus an emoji: 134 UTF-8 bytes,
# still under the ~153-byte budget). The prior wording ran 181-185 bytes
# formatted, over the companion's 176-byte hard cap on its own.
DEFAULT_ADDME_DM_TEXT = (
    "Hi {name}, AIDA here. You're in my contacts, so DM me anytime. "
    "Delete any old AIDA contact starting a655; keep 4b54."
)


def is_addme_trigger(text: Optional[str]) -> bool:
    """True if *text* (already stripped of any "Name: " channel prefix) is
    an !addme invocation."""
    if not text:
        return False
    return bool(_ADDME_TRIGGER_RE.match(text.strip()))


@dataclass
class AddmeResolution:
    """Result of resolving a sender's display name to a MeshCore pubkey."""

    status: str  # "ok" | "none" | "ambiguous"
    pubkey: Optional[str] = None
    source: Optional[str] = None  # "contacts" | "corescope"
    raw_advert_hex: Optional[str] = None  # signature-valid raw advert, if CoreScope had one


def _match_contacts_by_name(name: str, contacts: list[dict]) -> list[dict]:
    """Contacts whose ``name`` matches *name*: exact (case-sensitive) first,
    falling back to case-insensitive."""
    exact = [c for c in contacts if isinstance(c, dict) and (c.get("name") or "") == name]
    if exact:
        return exact
    name_lower = name.strip().lower()
    if not name_lower:
        return []
    return [
        c for c in contacts
        if isinstance(c, dict) and (c.get("name") or "").strip().lower() == name_lower
    ]


def resolve_from_contacts(name: str, contacts: list[dict]) -> AddmeResolution:
    """Resolve *name* against AIDA's own (already-fetched) contact list."""
    matches = _match_contacts_by_name(name, contacts)
    pubkeys = {(c.get("pubkey") or "").lower() for c in matches if c.get("pubkey")}
    if len(pubkeys) == 1:
        return AddmeResolution(status="ok", pubkey=next(iter(pubkeys)), source="contacts")
    if len(pubkeys) > 1:
        return AddmeResolution(status="ambiguous")
    return AddmeResolution(status="none")


async def _corescope_get_json(path: str, *, base_url: str, params: Optional[dict] = None):
    """GET *path* off the CoreScope API and return parsed JSON, or None on
    any failure (network, timeout, non-2xx, bad JSON) -- never raises."""
    import httpx  # local import: keeps this module importable without httpx present

    try:
        async with httpx.AsyncClient(timeout=_CORESCOPE_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("CoreScope %s lookup failed: %s", path, exc)
        return None


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("nodes") or data.get("packets") or data.get("data") or []
    return []


async def _corescope_find_signed_advert(
    pubkey: str, *, base_url: str = CORESCOPE_BASE_URL
) -> Optional[dict]:
    """Most recent signature-valid ADVERT packet for *pubkey*, or None."""
    data = await _corescope_get_json("/api/packets", base_url=base_url, params={"type": "ADVERT"})
    if data is None:
        return None
    packets = _as_list(data)
    pubkey_lower = pubkey.lower()

    def _packet_pubkey(p: dict) -> str:
        return str(p.get("pubkey") or p.get("public_key") or p.get("node_pubkey") or "").lower()

    candidates = [
        p for p in packets
        if isinstance(p, dict)
        and _packet_pubkey(p) == pubkey_lower
        and p.get("signatureValid") is True
        and p.get("raw_hex")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.get("last_seen") or p.get("timestamp") or 0, reverse=True)
    return candidates[0]


async def resolve_from_corescope(
    name: str, *, base_url: str = CORESCOPE_BASE_URL
) -> AddmeResolution:
    """Resolve *name* against the CoreScope public API (companion nodes only)."""
    data = await _corescope_get_json("/api/nodes", base_url=base_url)
    if data is None:
        return AddmeResolution(status="none")
    nodes = _as_list(data)
    matches = [
        n for n in nodes
        if isinstance(n, dict) and n.get("role") == "companion" and (n.get("name") or "") == name
    ]
    pubkeys = {(n.get("pubkey") or "").lower() for n in matches if n.get("pubkey")}
    if len(pubkeys) == 0:
        return AddmeResolution(status="none")
    if len(pubkeys) > 1:
        return AddmeResolution(status="ambiguous")

    pubkey = next(iter(pubkeys))
    advert = await _corescope_find_signed_advert(pubkey, base_url=base_url)
    raw_hex = advert.get("raw_hex") if advert else None
    return AddmeResolution(status="ok", pubkey=pubkey, source="corescope", raw_advert_hex=raw_hex)


async def resolve_addme_pubkey(
    name: str, contacts: list[dict], *, base_url: str = CORESCOPE_BASE_URL
) -> AddmeResolution:
    """Resolve *name* to exactly one pubkey: AIDA's contacts first, then
    CoreScope. Ambiguity found at either stage short-circuits (no point
    checking CoreScope once contacts already show >1 distinct pubkey)."""
    result = resolve_from_contacts(name, contacts)
    if result.status != "none":
        return result
    return await resolve_from_corescope(name, base_url=base_url)


def build_addme_reply(
    sender_name: str, outcome: str, *, advert_skipped_minutes: Optional[int] = None
) -> str:
    """Channel reply text for one of "success" | "none" | "ambiguous"."""
    name = sender_name or "there"
    prefix = f"@[{name}] "
    if outcome == "success":
        text = "added you and sent you a DM. Keep the AIDA contact starting 4b54."
        if advert_skipped_minutes is not None:
            text += f" I advertised {advert_skipped_minutes} min ago."
        return prefix + text
    if outcome == "none":
        return (
            prefix
            + "I can't find your node yet. Send a flood advert from your app, "
            "wait a minute, then !addme again."
        )
    if outcome == "ambiguous":
        return (
            prefix
            + f"more than one node is named {name}, so I can't tell which is you. "
            "Rename or DM me directly after adding AIDA (4b54…)."
        )
    return prefix + "!addme hit an internal error."


def _fit_to_budget(text: str, max_chars: int) -> str:
    """Best-effort trim to *max_chars*: drop the trailing cooldown sentence
    first (least important part of the message), then hard-truncate."""
    if len(text) <= max_chars:
        return text
    trimmed = text.split(" I advertised")[0]
    if len(trimmed) <= max_chars:
        return trimmed
    return text[:max_chars]


async def _send_channel_reply(transport, channel_name: Optional[str], text: str) -> None:
    max_chars = getattr(transport, "max_chars", 140)
    text = _fit_to_budget(text, max_chars)
    try:
        await transport.send_message_async(text, meshcore_channel=channel_name)
    except Exception as exc:
        logger.warning("MeshCore: !addme channel reply failed: %s", exc)


async def _add_contact_if_needed(
    transport, contacts: list[dict], pubkey: str, name: str, resolution: AddmeResolution
) -> None:
    """Add *pubkey* as a contact if it isn't one already, preferring the
    signed-advert import (CMD 0x12) when CoreScope had a signature-valid
    advert, falling back to the plain upsert (CMD 0x09)."""
    existing = {(c.get("pubkey") or "").lower() for c in contacts if isinstance(c, dict)}
    if pubkey.lower() in existing:
        return

    added = False
    source = None

    if resolution.raw_advert_hex:
        try:
            raw_bytes = bytes.fromhex(resolution.raw_advert_hex)
            import_signed = getattr(transport, "import_contact_signed_advert", None)
            if import_signed is not None:
                added = bool(import_signed(raw_bytes))
                if added:
                    source = "corescope-signed-advert"
        except Exception as exc:
            logger.warning("MeshCore: !addme signed-advert import failed: %s", exc)
            added = False

    if not added:
        try:
            transport.import_contact({
                "pubkey": pubkey,
                "name": name,
                "type": 0,
                "out_path_len": -1,
            })
            added = True
            source = source or "unsigned-import"
        except Exception as exc:
            logger.warning("MeshCore: !addme unsigned contact import failed: %s", exc)
            added = False

    if added:
        try:
            from meshai.persistence.addme import record_addme_contact
            record_addme_contact(pubkey=pubkey, name=name, source=source or "unknown")
        except Exception as exc:
            logger.warning("MeshCore: !addme provenance record failed: %s", exc)


def _schedule_delayed_dm(transport, pubkey: str, name: str, delay_seconds: float, dm_text: str) -> None:
    """Fire-and-forget: DM *pubkey* after *delay_seconds*, without blocking
    the caller. Scheduled as a Task on whichever loop is currently running
    (the MeshCore dedicated loop, when called from _on_channel_event)."""

    async def _delayed_dm() -> None:
        await asyncio.sleep(delay_seconds)
        try:
            ok = await transport.send_message_async(dm_text, destination=pubkey)
        except Exception as exc:
            # repr(), not str() -- a bare asyncio.TimeoutError's str() is ""
            # (empty), which used to log a hard failure as a blank, silent
            # "failed: " line that read no differently from success in the
            # logs. This is a genuine SEND failure (exception raised) --
            # distinct from the no-exception "sent, no ACK" case below,
            # which means the frame reached the queue but no delivery
            # confirmation came back.
            logger.warning("MeshCore: !addme DM to %s send failed (%r)", name, exc)
            return
        if ok:
            logger.info("MeshCore: !addme DM to %s ACKed", name)
        else:
            # send_message_async returned False without raising: best-effort
            # send with no ACK (could also mean nothing was ever transmitted,
            # e.g. contact resolution failure -- see the "MC: ..." warning
            # logged at the actual send site, just above this line).
            logger.info("MeshCore: !addme DM to %s sent, no ACK", name)

    asyncio.get_event_loop().create_task(_delayed_dm())


async def handle_addme_trigger(transport, msg) -> None:
    """Full !addme flow for one inbound channel MeshMessage.

    *transport* must expose: ``_mc_context`` (MeshCoreContextConfig-like,
    for the addme_* settings), ``_addme_last_advert`` /
    ``_addme_user_cooldowns`` (mutable state, initialized on the transport),
    ``get_contacts()``, ``send_advert_async()``, ``import_contact()``,
    ``import_contact_signed_advert()`` (optional), ``send_message_async()``,
    and ``max_chars``.
    """
    cfg = transport._mc_context
    now = _time.time()
    name = msg.sender_name or ""
    name_key = name.strip().lower()
    channel_name = msg.channel_name

    per_user_cooldown = getattr(cfg, "addme_per_user_cooldown_seconds", 300)
    last_user = transport._addme_user_cooldowns.get(name_key)
    if last_user is not None and (now - last_user) < per_user_cooldown:
        logger.debug("MeshCore: !addme from %s ignored (per-user cooldown)", name)
        return
    # Mark immediately (not after completion) so a burst of repeats while
    # this request is still in flight doesn't re-trigger the whole flow.
    transport._addme_user_cooldowns[name_key] = now

    contacts = transport.get_contacts()
    resolution = await resolve_addme_pubkey(name, contacts)

    if resolution.status == "none":
        await _send_channel_reply(transport, channel_name, build_addme_reply(name, "none"))
        return
    if resolution.status == "ambiguous":
        await _send_channel_reply(transport, channel_name, build_addme_reply(name, "ambiguous"))
        return

    pubkey = resolution.pubkey
    assert pubkey  # status == "ok" guarantees this

    advert_cooldown = getattr(cfg, "addme_advert_cooldown_seconds", 3600)
    advert_skipped_minutes: Optional[int] = None
    last_advert = transport._addme_last_advert
    if last_advert is not None and (now - last_advert) < advert_cooldown:
        advert_skipped_minutes = max(0, int(round((now - last_advert) / 60)))
    else:
        try:
            sent = await transport.send_advert_async()
        except Exception as exc:
            logger.warning("MeshCore: !addme self-advert failed: %s", exc)
            sent = False
        if sent:
            transport._addme_last_advert = _time.time()

    await _add_contact_if_needed(transport, contacts, pubkey, name, resolution)

    dm_delay = getattr(cfg, "addme_dm_delay_seconds", 20)
    dm_template = getattr(cfg, "addme_dm_text", DEFAULT_ADDME_DM_TEXT)
    dm_text = dm_template.format(name=name or "there")
    _schedule_delayed_dm(transport, pubkey, name, dm_delay, dm_text)

    reply = build_addme_reply(name, "success", advert_skipped_minutes=advert_skipped_minutes)
    await _send_channel_reply(transport, channel_name, reply)
