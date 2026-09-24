"""Unit tests for meshai.dedupe.InboundDedupeCache -- the bounded,
time-limited de-dupe cache used to drop a client's automatic resend of an
inbound DM/channel message (see router.py's should_respond()).
"""
from meshai.dedupe import InboundDedupeCache


class _FakeClock:
    """Deterministic, manually-advanced clock for testing TTL/window logic
    without real sleeps."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# id-keyed (stable msg_key: MeshCore sender_timestamp / Meshtastic packet id)
# ---------------------------------------------------------------------------


def test_same_key_within_ttl_is_duplicate():
    clock = _FakeClock()
    cache = InboundDedupeCache(clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is False
    # Same sender/text/timestamp seen again shortly after -- duplicate.
    clock.advance(1.0)
    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is True
    clock.advance(1.0)
    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is True


def test_new_msg_key_is_not_a_duplicate():
    """A genuinely new message (new sender_timestamp), even with identical
    text moments later, must be answered."""
    clock = _FakeClock()
    cache = InboundDedupeCache(clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is False
    clock.advance(1.0)
    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=222) is False


def test_id_keyed_entry_expires_after_ttl():
    clock = _FakeClock()
    cache = InboundDedupeCache(ttl_seconds=600.0, clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is False
    clock.advance(601.0)
    # Same key, but past the TTL -- treated as fresh again.
    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is False


def test_different_sender_same_key_is_not_a_duplicate():
    clock = _FakeClock()
    cache = InboundDedupeCache(clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi", msg_key=111) is False
    assert cache.is_duplicate("meshcore", "bob", "hi", msg_key=111) is False


def test_meshtastic_packet_id_dedupe():
    """Meshtastic packet id used the same way as MeshCore's sender_timestamp."""
    clock = _FakeClock()
    cache = InboundDedupeCache(clock=clock)

    assert cache.is_duplicate("meshtastic", "!bob0001", "status?", msg_key=("pkt", 555)) is False
    clock.advance(2.0)
    assert cache.is_duplicate("meshtastic", "!bob0001", "status?", msg_key=("pkt", 555)) is True
    # A different packet id (new transmission) is not a duplicate.
    assert cache.is_duplicate("meshtastic", "!bob0001", "status?", msg_key=("pkt", 556)) is False


# ---------------------------------------------------------------------------
# fallback (no stable msg_key)
# ---------------------------------------------------------------------------


def test_fallback_within_window_is_duplicate():
    clock = _FakeClock()
    cache = InboundDedupeCache(fallback_window_seconds=90.0, clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi") is False
    clock.advance(10.0)
    assert cache.is_duplicate("meshcore", "alice", "hi") is True


def test_fallback_after_window_is_answered():
    """A genuine later repeat of the same text, outside the short fallback
    window, must still be answered."""
    clock = _FakeClock()
    cache = InboundDedupeCache(fallback_window_seconds=90.0, clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi") is False
    clock.advance(91.0)
    assert cache.is_duplicate("meshcore", "alice", "hi") is False


def test_fallback_different_text_is_not_a_duplicate():
    clock = _FakeClock()
    cache = InboundDedupeCache(clock=clock)

    assert cache.is_duplicate("meshcore", "alice", "hi") is False
    assert cache.is_duplicate("meshcore", "alice", "bye") is False


# ---------------------------------------------------------------------------
# Bounded size (LRU-ish eviction)
# ---------------------------------------------------------------------------


def test_bounded_size_evicts_oldest():
    clock = _FakeClock()
    cache = InboundDedupeCache(max_size=3, clock=clock)

    cache.is_duplicate("meshcore", "u1", "m1", msg_key=1)
    clock.advance(0.1)
    cache.is_duplicate("meshcore", "u2", "m2", msg_key=2)
    clock.advance(0.1)
    cache.is_duplicate("meshcore", "u3", "m3", msg_key=3)
    clock.advance(0.1)
    # Pushes the u1/m1 entry out (bound is 3).
    cache.is_duplicate("meshcore", "u4", "m4", msg_key=4)

    assert len(cache._entries) == 3
    # The evicted (oldest) entry is treated as fresh again.
    assert cache.is_duplicate("meshcore", "u1", "m1", msg_key=1) is False
