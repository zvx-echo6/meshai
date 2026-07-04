"""Phase-0b: verify capture_fixtures.py is importable without a NATS connection.

This test guards against syntax errors, bad top-level imports, or any
network access at import time.  It does NOT execute main() or connect to NATS.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types


_SCRIPT_PATH = (
    pathlib.Path(__file__).parent.parent / "scripts" / "capture_fixtures.py"
)


class TestCaptureScriptImportable:
    def test_script_file_exists(self):
        assert _SCRIPT_PATH.is_file(), (
            f"Expected capture_fixtures.py at {_SCRIPT_PATH} but file not found"
        )

    def test_script_importable_without_network(self):
        """Load the script as a module; must succeed without NATS connection."""
        spec = importlib.util.spec_from_file_location(
            "capture_fixtures_script", _SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        # Execute the module body — must not connect to anything.
        spec.loader.exec_module(module)  # type: ignore[union-attr]

    def test_main_function_is_callable(self):
        """The script must expose a main() callable."""
        spec = importlib.util.spec_from_file_location(
            "capture_fixtures_script_main", _SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        assert callable(getattr(module, "main", None)), (
            "capture_fixtures.py must expose a main() callable"
        )

    def test_output_dir_helper_is_callable(self):
        """_output_dir is a helper for resolving fixture paths; must be importable."""
        spec = importlib.util.spec_from_file_location(
            "capture_fixtures_script_dir", _SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        assert callable(getattr(module, "_output_dir", None))

    def test_help_flag_is_parseable(self):
        """main(['--help']) must raise SystemExit(0), not a real error."""
        spec = importlib.util.spec_from_file_location(
            "capture_fixtures_script_help", _SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            module.main(["--help"])
        assert exc_info.value.code == 0
