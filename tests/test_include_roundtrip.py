"""v0.6-tail-4: !include YAML round-trip preservation tests.

Verifies:
  - The runtime loader (_load_yaml_with_includes) still substitutes
    !include contents (read-path behavior unchanged).
  - The preserve loader (_load_yaml_preserve) returns Include() sentinels
    instead of substituting.
  - The preserve dumper re-emits Include() as `!include path`.
  - A read-then-write-then-re-read round-trip through the preserve helpers
    yields identical effective config.
  - save_section() on a section whose target file uses !include for
    sibling sections succeeds (the PUT /api/config/bot 500 from v0.6-tail-3
    is closed).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Read path: runtime !include substitution still works (unchanged behavior).
# ---------------------------------------------------------------------------


def _write(p: Path, body: str) -> Path:
    p.write_text(body)
    return p


def test_runtime_load_substitutes_include(tmp_path):
    from meshai.config_loader import _load_yaml_with_includes

    _write(tmp_path / "child.yaml", "value: 42\nname: child\n")
    root = _write(
        tmp_path / "config.yaml",
        "top: 1\nnested: !include child.yaml\n",
    )

    data = _load_yaml_with_includes(root)
    # The !include directive must be substituted in at runtime so the
    # Config dataclass sees real values, not placeholders.
    assert data["top"] == 1
    assert data["nested"] == {"value": 42, "name": "child"}


# ---------------------------------------------------------------------------
# Preserve path: !include round-trips as a sentinel.
# ---------------------------------------------------------------------------


def test_preserve_load_returns_include_sentinel(tmp_path):
    from meshai.config_loader import Include, _load_yaml_preserve

    _write(tmp_path / "child.yaml", "value: 42\n")
    root = _write(
        tmp_path / "config.yaml",
        "top: 1\nnested: !include child.yaml\nlater: hi\n",
    )

    data = _load_yaml_preserve(root)
    assert data["top"] == 1
    # The !include node must NOT be substituted -- the placeholder is
    # what makes round-trip possible.
    assert isinstance(data["nested"], Include)
    assert data["nested"].path == "child.yaml"
    assert data["later"] == "hi"


def test_preserve_dump_reemits_include_directive(tmp_path):
    from meshai.config_loader import (
        Include,
        _dump_yaml_preserve,
        _load_yaml_preserve,
    )

    data = {
        "bot": {"name": "AIDA"},
        "domain": Include("env_feeds.yaml"),
    }
    out = tmp_path / "out.yaml"
    _dump_yaml_preserve(data, out)
    raw = out.read_text()
    # The directive must come back as text, not flattened to a dict or
    # serialized as some Python repr. PyYAML may auto-quote the scalar
    # (`!include 'env_feeds.yaml'`); both are equivalent at parse time.
    assert ("!include env_feeds.yaml" in raw
            or "!include 'env_feeds.yaml'" in raw)

    # And the round-trip back via _load_yaml_preserve must give us the
    # same Include placeholder.
    reread = _load_yaml_preserve(out)
    assert reread["bot"] == {"name": "AIDA"}
    assert isinstance(reread["domain"], Include)
    assert reread["domain"].path == "env_feeds.yaml"


def test_roundtrip_identical(tmp_path):
    """read -> write -> read produces the same data structure."""
    from meshai.config_loader import _dump_yaml_preserve, _load_yaml_preserve

    _write(tmp_path / "leaf.yaml", "x: 1\ny: 2\n")
    root = _write(
        tmp_path / "config.yaml",
        (
            "timezone: America/Boise\n"
            "bot:\n"
            "  name: AIDA\n"
            "  respond_to_dms: true\n"
            "domain: !include leaf.yaml\n"
        ),
    )

    first = _load_yaml_preserve(root)
    out = tmp_path / "rewrite.yaml"
    _dump_yaml_preserve(first, out)
    second = _load_yaml_preserve(out)
    assert first == second


# ---------------------------------------------------------------------------
# Integration: save_section() on config.yaml that uses !include succeeds.
# ---------------------------------------------------------------------------


@pytest.fixture
def prod_shaped_config(tmp_path):
    """Build a /data/config-shaped tree with config.yaml using !include."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Sibling files that config.yaml !include's.
    (config_dir / "meshtastic.yaml").write_text(
        "connection:\n  tcp_host: 1.2.3.4\n  port: 4403\n"
        "commands:\n  enabled: true\n  prefix: '!'\n"
    )
    (config_dir / "env_feeds.yaml").write_text(
        "usgs:\n  enabled: true\n  feed_source: central\n"
    )
    (config_dir / "llm.yaml").write_text(
        "backend: google\n  \n# placeholder\n"
    )
    (config_dir / "config.yaml").write_text(
        "timezone: America/Boise\n"
        "bot:\n"
        "  respond_to_dms: true\n"
        "  filter_bbs_protocols: true\n"
        "  name: AIDA\n"
        "response:\n"
        "  delay_min: 1.5\n"
        "  delay_max: 2.5\n"
        "meshtastic: !include meshtastic.yaml\n"
        "environmental: !include env_feeds.yaml\n"
        "llm: !include llm.yaml\n"
    )
    return config_dir


def test_save_section_inline_does_not_crash_with_includes(prod_shaped_config):
    """PUT /api/config/bot -> save_section('bot', ...) used to die with
    'could not determine a constructor for the tag !include'. After
    v0.6-tail-4 it must succeed and leave the include directives intact."""
    from meshai.config_loader import save_section

    result = save_section(
        "bot",
        {"respond_to_dms": False, "filter_bbs_protocols": True, "name": "AIDA"},
        config_dir=prod_shaped_config,
    )
    assert result["saved"] is True
    assert result["rejected_secrets"] == []

    # The on-disk config.yaml must still contain the !include directives
    # for the sibling sections (meshtastic, environmental, llm). PyYAML
    # may emit them quoted; both forms are valid.
    written = (prod_shaped_config / "config.yaml").read_text()
    for name in ("meshtastic.yaml", "env_feeds.yaml", "llm.yaml"):
        assert (f"!include {name}" in written
                or f"!include '{name}'" in written), (
            f"missing !include for {name}; got: {written!r}"
        )
    # And the change to bot.respond_to_dms must have actually persisted.
    assert "respond_to_dms: false" in written


def test_save_section_inline_then_runtime_reload_sees_change(prod_shaped_config):
    """Round-trip integrity: after save_section() the runtime loader
    must still resolve the !include directives AND see the saved
    change to the inline section."""
    from meshai.config_loader import _load_yaml_with_includes, save_section

    save_section(
        "response",
        {"delay_min": 0.5, "delay_max": 1.0},
        config_dir=prod_shaped_config,
    )

    runtime = _load_yaml_with_includes(prod_shaped_config / "config.yaml")
    # The save_section change took effect.
    assert runtime["response"]["delay_min"] == 0.5
    # The includes still resolve at runtime.
    assert runtime["meshtastic"]["connection"]["tcp_host"] == "1.2.3.4"
    assert runtime["environmental"]["usgs"]["feed_source"] == "central"


def test_save_section_dedicated_file_unchanged(prod_shaped_config):
    """save_section() on a section whose target_file is a !include'd
    file (e.g. environmental -> env_feeds.yaml) must not be affected
    by this patch -- those files don't use !include themselves."""
    from meshai.config_loader import save_section

    result = save_section(
        "environmental",
        {"usgs": {"enabled": False, "feed_source": "central"}},
        config_dir=prod_shaped_config,
    )
    assert result["saved"] is True

    written = (prod_shaped_config / "env_feeds.yaml").read_text()
    assert "enabled: false" in written
    # config.yaml is untouched -- !include directives still there.
    main = (prod_shaped_config / "config.yaml").read_text()
    assert "!include env_feeds.yaml" in main


# ---------------------------------------------------------------------------
# Defensive: cycle detection still trips for the runtime loader.
# ---------------------------------------------------------------------------


def test_runtime_loader_detects_cycles(tmp_path):
    from meshai.config_loader import _load_yaml_with_includes

    _write(tmp_path / "a.yaml", "child: !include b.yaml\n")
    _write(tmp_path / "b.yaml", "child: !include a.yaml\n")

    with pytest.raises(yaml.YAMLError, match="Circular include"):
        _load_yaml_with_includes(tmp_path / "a.yaml")
