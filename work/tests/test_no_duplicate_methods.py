"""Guard test: no class in meshai/ may define the same method name twice.

Motivation (issue #127): ``MeshCoreTransport`` defined ``_resolve_contact``
TWICE — once at module-load-earlier line 171 (PR #56's refetch-on-miss DM
resolver) and again later at line 1227 (PR #92's telemetry resolver, added
independently and never noticed it collided with an existing name). Python
silently keeps only the LAST definition in a class body; the first is not an
error, a warning, or even visible to static type checkers or linters in the
configurations this project runs. The result: every caller of the first
implementation silently got the second implementation's (materially
different) behavior instead, for months, with no signal anywhere.

This test AST-parses every .py file under meshai/ and asserts that no
ClassDef body defines the same (non-overload, non-property-pair) function
name more than once. It is a static, source-level check — it doesn't need
the module to be importable (meshai.main can't be imported in this test env
at all; see test_central_boot_guard.py), so it runs over the raw source tree.

Legitimate duplicate-name patterns that must NOT be flagged:
  - ``@property`` / ``@x.setter`` / ``@x.deleter`` triplets (same name by
    design — that's how Python properties work).
  - ``@typing.overload`` stacks (multiple signatures, same name, followed by
    exactly one real implementation) — a standard typing idiom.
  - ``@overload`` from other modules aliased/imported differently is treated
    the same way: any decorator whose name (attribute or plain) ends in
    "overload" or is exactly "property"/"setter"/"deleter" (as a `.` attr)
    exempts that definition from the duplicate count.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MESHAI_ROOT = REPO_ROOT / "meshai"


def _iter_python_files(root: pathlib.Path):
    yield from root.rglob("*.py")


def _decorator_names(node: ast.AST) -> list[str]:
    """Return the flat list of decorator names on a function/method def.

    Handles ``@overload``, ``@typing.overload`` (Attribute), ``@property``,
    ``@x.setter``, ``@x.deleter`` (Attribute with .attr == setter/deleter).
    """
    names = []
    for dec in getattr(node, "decorator_list", []):
        target = dec
        # Decorators can be bare Name/Attribute, or a Call wrapping one
        # (e.g. @some_decorator(...)) — unwrap the call to get at the name.
        if isinstance(target, ast.Call):
            target = target.func
        if isinstance(target, ast.Attribute):
            names.append(target.attr)
        elif isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _is_exempt(node: ast.AST) -> bool:
    """True if this def's decorators mark it as a legitimate same-name reuse."""
    for name in _decorator_names(node):
        if name in ("setter", "deleter", "property"):
            return True
        if name.endswith("overload"):  # overload / typing.overload
            return True
    return False


def _find_duplicate_methods_in_class(cls: ast.ClassDef) -> dict[str, int]:
    """Return {method_name: count} for names defined >1 time in *cls*,
    excluding property accessor groups and @overload stacks."""
    counts: dict[str, int] = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_exempt(stmt):
                continue
            counts[stmt.name] = counts.get(stmt.name, 0) + 1
    return {name: n for name, n in counts.items() if n > 1}


def _collect_all_duplicates() -> dict[str, dict[str, int]]:
    """Walk every class in every .py file under meshai/; return
    {"path.py::ClassName": {method_name: count}} for classes with dupes."""
    findings: dict[str, dict[str, int]] = {}
    for path in _iter_python_files(MESHAI_ROOT):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                dupes = _find_duplicate_methods_in_class(node)
                if dupes:
                    rel = path.relative_to(REPO_ROOT)
                    findings[f"{rel}::{node.name}"] = dupes
    return findings


def test_no_class_defines_the_same_method_twice():
    """Fails if ANY class body in meshai/ defines a method name more than
    once (excluding @property/@setter/@deleter groups and @overload stacks).

    This is exactly the failure mode from issue #127: MeshCoreTransport had
    two ``_resolve_contact`` defs and Python silently kept only the second.
    """
    findings = _collect_all_duplicates()
    assert not findings, (
        "Duplicate method name(s) found within a single class body — Python "
        "silently keeps only the LAST definition, discarding the others with "
        "no error/warning (see issue #127). Fix by renaming or merging:\n"
        + "\n".join(f"  {cls}: {dupes}" for cls, dupes in sorted(findings.items()))
    )


def test_guard_sanity_meshai_root_is_populated():
    """Sanity check the guard actually scanned files (protects against a
    silently-empty rglob due to a path typo hiding a real bug forever)."""
    files = list(_iter_python_files(MESHAI_ROOT))
    assert len(files) > 10, f"expected many .py files under {MESHAI_ROOT}, found {len(files)}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
