"""Phase-0 import smoke test: every meshai/central/*_handler.py module
must be importable without error after the budget shim refactor.

This guards against a broken import chain (e.g. circular imports or a bad
re-export in the shim) that would silently break all handlers.
"""

import glob
import importlib
import os


def _handler_modules():
    """Collect meshai.central.*_handler module names by globbing the source tree."""
    pattern = os.path.join(
        os.path.dirname(__file__),
        "..", "meshai", "central", "*_handler.py",
    )
    paths = sorted(glob.glob(pattern))
    assert paths, "No *_handler.py files found — check the glob path"
    modules = []
    for p in paths:
        name = os.path.basename(p)[:-3]  # strip .py
        modules.append(f"meshai.central.{name}")
    return modules


def test_all_central_handlers_importable():
    """Each *_handler module must import cleanly (no ImportError / circular deps)."""
    failed = []
    for mod_name in _handler_modules():
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            failed.append(f"{mod_name}: {exc}")
    assert not failed, (
        "The following handler modules raised ImportError:\n" + "\n".join(failed)
    )
