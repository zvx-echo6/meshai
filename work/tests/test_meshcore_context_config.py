"""Config round-trip tests for the MeshCore passive-context block.

Verifies that ``meshcore_context`` survives save_config -> load_config, and
that a YAML lacking the section yields the dataclass defaults (proving the
generic nested-dataclass loader branch handles it with no special-casing).
"""

import yaml

from meshai.config import (
    Config,
    MeshCoreContextConfig,
    load_config,
    save_config,
)


def test_meshcore_context_round_trip(tmp_path):
    cfg = Config()
    cfg.meshcore_context = MeshCoreContextConfig(
        enable_passive_context=False,
        observe_channels=["#aida", "#general"],
        ignore_contacts=["a1b2", "SpamNode"],
        respond_to_dms=False,
        addme_enabled=False,
        addme_channels=["#aida", "#other"],
        addme_advert_cooldown_seconds=1800,
        addme_dm_delay_seconds=10,
        addme_per_user_cooldown_seconds=60,
        addme_dm_text="Custom {name} text",
    )

    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    loaded = load_config(path)

    mc = loaded.meshcore_context
    assert isinstance(mc, MeshCoreContextConfig)
    assert mc.enable_passive_context is False
    assert mc.observe_channels == ["#aida", "#general"]
    assert mc.ignore_contacts == ["a1b2", "SpamNode"]
    assert mc.respond_to_dms is False
    assert mc.addme_enabled is False
    assert mc.addme_channels == ["#aida", "#other"]
    assert mc.addme_advert_cooldown_seconds == 1800
    assert mc.addme_dm_delay_seconds == 10
    assert mc.addme_per_user_cooldown_seconds == 60
    assert mc.addme_dm_text == "Custom {name} text"


def test_meshcore_context_defaults_when_absent(tmp_path):
    # A minimal YAML with no meshcore_context section at all.
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"timezone": "America/Boise"}))

    loaded = load_config(path)
    mc = loaded.meshcore_context
    assert isinstance(mc, MeshCoreContextConfig)
    assert mc.enable_passive_context is True
    assert mc.observe_channels == []
    assert mc.ignore_contacts == []
    assert mc.respond_to_dms is True
    assert mc.addme_enabled is True
    assert mc.addme_channels == ["#aida"]
    assert mc.addme_advert_cooldown_seconds == 3600
    assert mc.addme_dm_delay_seconds == 20
    assert mc.addme_per_user_cooldown_seconds == 300
    assert "{name}" in mc.addme_dm_text
