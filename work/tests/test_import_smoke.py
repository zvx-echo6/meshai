"""Phase-0 import smoke test: every meshai/central/*_handler.py module
must be importable without error after the budget shim refactor.

This guards against a broken import chain (e.g. circular imports or a bad
re-export in the shim) that would silently break all handlers.

chore/ripout-2d: the last two central/ handlers (firms_handler.py,
wfigs_handler.py) relocated to meshai/env/fire_fusion.py + fire_render.py
(central/ has no *_handler.py left -- the fusion engine's only consumer is
the native env/firms.py adapter). The glob below now also covers those two
explicitly so this guard still exercises the relocated live import chain;
it keeps globbing central/ too in case a future handler lands back there.
"""

import glob
import importlib
import os


def _handler_modules():
    """Collect handler-ish module names: meshai.central.*_handler (glob) +
    the fire-fusion modules relocated out of central/ during the ripout."""
    central_pattern = os.path.join(
        os.path.dirname(__file__),
        "..", "meshai", "central", "*_handler.py",
    )
    central_paths = sorted(glob.glob(central_pattern))
    modules = [f"meshai.central.{os.path.basename(p)[:-3]}" for p in central_paths]
    modules += ["meshai.env.fire_fusion", "meshai.env.fire_render"]
    assert modules, "No handler-ish modules found — check the glob path"
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
