"""Tests for MeshContext.update_settings — live hot-reload without restart.

Covers:
  - update_settings applies max_age, observe_channels, ignore_nodes in place.
  - Normalization mirrors __init__ exactly (empty list → None for
    observe_channels; empty list/None → set() for ignore_nodes).
  - An observation older than a newly-shortened max_age is pruned and
    subsequently excluded by get_context_block.
  - Omitting a parameter leaves the current value unchanged.
"""

import time

from meshai.context import MeshContext, MeshObservation


def _make_context(**kwargs) -> MeshContext:
    return MeshContext(**kwargs)


class TestUpdateSettingsNormalization:
    """update_settings mirrors __init__ normalization rules."""

    def test_max_age_updated(self):
        ctx = _make_context(max_age=3600)
        ctx.update_settings(max_age=7200)
        assert ctx._max_age == 7200

    def test_observe_channels_non_empty_becomes_set(self):
        ctx = _make_context()
        ctx.update_settings(observe_channels=[1, 2, 3])
        assert ctx._observe_channels == {1, 2, 3}

    def test_observe_channels_empty_list_becomes_none(self):
        """Empty list → None (observe all), mirroring constructor behaviour."""
        ctx = _make_context(observe_channels=[1, 2])
        ctx.update_settings(observe_channels=[])
        assert ctx._observe_channels is None

    def test_observe_channels_none_arg_leaves_unchanged(self):
        """Passing None as argument leaves _observe_channels unchanged."""
        ctx = _make_context(observe_channels=[5])
        ctx.update_settings(observe_channels=None)
        assert ctx._observe_channels == {5}

    def test_ignore_nodes_non_empty_becomes_set(self):
        ctx = _make_context()
        ctx.update_settings(ignore_nodes=["!abc", "!def"])
        assert ctx._ignore_nodes == {"!abc", "!def"}

    def test_ignore_nodes_empty_list_becomes_empty_set(self):
        """Empty list → set(), mirroring constructor behaviour."""
        ctx = _make_context(ignore_nodes=["!abc"])
        ctx.update_settings(ignore_nodes=[])
        assert ctx._ignore_nodes == set()

    def test_ignore_nodes_none_arg_leaves_unchanged(self):
        """Passing None as argument leaves _ignore_nodes unchanged."""
        ctx = _make_context(ignore_nodes=["!abc"])
        ctx.update_settings(ignore_nodes=None)
        assert ctx._ignore_nodes == {"!abc"}

    def test_omitted_params_unchanged(self):
        """Only supplied params are modified; others stay as-is."""
        ctx = _make_context(max_age=100, observe_channels=[3], ignore_nodes=["!x"])
        ctx.update_settings(max_age=999)
        assert ctx._max_age == 999
        assert ctx._observe_channels == {3}
        assert ctx._ignore_nodes == {"!x"}


class TestPruneAfterMaxAgeShortened:
    """After update_settings shortens max_age, prune() removes old entries
    and get_context_block returns only the surviving observations."""

    def _insert_obs(self, ctx: MeshContext, timestamp: float, text: str = "msg"):
        """Bypass observe() to inject an observation with an arbitrary timestamp."""
        obs = MeshObservation(
            timestamp=timestamp,
            sender_name="TestNode",
            sender_id="!test1",
            channel=0,
            is_dm=False,
            text=text,
        )
        ctx._buffer.append(obs)

    def test_old_observation_pruned_after_max_age_shortened(self):
        now = time.time()

        # Start with a generous max_age (1 hour).
        ctx = _make_context(max_age=3600)

        # Insert one observation that is 30 minutes old.
        self._insert_obs(ctx, now - 1800, text="old message")
        assert ctx.count == 1
        assert "old message" in ctx.get_context_block()

        # Shorten max_age to 10 minutes — the 30-minute-old observation is now stale.
        ctx.update_settings(max_age=600)
        assert ctx._max_age == 600

        # prune() is the documented mechanism to remove expired observations.
        pruned = ctx.prune()
        assert pruned == 1
        assert ctx.count == 0
        assert ctx.get_context_block() == ""

    def test_recent_observation_survives_after_max_age_shortened(self):
        now = time.time()

        ctx = _make_context(max_age=3600)

        # Insert one old and one recent observation.
        self._insert_obs(ctx, now - 1800, text="old message")
        self._insert_obs(ctx, now - 60, text="recent message")

        # Shorten max_age to 10 minutes — only the old one is stale.
        ctx.update_settings(max_age=600)
        ctx.prune()

        assert ctx.count == 1
        block = ctx.get_context_block()
        assert "recent message" in block
        assert "old message" not in block
