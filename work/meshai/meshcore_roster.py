"""Pure roster helpers for the MeshCore companion: reconcile + route health.

Deliberately free of device I/O and of the ``meshcore`` lib itself, so the two
pieces of logic worth getting right — what a roster IS after a resync, and
whether the routing matrix still points at destinations that exist — can be
unit-tested without a radio.

Two independent concerns live here:

``reconcile_contacts``
    Replace-semantics for a FULL contact refetch. The lib only ever merges
    (see below), so this is what makes an operator-triggered resync able to
    *drop* entries rather than only ever grow the roster.

``check_route_health`` / ``find_name_collisions``
    Read-only checks over the companion roster + the region-routing matrix.
    Preventive: they answer "would this cell resolve if it fired right now?"
    without sending anything.
"""

from typing import Any, Optional

# MeshCore contact.type as reported by the firmware CONTACT_TYPENAMES table
# [NONE, CLI, REP, ROOM, SENS]. Mirrors MeshCoreTransport.ROOM_CONTACT_TYPE.
ROOM_CONTACT_TYPE = 3


def reconcile_contacts(
    cached: dict[str, dict], fresh: dict[str, dict]
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Reconcile a cached roster against a FULL refetch, with replace semantics.

    The meshcore lib's contact handler (``meshcore/meshcore.py::_update_contacts``)
    only ever ``.update()``s existing entries or adds new ones — it has no
    removal path. So merging a fetch into the cache can never shrink it: an
    entry removed on the companion would persist in the cache until reconnect.
    Reconciling against an authoritative full fetch restores replace semantics:
    anything absent from *fresh* is dropped.

    Per-contact FIELDS are merged (fresh wins) rather than replaced wholesale,
    which keeps the lib's field-merge behavior for contacts that still exist —
    a fresh record missing an optional field must not blank the cached one.

    IMPORTANT — the caller must only pass a *fresh* that came from a SUCCESSFUL
    full fetch (``get_contacts(lastmod=0)`` returning a CONTACTS event). This
    function trusts *fresh* as authoritative: an empty *fresh* legitimately
    means "the companion has no contacts" and will empty the roster. Passing a
    partial/failed fetch here would silently delete real contacts.

    Args:
        cached: pubkey -> contact dict (the lib's current mirror).
        fresh:  pubkey -> contact dict (authoritative FULL fetch).

    Returns:
        (reconciled, stats). ``stats`` carries before/after/added/removed/updated
        counts plus ``added_keys``/``removed_keys`` (sorted pubkey lists) so the
        caller can report exactly what a resync changed.
    """
    reconciled: dict[str, dict] = {}
    added: list[str] = []
    updated: list[str] = []

    for pubkey, contact in fresh.items():
        previous = cached.get(pubkey)
        if previous is None:
            reconciled[pubkey] = dict(contact)
            added.append(pubkey)
            continue
        merged = {**previous, **contact}
        reconciled[pubkey] = merged
        if merged != previous:
            updated.append(pubkey)

    removed = [pubkey for pubkey in cached if pubkey not in fresh]

    stats: dict[str, Any] = {
        "before": len(cached),
        "after": len(reconciled),
        "added": len(added),
        "removed": len(removed),
        "updated": len(updated),
        "added_keys": sorted(added),
        "removed_keys": sorted(removed),
    }
    return reconciled, stats


def _find_contact_by_key_prefix(
    contacts: list[dict], prefix: str
) -> Optional[dict]:
    """Resolve *prefix* to a contact the way the send path does.

    Mirrors ``meshcore.MeshCore.get_contact_by_key_prefix``: case-insensitive
    ``startswith`` on the contact's pubkey. Room routing cells are resolved
    through that same call (``MeshCoreTransport._resolve_contact``), so the
    health check MUST use identical matching or it would report a cell as
    dangling that the dispatcher can in fact resolve (a 12-hex prefix from the
    room picker is a legitimate cell value, not just a full key).
    """
    if not prefix:
        return None
    needle = prefix.lower()
    for contact in contacts:
        pubkey = (contact.get("pubkey") or "").lower()
        if pubkey.startswith(needle):
            return contact
    return None


def check_route_health(
    cells: dict,
    channel_names: list[str],
    contacts: list[dict],
) -> list[dict]:
    """Flag region-routing cells whose MeshCore target does not exist.

    Preventive check — it resolves each cell's ``mc`` target against the live
    companion roster/channel table and reports the ones that would not resolve.
    Nothing is sent.

    A cell's ``mc`` value is either ``room:<pubkey>`` (addressed room send) or a
    bare channel NAME; the parse is delegated to the single canonical
    implementation in ``meshai.notifications.channels`` so this can never drift
    from what the dispatcher actually does.

    Cells with no ``mc`` target are skipped (nothing to resolve). Disabled cells
    ARE still checked and returned with ``enabled: False`` — a broken cell is
    worth surfacing before someone re-enables it — so callers should weight the
    enabled ones when deciding how loudly to complain.

    Args:
        cells: ``region_routes.cells`` — family -> region -> cell dict.
        channel_names: channel NAMES on the companion (``known_channels()``).
        contacts: roster dicts with at least ``pubkey``/``name``/``type``.

    Returns:
        A list of offending cells, each
        ``{family, region, target, kind, reason, enabled}``. Empty list = healthy.
    """
    # Lazy import: keeps this module dependency-free at import time and avoids
    # pulling the notifications stack (httpx/smtplib) into transport callers.
    from meshai.notifications.channels import parse_meshcore_room  # noqa: PLC0415

    known = {name for name in channel_names}
    problems: list[dict] = []

    for family, regions in (cells or {}).items():
        if not isinstance(regions, dict):
            continue
        for region, cell in regions.items():
            if not isinstance(cell, dict):
                continue
            target = cell.get("mc")
            if not target or not isinstance(target, str):
                continue
            enabled = bool(cell.get("enabled", True))

            room_pubkey = parse_meshcore_room(target)
            if room_pubkey is not None:
                contact = _find_contact_by_key_prefix(contacts, room_pubkey)
                if contact is None:
                    problems.append({
                        "family": family,
                        "region": region,
                        "target": target,
                        "kind": "room",
                        "reason": "room_not_found",
                        "enabled": enabled,
                    })
                elif contact.get("type") != ROOM_CONTACT_TYPE:
                    # Resolves to a real contact that is not a room server —
                    # an addressed send would go to the wrong kind of node.
                    problems.append({
                        "family": family,
                        "region": region,
                        "target": target,
                        "kind": "room",
                        "reason": "not_a_room",
                        "enabled": enabled,
                    })
                continue

            if target not in known:
                problems.append({
                    "family": family,
                    "region": region,
                    "target": target,
                    "kind": "channel",
                    "reason": "channel_not_found",
                    "enabled": enabled,
                })

    return problems


def find_name_collisions(contacts: list[dict]) -> list[dict]:
    """Group roster entries that share a name but have different pubkeys.

    A name is not a stable identifier on MeshCore — two operators can advertise
    the same ``adv_name`` from different keypairs. Anything that picks a target
    by name alone (a room picker, an operator reading a table) cannot tell them
    apart, so surfacing the collision is what makes the ambiguity visible.

    Only same-name/different-pubkey groups are returned; duplicates of the same
    pubkey are not a collision.

    Returns:
        ``[{name, count, contacts: [{pubkey, type}, ...]}]``, sorted by name.
        Empty list = no collisions.
    """
    by_name: dict[str, dict[str, dict]] = {}
    for contact in contacts:
        name = contact.get("name")
        if not name:
            continue
        pubkey = contact.get("pubkey") or ""
        if not pubkey:
            continue
        by_name.setdefault(name, {})[pubkey] = contact

    collisions: list[dict] = []
    for name, keyed in by_name.items():
        if len(keyed) < 2:
            continue
        collisions.append({
            "name": name,
            "count": len(keyed),
            "contacts": [
                {"pubkey": pubkey, "type": c.get("type")}
                for pubkey, c in sorted(keyed.items())
            ],
        })

    return sorted(collisions, key=lambda c: c["name"])
