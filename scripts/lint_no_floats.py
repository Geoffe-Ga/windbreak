#!/usr/bin/env python3
"""AST-based lint enforcing "no floats on the money path" (SPEC S17.3).

This tool backs SPEC S6.1: the money/price/probability packages must never
contain a float literal, a ``float`` annotation (including forward-ref string
annotations), a true-division operator, or a ``float(...)`` cast. Only integer
arithmetic and windbreak's sanctioned scaled-integer types are allowed there.

Emitted violation codes:
    * ``FLOAT-001`` -- float literal, e.g. ``x = 1.5``.
    * ``FLOAT-002`` -- float annotation on an argument, return, or ``AnnAssign``
      (bare ``float`` or a string ``"float"`` forward reference).
    * ``FLOAT-003`` -- true division ``/`` or ``/=`` (``//``, ``//=`` and
      ``divmod(...)`` are fine). ``pathlib.Path`` joins are exempt: a ``/`` (or
      ``/=``) is suppressed only when at least one operand is *provably*
      Path-typed -- a direct ``Path(...)``/``pathlib.Path(...)`` call, a
      bare/qualified ``Path`` parameter annotation, or a name resolved to a
      Path binding by lexical scope. Name resolution is shadowing-aware: each
      scope (module, function/lambda body, class body, and comprehension) maps
      its locally bound names to whether they are Path-typed, and a name
      resolves to the innermost scope that binds it. The common local binding
      forms are routed through the scope model, so an inner binding shadows an
      outer one: a function/lambda parameter, a plain or annotated assignment,
      an augmented assignment, a walrus (``:=``), a for-loop or comprehension
      target, a tuple/list-unpack element (including a ``*starred`` one), a
      ``with``/``async with ... as`` target, an ``except ... as`` name, or a
      ``match`` capture pattern (``case name``, ``*rest``, ``**rest``) that
      reuses a Path name all resolve as non-Path and fail closed. Anything
      ambiguous (a string/forward-ref or ``Path | None`` annotation) also fails
      closed and still trips FLOAT-003.

      Limitations (deliberate, low-likelihood conservative tradeoffs):

      * :func:`_names_path` trusts any bare ``Path`` or ``*.Path`` reference, so
        a non-``pathlib`` ``X.Path`` would be treated as a path type.
      * ``global``/``nonlocal`` rebindings reassign a name owned by an *outer*
        scope rather than binding locally, so they are not tracked; under a name
        collision they may fail *open*. This is accepted because money-path code
        rarely uses ``global``/``nonlocal`` rebinding to shadow a ``Path``
        constant.
      * ``import``/``from ... import ... as`` rebindings and a walrus (``:=``)
        that appears *inside* a comprehension (which per PEP 572 binds in the
        containing scope, not the comprehension's own) are likewise not tracked;
        under a name collision with an outer ``Path`` binding they too may fail
        *open*. Accepted for the same reason -- these rebinding shapes are
        vanishingly rare in money-path code and ``joinpath(...)`` remains the
        escape valve.
    * ``FLOAT-004`` -- ``float(...)`` cast, including ``builtins.float(...)``.

The guarded packages live in :data:`DENYLISTED_PACKAGES`. Later epics extend the
money path by appending their package prefixes (relative to the repo root) to
that tuple -- e.g. a future ``windbreak/settlement`` -- and the full-scan mode
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
#: exchange-facing ``windbreak/connector`` (prices, quantities, balances) and
#: ``windbreak/screener`` (eligibility decisions derived from those values).
#: Issue #22 extends the path with the probability/money-bearing
#: ``windbreak/forecast`` package (probability_ppm, research_cost_micros, ...).
#: Issue #31 extends the path with the shared ``windbreak/tokens`` package, whose
#: approval-token claims carry money-bearing fields (max_fee_micros, ...).
#: Issue #43 extends the path with the ``windbreak/selector`` package, whose
#: decisions carry price/size/notional/probability intents (SPEC S9.1).
#: Issue #48 extends the path with the ``windbreak/scheduler`` package, the
#: always-on PAPER composition root, whose tick computes equity/positions in
#: scaled-integer micros/centis (SPEC S6.1) -- a strengthening of the gate over
#: the new money-handling package, never a weakening.
#: Issue #187 extends the path with the ``windbreak/evaluation`` and
#: ``windbreak/reports`` packages -- the evaluation harness and its reporting
#: surface, whose money/probability aggregates stay on the scaled-integer money
#: path (SPEC S6.1) -- promoting their exact-integer discipline from convention
#: to lint enforcement.
DENYLISTED_PACKAGES: tuple[str, ...] = (
    "windbreak/numeric",
    "windbreak/ledger",
    "windbreak/riskkernel",
    "windbreak/connector",
    "windbreak/screener",
    "windbreak/forecast",
    "windbreak/tokens",
    "windbreak/selector",
    "windbreak/scheduler",
    "windbreak/evaluation",
    "windbreak/reports",
)

FLOAT_LITERAL_CODE = "FLOAT-001"
FLOAT_ANNOTATION_CODE = "FLOAT-002"
TRUE_DIVISION_CODE = "FLOAT-003"
FLOAT_CAST_CODE = "FLOAT-004"

#: Message shared by every FLOAT-003 finding (plain ``/`` and augmented ``/=``).
_TRUE_DIVISION_MESSAGE = "true division is banned"

#: The type name that marks a ``pathlib`` path, matched bare (``Path``) or as
#: the trailing attribute of a qualified reference (``pathlib.Path``).
_PATH_TYPE_NAME = "Path"

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


def _names_float(func: ast.expr) -> bool:
    """Return whether ``func`` refers to the builtin ``float`` (bare or qualified)."""
    if isinstance(func, ast.Name):
        return func.id == "float"
    if isinstance(func, ast.Attribute):
        return func.attr == "float"
    return False


def _names_path(func: ast.expr) -> bool:
    """Return whether ``func`` refers to ``pathlib.Path`` (bare or qualified).

    Args:
        func: The callee (or annotation) expression to classify.

    Returns:
        True for a bare ``Path`` :class:`ast.Name` or an :class:`ast.Attribute`
        ending in ``.Path`` (e.g. ``pathlib.Path``), else False.
    """
    if isinstance(func, ast.Name):
        return func.id == _PATH_TYPE_NAME
    if isinstance(func, ast.Attribute):
        return func.attr == _PATH_TYPE_NAME
    return False


def _is_path_annotation(annotation: ast.expr) -> bool:
    """Return whether ``annotation`` is an unambiguous bare/qualified ``Path``.

    Only a plain ``Path`` (:class:`ast.Name`) or ``pathlib.Path``
    (:class:`ast.Attribute`) counts. String/forward-ref annotations and unions
    such as ``Path | None`` are treated as ambiguous and return ``False`` so
    their ``/`` usages fail closed as FLOAT-003.

    Args:
        annotation: The annotation expression to classify.

    Returns:
        True only for a directly named ``Path`` type.
    """
    return _names_path(annotation)


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


def _arg_nodes(args: ast.arguments) -> list[ast.arg]:
    """Return every declared argument node, positional through ``**kwargs``.

    Args:
        args: The ``arguments`` block of a function signature.

    Returns:
        Positional-only, positional, and keyword-only args followed by the
        ``*args`` and ``**kwargs`` nodes when present.
    """
    nodes = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        nodes.append(args.vararg)
    if args.kwarg is not None:
        nodes.append(args.kwarg)
    return nodes


def _signature_annotations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[ast.expr]:
    """Yield every present annotation node in a function signature.

    Args:
        node: The (async) function definition to inspect.

    Yields:
        Each non-``None`` argument annotation, then the return annotation.
    """
    for arg in _arg_nodes(node.args):
        if arg.annotation is not None:
            yield arg.annotation
    if node.returns is not None:
        yield node.returns


def _param_path_flags(args: ast.arguments) -> dict[str, bool]:
    """Return every parameter name mapped to whether it is Path-typed.

    Args:
        args: The ``arguments`` block of a function being entered.

    Returns:
        A mapping of *every* declared parameter name to ``True`` when its
        annotation is an unambiguous bare/qualified ``Path`` and ``False``
        otherwise. Seeding a function scope with the full parameter set (not
        just the Path-typed ones) lets a non-Path parameter correctly shadow an
        outer Path binding of the same name, so genuine division fails closed.
    """
    return {
        arg.arg: arg.annotation is not None and _is_path_annotation(arg.annotation)
        for arg in _arg_nodes(args)
    }


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
    """Return the flat-walk violations for a single AST ``node``.

    FLOAT-003 (true division) is intentionally absent here: it needs the
    scope-aware :class:`_DivisionVisitor` to exempt ``pathlib.Path`` joins, so
    it is collected separately in :func:`collect_violations`.
    """
    candidates = (
        _literal_violation(node),
        _cast_violation(node),
    )
    found = [item for item in candidates if item is not None]
    found.extend(_annotation_violations(node))
    return found


class _DivisionVisitor(ast.NodeVisitor):
    """Scope-aware, shadowing-aware FLOAT-003 pass exempting ``Path`` joins.

    A flat :func:`ast.walk` cannot tell numeric ``a / b`` from a path join
    ``output_dir / "file"``. This visitor walks the tree keeping a stack of
    lexical scopes (the module plus each function body, class body, and
    comprehension). Every scope is a ``dict`` mapping each locally bound name to
    whether it is *provably* ``Path``-typed. A name resolves to the innermost
    scope that binds it, so an inner binding -- an ``int`` parameter, a
    rebinding assignment, a for-loop or comprehension target, or a tuple-unpack
    element -- shadows an outer Path binding of the same name. A ``/`` or ``/=``
    is reported as :data:`TRUE_DIVISION_CODE` only when no operand is Path-typed,
    so genuine division fails closed (see the module docstring for the rare
    untracked rebinding shapes -- ``global``/``nonlocal``, ``import ... as``,
    comprehension-scoped walrus -- that are accepted limitations).

    Attributes:
        violations: FLOAT-003 findings accumulated during the walk.
    """

    def __init__(self) -> None:
        """Initialise the visitor with an empty module scope."""
        self.violations: list[Violation] = []
        self._scopes: list[dict[str, bool]] = [{}]

    def _name_is_path(self, name: str) -> bool:
        """Return whether ``name`` resolves to a Path binding, innermost first.

        Args:
            name: The identifier to resolve.

        Returns:
            The Path-typing flag of the innermost scope that binds ``name``; if
            no scope binds it, ``False`` (fail closed for unknown names).
        """
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return False

    def _bind_target(self, target: ast.expr, is_path: bool) -> None:
        """Register an assignment target in the current scope.

        Args:
            target: The binding site. A plain ``Name`` is recorded with the
                given flag. A ``Tuple``/``List`` is destructured and each element
                bound as non-Path, since an unpacked element is never provably a
                ``Path`` (fail closed). A ``Starred`` target (``*rest``) is
                unwrapped and its inner name bound non-Path likewise.
                Attribute/Subscript targets are not simple local names and are
                ignored.
            is_path: Whether ``target`` is provably Path-typed.
        """
        if isinstance(target, ast.Name):
            self._scopes[-1][target.id] = is_path
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value, False)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self._bind_target(element, False)

    def _nested_join_is_path(self, expr: ast.expr) -> bool:
        """Return whether ``expr`` is a ``/`` join with a Path-typed operand.

        This keeps a chained join like ``root / "a" / "b"`` fully suppressed:
        the outer ``/`` sees its left operand is itself a Path-typed join.
        """
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            return self._operand_is_path(expr.left) or self._operand_is_path(expr.right)
        return False

    def _operand_is_path(self, expr: ast.expr) -> bool:
        """Return whether ``expr`` provably evaluates to a ``Path``.

        Args:
            expr: A ``/`` operand to classify.

        Returns:
            True for a direct ``Path(...)`` call, a Path-typed name, a walrus
            (``:=``) whose value is provably Path, or a nested ``/`` join with a
            Path-typed operand.
        """
        if isinstance(expr, ast.Call):
            return _names_path(expr.func)
        if isinstance(expr, ast.Name):
            return self._name_is_path(expr.id)
        if isinstance(expr, ast.NamedExpr):
            return self._operand_is_path(expr.value)
        return self._nested_join_is_path(expr)

    def _record_division(self, node: ast.BinOp | ast.AugAssign) -> None:
        """Append a FLOAT-003 finding located at ``node``."""
        self.violations.append(
            _violation(node, TRUE_DIVISION_CODE, _TRUE_DIVISION_MESSAGE)
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """Flag a ``/`` BinOp unless an operand is Path-typed (a join)."""
        if isinstance(node.op, ast.Div) and not (
            self._operand_is_path(node.left) or self._operand_is_path(node.right)
        ):
            self._record_division(node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Flag ``target /= x`` unless the target name is Path-typed."""
        target = node.target
        target_is_path = isinstance(target, ast.Name) and self._name_is_path(target.id)
        if isinstance(node.op, ast.Div) and not target_is_path:
            self._record_division(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit the RHS, then bind each target with the RHS's Path-typing.

        A direct ``Path(...)``/``pathlib.Path(...)`` call marks every target
        Path-typed (so ``a = b = Path(...)`` binds both ``True``); any other RHS
        fails closed and binds them non-Path, correctly shadowing an outer Path
        name (so ``p = 5`` and ``p, q = 5, 6`` both drop ``p``).
        """
        is_path = isinstance(node.value, ast.Call) and _names_path(node.value.func)
        self.generic_visit(node)
        for target in node.targets:
            self._bind_target(target, is_path)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Visit the value, then bind a ``name: T`` target by its annotation.

        A ``name: Path`` target is recorded Path-typed; any other annotation
        (e.g. ``name: int``) records non-Path, shadowing an outer Path ``name``.
        """
        self.generic_visit(node)
        if isinstance(node.target, ast.Name):
            self._scopes[-1][node.target.id] = _is_path_annotation(node.annotation)

    def visit_For(self, node: ast.For) -> None:
        """Bind the loop target non-Path (fail closed), then walk the loop."""
        self._bind_target(node.target, False)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """Bind the async loop target non-Path, then walk the loop."""
        self._bind_target(node.target, False)
        self.generic_visit(node)

    def _enter_param_scope(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        """Push a scope seeded with all params' Path flags, recurse, then pop.

        Shared by ``def``, ``async def``, and ``lambda`` -- each exposes an
        ``.args`` block whose parameter names must shadow any outer binding of
        the same name (a ``lambda`` param is always non-Path, since lambda
        parameters cannot be annotated).

        Args:
            node: The function, async function, or lambda to enter.
        """
        self._scopes.append(_param_path_flags(node.args))
        self.generic_visit(node)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Walk a function body under a fresh, parameter-seeded scope."""
        self._enter_param_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Walk an async function body under a fresh, parameter-seeded scope."""
        self._enter_param_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Walk a lambda body under a fresh, parameter-seeded scope.

        Lambda parameters cannot be annotated, so every one seeds as non-Path
        and shadows an outer Path name of the same identifier (fail closed).
        """
        self._enter_param_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Walk a class body under its own scope so attributes never leak.

        A class-body ``d = Path(...)`` binds ``d`` in the class scope only; it
        must not leak Path-typing into the enclosing/module scope.
        """
        self._scopes.append({})
        self.generic_visit(node)
        self._scopes.pop()

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        """Walk a comprehension under its own scope with non-Path targets.

        Python 3 gives each comprehension its own lexical scope, so its
        generator targets must neither leak into the enclosing scope nor be
        treated as Path-typed. Each target is bound non-Path (fail closed)
        before the body is visited, shadowing any outer Path name.

        Args:
            node: The comprehension whose generators and body to visit.
        """
        self._scopes.append({})
        for generator in node.generators:
            self._bind_target(generator.target, False)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """Walk a list comprehension under its own non-leaking scope."""
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """Walk a set comprehension under its own non-leaking scope."""
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """Walk a dict comprehension under its own non-leaking scope."""
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """Walk a generator expression under its own non-leaking scope."""
        self._visit_comprehension(node)

    def _bind_with_items(self, items: list[ast.withitem]) -> None:
        """Bind every ``with ... as`` target non-Path in the current scope.

        A ``with`` block does not open a new lexical scope, so its ``as``-target
        rebinds a name in the enclosing scope. Such a target is never provably a
        ``Path`` (the context manager's yielded value is opaque here), so it is
        bound non-Path and shadows any outer Path name (fail closed).

        Args:
            items: The ``withitem`` list of a ``with``/``async with`` block.
        """
        for item in items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars, False)

    def visit_With(self, node: ast.With) -> None:
        """Rebind each ``with ... as`` target non-Path, then walk the block."""
        self._bind_with_items(node.items)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """Rebind each ``async with ... as`` target non-Path, then walk it."""
        self._bind_with_items(node.items)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """Visit children, then bind the walrus (``:=``) target by its value.

        A ``(name := Path(...))`` records ``name`` Path-typed so later joins on
        it stay suppressed; any other value (e.g. ``(name := 5)``) binds it
        non-Path, shadowing an outer Path ``name`` so genuine division on it
        fails closed.
        """
        self.generic_visit(node)
        is_path = isinstance(node.value, ast.Call) and _names_path(node.value.func)
        self._bind_target(node.target, is_path)

    def _bind_capture(self, name: str | None) -> None:
        """Bind a capture name non-Path in the current scope when present.

        Shared by ``except ... as`` handlers and ``match`` capture patterns,
        whose captured identifiers arrive as ``str | None`` (not ``Name``
        nodes). A capture is never provably ``Path``, so it is recorded non-Path
        and shadows any outer Path binding of the same name (fail closed).

        Args:
            name: The captured identifier, or ``None`` when the form binds none.
        """
        if name is not None:
            self._scopes[-1][name] = False

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Rebind an ``except ... as name`` capture non-Path, then walk it."""
        self._bind_capture(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        """Rebind a ``case name``/``as name`` capture non-Path, then walk it."""
        self._bind_capture(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        """Rebind a ``case [*rest]`` star capture non-Path, then walk it."""
        self._bind_capture(node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        """Rebind a ``case {..., **rest}`` capture non-Path, then walk it."""
        self._bind_capture(node.rest)
        self.generic_visit(node)


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
    division_visitor = _DivisionVisitor()
    division_visitor.visit(tree)
    violations.extend(division_visitor.violations)
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
