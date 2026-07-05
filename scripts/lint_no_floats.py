#!/usr/bin/env python3
"""AST-based lint enforcing "no floats on the money path" (SPEC S17.3).

This tool backs SPEC S6.1: the money/price/probability packages must never
contain a float literal, a ``float`` annotation (including forward-ref string
annotations), a true-division operator, or a ``float(...)`` cast. Only integer
arithmetic and hedgekit's sanctioned scaled-integer types are allowed there.

Emitted violation codes:
    * ``FLOAT-001`` -- float literal, e.g. ``x = 1.5``.
    * ``FLOAT-002`` -- float annotation on an argument, return, or ``AnnAssign``
      (bare ``float`` or a string ``"float"`` forward reference).
    * ``FLOAT-003`` -- true division ``/`` or ``/=`` (``//``, ``//=`` and
      ``divmod(...)`` are fine).
    * ``FLOAT-004`` -- ``float(...)`` cast, including ``builtins.float(...)``.

The guarded packages live in :data:`DENYLISTED_PACKAGES`. Later epics extend the
money path by appending their package prefixes (relative to the repo root) to
that tuple -- e.g. a future ``hedgekit/settlement`` -- and the full-scan mode
picks them up automatically.

Usage:
    * ``lint_no_floats.py PATH [PATH ...]`` -- lint exactly the given paths;
      a directory is expanded to every ``*.py`` beneath it.
    * ``lint_no_floats.py`` (no args) -- scan every ``*.py`` under each existing
      denylisted package.

Exit 1 if any violation is found, else 0 (with empty stdout).
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Package prefixes (relative to the repo root) guarded against floats. Later
#: epics append their own money-path packages here; full-scan mode globs
#: ``**/*.py`` under each one that exists. Issue #16 extends the path with the
#: exchange-facing ``hedgekit/connector`` (prices, quantities, balances) and
#: ``hedgekit/screener`` (eligibility decisions derived from those values).
#: Issue #22 extends the path with the probability/money-bearing
#: ``hedgekit/forecast`` package (probability_ppm, research_cost_micros, ...).
DENYLISTED_PACKAGES: tuple[str, ...] = (
    "hedgekit/numeric",
    "hedgekit/ledger",
    "hedgekit/riskkernel",
    "hedgekit/connector",
    "hedgekit/screener",
    "hedgekit/forecast",
)

FLOAT_LITERAL_CODE = "FLOAT-001"
FLOAT_ANNOTATION_CODE = "FLOAT-002"
TRUE_DIVISION_CODE = "FLOAT-003"
FLOAT_CAST_CODE = "FLOAT-004"

#: Repo root, derived from this script's own location (``<root>/scripts/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Violation(NamedTuple):
    """A single float-lint finding.

    Attributes:
        line: 1-based source line number.
        col: 0-based source column offset.
        code: One of the ``FLOAT-00x`` codes.
        message: Human-readable explanation.
    """

    line: int
    col: int
    code: str
    message: str


def _violation(node: ast.AST, code: str, message: str) -> Violation:
    """Build a :class:`Violation` located at ``node``."""
    line = getattr(node, "lineno", 1)
    col = getattr(node, "col_offset", 0)
    return Violation(line, col, code, message)


def _literal_violation(node: ast.AST) -> Violation | None:
    """Return a FLOAT-001 finding if ``node`` is a bare float literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return _violation(node, FLOAT_LITERAL_CODE, "float literal is banned")
    return None


def _division_violation(node: ast.AST) -> Violation | None:
    """Return a FLOAT-003 finding if ``node`` is true division (``/`` or ``/=``)."""
    if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
        return _violation(node, TRUE_DIVISION_CODE, "true division is banned")
    return None


def _names_float(func: ast.expr) -> bool:
    """Return whether ``func`` refers to the builtin ``float`` (bare or qualified)."""
    if isinstance(func, ast.Name):
        return func.id == "float"
    if isinstance(func, ast.Attribute):
        return func.attr == "float"
    return False


def _cast_violation(node: ast.AST) -> Violation | None:
    """Return a FLOAT-004 finding if ``node`` is a ``float(...)`` cast call."""
    if isinstance(node, ast.Call) and _names_float(node.func):
        return _violation(node, FLOAT_CAST_CODE, "float(...) cast is banned")
    return None


def _is_float_annotation(node: ast.AST) -> bool:
    """Return whether ``node`` is a bare ``float`` or the string ``"float"``."""
    if isinstance(node, ast.Name):
        return node.id == "float"
    if isinstance(node, ast.Constant):
        return bool(node.value == "float")
    return False


def _signature_annotations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[ast.expr]:
    """Yield every present annotation node in a function signature.

    Args:
        node: The (async) function definition to inspect.

    Yields:
        Each non-``None`` argument annotation, then the return annotation.
    """
    args = node.args
    arg_nodes = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        arg_nodes.append(args.vararg)
    if args.kwarg is not None:
        arg_nodes.append(args.kwarg)
    for arg in arg_nodes:
        if arg.annotation is not None:
            yield arg.annotation
    if node.returns is not None:
        yield node.returns


def _annotations_of(node: ast.AST) -> Iterator[ast.expr]:
    """Yield the annotation nodes owned by ``node`` (functions / ``AnnAssign``)."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from _signature_annotations(node)
    elif isinstance(node, ast.AnnAssign):
        yield node.annotation


def _annotation_violations(node: ast.AST) -> list[Violation]:
    """Return FLOAT-002 findings for any ``float`` within ``node``'s annotations."""
    found: list[Violation] = []
    for annotation in _annotations_of(node):
        found.extend(
            _violation(sub, FLOAT_ANNOTATION_CODE, "float annotation is banned")
            for sub in ast.walk(annotation)
            if _is_float_annotation(sub)
        )
    return found


def _node_violations(node: ast.AST) -> list[Violation]:
    """Return every violation attributable to a single AST ``node``."""
    candidates = (
        _literal_violation(node),
        _division_violation(node),
        _cast_violation(node),
    )
    found = [item for item in candidates if item is not None]
    found.extend(_annotation_violations(node))
    return found


def collect_violations(source: str, filename: str) -> list[Violation]:
    """Parse ``source`` and return its float-path violations, sorted.

    Args:
        source: Python source text.
        filename: Name used for the parse (surfaced only in parse errors).

    Returns:
        Violations sorted by ``(line, col, code, message)`` for deterministic
        output.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        violations.extend(_node_violations(node))
    return sorted(violations)


def _lint_file(path: Path) -> list[str]:
    """Lint one file into ``path:line:col CODE message`` output lines."""
    source = path.read_text(encoding="utf-8")
    return [
        f"{path}:{item.line}:{item.col} {item.code} {item.message}"
        for item in collect_violations(source, str(path))
    ]


def _expand_targets(paths: list[Path]) -> list[Path]:
    """Expand each path to concrete ``*.py`` files (directories recurse)."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path)
    return files


def _denylisted_files() -> list[Path]:
    """Return every ``*.py`` file under each existing denylisted package."""
    files: list[Path] = []
    for package in DENYLISTED_PACKAGES:
        package_dir = _REPO_ROOT / package
        if package_dir.is_dir():
            files.extend(sorted(package_dir.rglob("*.py")))
    return files


def main(argv: list[str] | None = None) -> int:
    """Run the float-lint CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        ``1`` if any violation was found, otherwise ``0``.
    """
    parser = argparse.ArgumentParser(description="Ban floats on the money path.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to lint; if omitted, scan denylisted packages.",
    )
    args = parser.parse_args(argv)

    if args.paths:
        targets = _expand_targets([Path(raw) for raw in args.paths])
    else:
        targets = _denylisted_files()

    lines: list[str] = []
    for path in targets:
        lines.extend(_lint_file(path))

    for line in lines:
        print(line)
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
