"""Tests for MeshCore roster management: reconcile, route health, name collisions.

Covers:
  - reconcile_contacts(): full-refetch replace semantics (the crux — a merge
    can never remove, so this is what lets a resync drop stale entries)
  - check_route_health(): region-routing cells pointing at absent rooms/channels
  - find_name_collisions(): same name, different pubkey
  - MeshCoreTransport.self_info(): reports the ACTUAL connection, never a
    config value belonging to a different conn_type
  - MeshCoreTransport._refresh_contacts_async / remove_contact / import_contact

The companion is mocked throughout: no device I/O, no mesh traffic, nothing
removed from a real radio.
"""

import asyncio
import sys
import types

import pytest

from meshai.meshcore_roster import (
    check_route_health,
    find_name_collisions,
    reconcile_contacts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contact(pubkey: str, name: str = "node", type_: int = 1, **extra) -> dict:
    """A lib-shaped contact dict (public_key is the lib's key field)."""
    base = {
        "public_key": pubkey,
        "adv_name": name,
        "type": type_,
        "last_advert": 1000,
        "out_path_len": -1,
        "out_path": "",
        "out_path_hash_mode": 0,
        "flags": 0,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
    }
    base.update(extra)
    return base


def _roster(pubkey: str, name: str = "node", type_: int = 1) -> dict:
    """A roster-projection contact dict (as get_contacts() returns)."""
    return {"pubkey": pubkey, "name": name, "type": type_, "last_advert": 1000}


KEY_A = "aa" * 32
KEY_B = "bb" * 32
KEY_C = "cc" * 32


# ---------------------------------------------------------------------------
# Part 1: reconcile_contacts — replace semantics
# ---------------------------------------------------------------------------

class TestReconcileContacts:
    def test_absent_contact_is_dropped(self):
        """The crux: a contact missing from the FULL refetch is removed.

        The lib's own fetch handler only merges, so without this a deleted
        contact survives in the cache forever.
        """
        cached = {KEY_A: _contact(KEY_A, "alpha"), KEY_B: _contact(KEY_B, "bravo")}
        fresh = {KEY_A: _contact(KEY_A, "alpha")}

        reconciled, stats = reconcile_contacts(cached, fresh)

        assert KEY_B not in reconciled
        assert set(reconciled) == {KEY_A}
        assert stats["removed"] == 1
        assert stats["removed_keys"] == [KEY_B]
        assert stats["before"] == 2
        assert stats["after"] == 1

    def test_new_contact_is_added(self):
        cached = {KEY_A: _contact(KEY_A, "alpha")}
        fresh = {KEY_A: _contact(KEY_A, "alpha"), KEY_B: _contact(KEY_B, "bravo")}

        reconciled, stats = reconcile_contacts(cached, fresh)

        assert set(reconciled) == {KEY_A, KEY_B}
        assert stats["added"] == 1
        assert stats["added_keys"] == [KEY_B]
        assert stats["removed"] == 0

    def test_changed_contact_is_updated(self):
        cached = {KEY_A: _contact(KEY_A, "alpha", last_advert=1000)}
        fresh = {KEY_A: _contact(KEY_A, "alpha", last_advert=2000)}

        reconciled, stats = reconcile_contacts(cached, fresh)

        assert reconciled[KEY_A]["last_advert"] == 2000
        assert stats["updated"] == 1
        assert stats["added"] == 0
        assert stats["removed"] == 0

    def test_unchanged_contact_not_counted_as_updated(self):
        cached = {KEY_A: _contact(KEY_A, "alpha")}
        fresh = {KEY_A: _contact(KEY_A, "alpha")}

        _, stats = reconcile_contacts(cached, fresh)

        assert stats["updated"] == 0
        assert stats["added"] == 0
        assert stats["removed"] == 0
        assert stats["after"] == 1

    def test_fields_merge_rather_than_replace(self):
        """A fresh record missing an optional field must not blank the cached one."""
        cached = {KEY_A: _contact(KEY_A, "alpha", nickname="local-only")}
        fresh = {KEY_A: {"public_key": KEY_A, "adv_name": "alpha-renamed"}}

        reconciled, _ = reconcile_contacts(cached, fresh)

        assert reconciled[KEY_A]["adv_name"] == "alpha-renamed"   # fresh wins
        assert reconciled[KEY_A]["nickname"] == "local-only"      # survives
        assert reconciled[KEY_A]["type"] == 1                     # survives

    def test_add_remove_and_update_together(self):
        cached = {
            KEY_A: _contact(KEY_A, "alpha", last_advert=1000),
            KEY_B: _contact(KEY_B, "bravo"),
        }
        fresh = {
            KEY_A: _contact(KEY_A, "alpha", last_advert=2000),
            KEY_C: _contact(KEY_C, "charlie"),
        }

        reconciled, stats = reconcile_contacts(cached, fresh)

        assert set(reconciled) == {KEY_A, KEY_C}
        assert (stats["added"], stats["removed"], stats["updated"]) == (1, 1, 1)
        assert stats["added_keys"] == [KEY_C]
        assert stats["removed_keys"] == [KEY_B]

    def test_empty_fresh_empties_roster(self):
        """An authoritative full fetch of zero contacts means zero contacts.

        Guarding this would mean a genuinely-wiped companion could never be
        reflected; the caller is responsible for only passing a SUCCESSFUL fetch.
        """
        cached = {KEY_A: _contact(KEY_A), KEY_B: _contact(KEY_B)}

        reconciled, stats = reconcile_contacts(cached, {})

        assert reconciled == {}
        assert stats["removed"] == 2
        assert stats["after"] == 0

    def test_empty_cache_adds_everything(self):
        fresh = {KEY_A: _contact(KEY_A), KEY_B: _contact(KEY_B)}

        reconciled, stats = reconcile_contacts({}, fresh)

        assert set(reconciled) == {KEY_A, KEY_B}
        assert stats["added"] == 2
        assert stats["before"] == 0

    def test_does_not_mutate_inputs(self):
        cached = {KEY_A: _contact(KEY_A, "alpha", last_advert=1000)}
        fresh = {KEY_B: _contact(KEY_B, "bravo")}
        cached_snapshot = {KEY_A: dict(cached[KEY_A])}

        reconcile_contacts(cached, fresh)

        assert cached == cached_snapshot   # caller's cache untouched
        assert set(fresh) == {KEY_B}

    def test_merge_semantics_alone_can_never_remove(self):
        """Contrast: the lib's merge keeps a stale entry that reconcile drops.

        Documents exactly what the reconcile adds over the lib's behavior.
        """
        cached = {KEY_A: _contact(KEY_A), KEY_B: _contact(KEY_B)}
        fresh = {KEY_A: _contact(KEY_A)}

        merged = dict(cached)
        merged.update(fresh)                    # what the lib does
        reconciled, _ = reconcile_contacts(cached, fresh)   # what we do

        assert KEY_B in merged                  # stale entry survives a merge
        assert KEY_B not in reconciled          # ...and is dropped by reconcile


# ---------------------------------------------------------------------------
# Part 2: check_route_health — dangling routing cells
# ---------------------------------------------------------------------------

class TestCheckRouteHealth:
    CHANNELS = ["Public", "#aida", "#sw-id-aida"]

    def test_healthy_cells_report_nothing(self):
        cells = {
            "weather": {
                "SW Idaho": {"mc": "#sw-id-aida", "enabled": True},
                "SC Idaho": {"mc": f"room:{KEY_A}", "enabled": True},
            }
        }
        contacts = [_roster(KEY_A, "SC Room", type_=3)]

        assert check_route_health(cells, self.CHANNELS, contacts) == []

    def test_cell_pointing_at_missing_room_is_flagged(self):
        cells = {"weather": {"SC Idaho": {"mc": f"room:{KEY_B}", "enabled": True}}}
        contacts = [_roster(KEY_A, "SC Room", type_=3)]

        problems = check_route_health(cells, self.CHANNELS, contacts)

        assert len(problems) == 1
        assert problems[0]["family"] == "weather"
        assert problems[0]["region"] == "SC Idaho"
        assert problems[0]["kind"] == "room"
        assert problems[0]["reason"] == "room_not_found"
        assert problems[0]["enabled"] is True

    def test_cell_pointing_at_missing_channel_is_flagged(self):
        cells = {"fire": {"East Idaho": {"mc": "#e-id-aida", "enabled": True}}}

        problems = check_route_health(cells, self.CHANNELS, [])

        assert len(problems) == 1
        assert problems[0]["kind"] == "channel"
        assert problems[0]["reason"] == "channel_not_found"
        assert problems[0]["target"] == "#e-id-aida"

    def test_room_cell_resolving_to_non_room_is_flagged(self):
        """A room cell that resolves to a plain node would address the wrong kind."""
        cells = {"weather": {"SC Idaho": {"mc": f"room:{KEY_A}", "enabled": True}}}
        contacts = [_roster(KEY_A, "Just A Node", type_=1)]

        problems = check_route_health(cells, self.CHANNELS, contacts)

        assert len(problems) == 1
        assert problems[0]["reason"] == "not_a_room"

    def test_room_cell_matches_by_prefix(self):
        """The send path resolves rooms by pubkey PREFIX — so must this check.

        A 12-hex prefix (what the room picker stores) is a legitimate cell
        value; treating it as dangling would be a false alarm.
        """
        cells = {"weather": {"SC Idaho": {"mc": f"room:{KEY_A[:12]}", "enabled": True}}}
        contacts = [_roster(KEY_A, "SC Room", type_=3)]

        assert check_route_health(cells, self.CHANNELS, contacts) == []

    def test_room_prefix_match_is_case_insensitive(self):
        cells = {"weather": {"SC Idaho": {"mc": f"room:{KEY_A[:12].upper()}", "enabled": True}}}
        contacts = [_roster(KEY_A, "SC Room", type_=3)]

        assert check_route_health(cells, self.CHANNELS, contacts) == []

    def test_disabled_cell_still_reported_but_marked(self):
        cells = {"weather": {"SC Idaho": {"mc": f"room:{KEY_B}", "enabled": False}}}

        problems = check_route_health(cells, self.CHANNELS, [])

        assert len(problems) == 1
        assert problems[0]["enabled"] is False

    def test_cell_without_mc_target_is_skipped(self):
        cells = {"weather": {"SW Idaho": {"mt": 3, "mc": None, "enabled": True}}}

        assert check_route_health(cells, self.CHANNELS, []) == []

    def test_empty_room_pubkey_treated_as_channel_name(self):
        """``room:`` with no pubkey is not a room target (parser yields None)."""
        cells = {"weather": {"SC Idaho": {"mc": "room:", "enabled": True}}}

        problems = check_route_health(cells, self.CHANNELS, [])

        assert len(problems) == 1
        assert problems[0]["kind"] == "channel"

    def test_empty_cells_and_missing_families(self):
        assert check_route_health({}, self.CHANNELS, []) == []
        assert check_route_health({"weather": None}, self.CHANNELS, []) == []

    def test_multiple_families_and_regions(self):
        cells = {
            "weather": {
                "SW Idaho": {"mc": "#sw-id-aida", "enabled": True},   # healthy
                "SC Idaho": {"mc": f"room:{KEY_B}", "enabled": True},  # dangling
            },
            "fire": {
                "East Idaho": {"mc": "#gone", "enabled": True},        # dangling
            },
        }
        contacts = [_roster(KEY_A, "SC Room", type_=3)]

        problems = check_route_health(cells, self.CHANNELS, contacts)

        assert len(problems) == 2
        assert {p["reason"] for p in problems} == {"room_not_found", "channel_not_found"}


# ---------------------------------------------------------------------------
# Part 3: find_name_collisions
# ---------------------------------------------------------------------------

class TestFindNameCollisions:
    def test_same_name_different_pubkey_is_a_collision(self):
        contacts = [
            _roster(KEY_A, "SC ID AIDA Alerts", type_=3),
            _roster(KEY_B, "SC ID AIDA Alerts", type_=3),
        ]

        collisions = find_name_collisions(contacts)

        assert len(collisions) == 1
        assert collisions[0]["name"] == "SC ID AIDA Alerts"
        assert collisions[0]["count"] == 2
        assert {c["pubkey"] for c in collisions[0]["contacts"]} == {KEY_A, KEY_B}

    def test_distinct_names_are_not_collisions(self):
        contacts = [_roster(KEY_A, "SC ID AIDA"), _roster(KEY_B, "SC ID AIDA Alerts")]

        assert find_name_collisions(contacts) == []

    def test_same_pubkey_twice_is_not_a_collision(self):
        contacts = [_roster(KEY_A, "dup"), _roster(KEY_A, "dup")]

        assert find_name_collisions(contacts) == []

    def test_unnamed_contacts_ignored(self):
        contacts = [_roster(KEY_A, None), _roster(KEY_B, None)]

        assert find_name_collisions(contacts) == []

    def test_empty_roster(self):
        assert find_name_collisions([]) == []


# ---------------------------------------------------------------------------
# Part 4: transport — self_info() connection reporting
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_meshcore(monkeypatch):
    """Register a fake ``meshcore`` module for the lazy imports in the transport.

    monkeypatch.setitem (not sys.modules.setdefault) so this wins even when the
    real lib — or another test module's fake — is already imported, and is
    restored afterwards.
    """
    mod = types.ModuleType("meshcore")

    class EventType:
        ERROR = "ERROR"
        CONTACTS = "CONTACTS"
        OK = "OK"

    mod.EventType = EventType
    monkeypatch.setitem(sys.modules, "meshcore", mod)
    return mod


class _Event:
    def __init__(self, type_, payload=None):
        self.type = type_
        self.payload = payload or {}


class _FakeCommands:
    """Records calls; returns whatever the test queues up."""

    def __init__(self):
        self.get_contacts_calls = []
        self.removed = []
        self.added = []
        self.get_contacts_result = None
        self.remove_result = _Event("OK")
        self.add_result = _Event("OK")

    async def get_contacts(self, lastmod=0, timeout=5):
        self.get_contacts_calls.append(lastmod)
        return self.get_contacts_result

    async def remove_contact(self, key):
        self.removed.append(key)
        return self.remove_result

    async def add_contact(self, contact):
        self.added.append(contact)
        return self.add_result


class _FakeMC:
    def __init__(self, contacts=None):
        self._contacts = dict(contacts or {})
        self._contacts_dirty = True
        self._lastmod = 500
        self.commands = _FakeCommands()
        self.auto_update_contacts = False

    @property
    def contacts(self):
        return self._contacts


def _transport(**cfg_kwargs):
    """Build a transport with a fake MC attached and marked connected."""
    from meshai.config import ConnectionConfig
    from meshai.transport.meshcore_transport import MeshCoreTransport

    transport = MeshCoreTransport(ConnectionConfig(**cfg_kwargs))
    return transport


class TestSelfInfoConnectionReporting:
    """self_info() must describe the ACTUAL connection, never a stale config value."""

    def test_serial_does_not_report_config_host(self):
        """The bug: a serial connection reporting a leftover meshcore_host.

        meshcore_host/port are never read on the serial path, so surfacing them
        names a device meshai is not talking to — which is what sends an
        investigation to the wrong physical radio.
        """
        t = _transport(
            meshcore_conn_type="serial",
            meshcore_serial_port="/dev/meshcore-rak",
            meshcore_baud=115200,
            # A stale TCP host left in config from a previous companion:
            meshcore_host="192.168.1.253",
            meshcore_port=5050,
        )
        t._mc = _FakeMC()
        t._connected = True
        t._self_info = {"name": "AIDA", "public_key": KEY_A}

        info = t.self_info()

        assert info["conn_type"] == "serial"
        assert info["serial_port"] == "/dev/meshcore-rak"
        assert info["baud"] == 115200
        assert info["target"] == "serial:/dev/meshcore-rak@115200"
        # The stale host must NOT be surfaced:
        assert info["host"] is None
        assert info["port"] is None
        # Identity still comes from the real device:
        assert info["name"] == "AIDA"
        assert info["pubkey"] == KEY_A

    def test_tcp_reports_host_and_port(self):
        t = _transport(
            meshcore_conn_type="tcp",
            meshcore_host="100.64.0.9",
            meshcore_port=5050,
        )
        t._mc = _FakeMC()
        t._connected = True
        t._self_info = {"name": "TCPNode", "public_key": KEY_B}

        info = t.self_info()

        assert info["conn_type"] == "tcp"
        assert info["host"] == "100.64.0.9"
        assert info["port"] == 5050
        assert info["target"] == "100.64.0.9:5050"
        assert info["serial_port"] is None
        assert info["baud"] is None

    def test_ble_reports_address_only(self):
        t = _transport(
            meshcore_conn_type="ble",
            meshcore_ble_address="AA:BB:CC:DD:EE:FF",
            meshcore_host="192.168.1.253",
        )
        t._mc = _FakeMC()
        t._connected = True
        t._self_info = {"name": "BleNode", "public_key": KEY_C}

        info = t.self_info()

        assert info["conn_type"] == "ble"
        assert info["ble_address"] == "AA:BB:CC:DD:EE:FF"
        assert info["target"] == "ble:AA:BB:CC:DD:EE:FF"
        assert info["host"] is None
        assert info["port"] is None

    def test_not_connected_reports_only_connected_false(self):
        t = _transport(meshcore_conn_type="serial", meshcore_serial_port="/dev/x")

        assert t.self_info() == {"connected": False}

    def test_descriptor_matches_connect_log_target(self):
        """connect() and self_info() must never disagree about the target."""
        t = _transport(
            meshcore_conn_type="serial",
            meshcore_serial_port="/dev/meshcore-rak",
            meshcore_baud=115200,
        )
        t._mc = _FakeMC()
        t._connected = True
        t._self_info = {}

        assert t.self_info()["target"] == t._connection_descriptor()["target"]


# ---------------------------------------------------------------------------
# Part 5: transport — refresh / remove / import against a mocked companion
# ---------------------------------------------------------------------------

class TestRefreshContactsAsync:
    def test_full_refetch_uses_lastmod_zero_and_reconciles(self, fake_meshcore):
        """The resync must be FULL (lastmod=0), not the lib's incremental fetch.

        An incremental fetch cannot see a contact whose last_advert predates
        _lastmod, and merging its result could never drop the stale KEY_B.
        """
        t = _transport(meshcore_conn_type="serial", meshcore_serial_port="/dev/x")
        mc = _FakeMC({KEY_A: _contact(KEY_A, "alpha"), KEY_B: _contact(KEY_B, "bravo")})
        mc.commands.get_contacts_result = _Event(
            "CONTACTS", {KEY_A: _contact(KEY_A, "alpha"), KEY_C: _contact(KEY_C, "charlie")}
        )
        t._mc = mc
        t._connected = True

        stats = asyncio.run(t._refresh_contacts_async())

        assert mc.commands.get_contacts_calls == [0]        # FULL, not _lastmod
        assert set(mc._contacts) == {KEY_A, KEY_C}          # cache replaced in place
        assert stats["removed"] == 1 and stats["added"] == 1
        assert t._contacts_synced_at is not None

    def test_payload_rekeyed_by_public_key(self, fake_meshcore):
        """The event payload may be keyed by anything; the cache is by pubkey."""
        t = _transport()
        mc = _FakeMC()
        mc.commands.get_contacts_result = _Event(
            "CONTACTS", {"some-other-key": _contact(KEY_A, "alpha")}
        )
        t._mc = mc
        t._connected = True

        asyncio.run(t._refresh_contacts_async())

        assert set(mc._contacts) == {KEY_A}

    def test_error_event_leaves_cache_untouched(self, fake_meshcore):
        """A failed fetch must never be treated as authoritative — that would
        delete the entire roster."""
        t = _transport()
        mc = _FakeMC({KEY_A: _contact(KEY_A), KEY_B: _contact(KEY_B)})
        mc.commands.get_contacts_result = _Event("ERROR", {"reason": "timeout"})
        t._mc = mc
        t._connected = True

        with pytest.raises(RuntimeError, match="timeout"):
            asyncio.run(t._refresh_contacts_async())

        assert set(mc._contacts) == {KEY_A, KEY_B}   # intact

    def test_no_response_raises(self, fake_meshcore):
        t = _transport()
        mc = _FakeMC({KEY_A: _contact(KEY_A)})
        mc.commands.get_contacts_result = None
        t._mc = mc
        t._connected = True

        with pytest.raises(RuntimeError, match="no response"):
            asyncio.run(t._refresh_contacts_async())

        assert set(mc._contacts) == {KEY_A}

    def test_cache_object_identity_preserved(self, fake_meshcore):
        """The lib mutates _contacts in place; replacing the dict would orphan it."""
        t = _transport()
        mc = _FakeMC({KEY_B: _contact(KEY_B)})
        original = mc._contacts
        mc.commands.get_contacts_result = _Event("CONTACTS", {KEY_A: _contact(KEY_A)})
        t._mc = mc
        t._connected = True

        asyncio.run(t._refresh_contacts_async())

        assert mc._contacts is original


class TestResync:
    """resync() must re-read BOTH halves of the connect-time device view.

    Channels are enumerated once at connect (_enumerate_channels) and never
    re-read, so a channel provisioned on the radio afterwards stays invisible
    until the process restarts — the resync is the only path that picks it up.
    """

    def _transport_with_loop(self):
        """A transport whose _run_coro works (real loop, fake device)."""
        import threading

        t = _transport(meshcore_conn_type="serial", meshcore_serial_port="/dev/x")
        mc = _FakeMC({KEY_A: _contact(KEY_A, "alpha"), KEY_B: _contact(KEY_B, "bravo")})
        mc.commands.get_contacts_result = _Event("CONTACTS", {KEY_A: _contact(KEY_A, "alpha")})
        t._mc = mc
        t._connected = True
        t._loop = asyncio.new_event_loop()
        threading.Thread(
            target=lambda: (asyncio.set_event_loop(t._loop), t._loop.run_forever()),
            daemon=True,
        ).start()
        for _ in range(50):
            if t._loop.is_running():
                break
            __import__("time").sleep(0.02)
        return t, mc

    def test_resync_reports_contact_and_channel_deltas(self, fake_meshcore, monkeypatch):
        t, mc = self._transport_with_loop()
        try:
            t._chan_name_to_idx = {"#aida": 1, "#old": 2}

            # Stand in for the companion's channel table on re-enumeration:
            # #old is gone, #new appeared.
            def fake_enumerate():
                t._chan_name_to_idx = {"#aida": 1, "#new": 3}

            monkeypatch.setattr(t, "_enumerate_channels", fake_enumerate)

            result = t.resync()

            assert result["contacts"]["removed"] == 1          # KEY_B dropped
            assert result["channels"]["added"] == ["#new"]
            assert result["channels"]["removed"] == ["#old"]
            assert result["channels"]["before"] == 2
            assert result["channels"]["after"] == 2
        finally:
            t._loop.call_soon_threadsafe(t._loop.stop)

    def test_resync_raises_when_not_connected(self):
        with pytest.raises(RuntimeError, match="not connected"):
            _transport().resync()


class TestRemoveContact:
    def test_rejects_prefix_requiring_full_key(self):
        """A prefix could match the wrong node — and a wrong delete is permanent."""
        t = _transport()
        t._mc = _FakeMC()
        t._connected = True

        with pytest.raises(ValueError, match="full 64-character"):
            t.remove_contact(KEY_A[:12])

    def test_rejects_non_hex(self):
        t = _transport()
        t._mc = _FakeMC()
        t._connected = True

        with pytest.raises(ValueError, match="Invalid pubkey hex"):
            t.remove_contact("z" * 64)

    def test_raises_when_not_connected(self):
        t = _transport()

        with pytest.raises(RuntimeError, match="not connected"):
            t.remove_contact(KEY_A)


class TestImportContact:
    def test_rejects_record_without_full_pubkey(self):
        t = _transport()
        t._mc = _FakeMC()
        t._connected = True

        with pytest.raises(ValueError, match="full 64-character"):
            t.import_contact({"pubkey": "abcd", "name": "x"})

    def test_raises_when_not_connected(self):
        t = _transport()

        with pytest.raises(RuntimeError, match="not connected"):
            t.import_contact({"pubkey": KEY_A})


class TestExportRoster:
    def test_export_carries_importable_fields(self):
        """An export missing the update_contact field set cannot be re-imported."""
        t = _transport()
        t._mc = _FakeMC({KEY_A: _contact(KEY_A, "alpha", out_path_len=2, out_path="abcd")})
        t._connected = True

        records = t.export_roster()

        assert len(records) == 1
        record = records[0]
        for field in (
            "name", "pubkey", "type", "flags", "last_advert",
            "adv_lat", "adv_lon", "out_path", "out_path_len", "out_path_hash_mode",
        ):
            assert field in record
        assert record["pubkey"] == KEY_A
        assert record["name"] == "alpha"
        assert record["path_established"] is True

    def test_export_marks_flood_only_contact(self):
        t = _transport()
        t._mc = _FakeMC({KEY_A: _contact(KEY_A, "alpha", out_path_len=-1)})
        t._connected = True

        assert t.export_roster()[0]["path_established"] is False

    def test_export_empty_when_not_connected(self):
        assert _transport().export_roster() == []
