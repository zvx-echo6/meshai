"""Tests for meshai/persistence/addme.py (v32 migration + accessors)."""
from meshai.persistence import get_db
from meshai.persistence.addme import (
    get_addme_contact,
    list_addme_contacts,
    record_addme_contact,
)


def test_record_and_get_addme_contact():
    conn = get_db()
    record_addme_contact("AA" * 32, "Bob", "corescope-signed-advert", added_at=1000.0, conn=conn)
    row = get_addme_contact("aa" * 32, conn=conn)
    assert row is not None
    assert row["pubkey"] == "aa" * 32
    assert row["name"] == "Bob"
    assert row["source"] == "corescope-signed-advert"
    assert row["added_at"] == 1000.0


def test_record_addme_contact_upserts():
    conn = get_db()
    record_addme_contact("bb" * 32, "Bob", "unsigned-import", added_at=1.0, conn=conn)
    record_addme_contact("bb" * 32, "Bobby", "corescope-signed-advert", added_at=2.0, conn=conn)
    row = get_addme_contact("bb" * 32, conn=conn)
    assert row["name"] == "Bobby"
    assert row["source"] == "corescope-signed-advert"
    assert row["added_at"] == 2.0
    assert len(list_addme_contacts(conn=conn)) == 1


def test_get_addme_contact_missing_returns_none():
    conn = get_db()
    assert get_addme_contact("cc" * 32, conn=conn) is None


def test_list_addme_contacts_orders_most_recent_first():
    conn = get_db()
    record_addme_contact("dd" * 32, "First", "unsigned-import", added_at=1.0, conn=conn)
    record_addme_contact("ee" * 32, "Second", "unsigned-import", added_at=2.0, conn=conn)
    rows = list_addme_contacts(conn=conn)
    pubkeys = [r["pubkey"] for r in rows]
    assert pubkeys.index("ee" * 32) < pubkeys.index("dd" * 32)
