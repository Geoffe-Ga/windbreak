"""Failing-first tests for the order-submission-client import boundary
(issue #37, RED).

SPEC S5.2/S5.3 reserve the exchange order-submission client
(`windbreak.connector.paper`, and by extension its `PaperExchange`/
`PaperOrderIntent` re-exports off `windbreak.connector`) to the Order Gateway
alone: no other package may hold a live trading credential. This mirrors
`tests/riskkernel/test_process_isolation.py`'s pure-`ast` signing-key-isolation
scanner exactly, retargeted at the connector-paper boundary, plus a matching
`plans/architecture/.importlinter` forbidden-modules contract.

`plans/architecture/.importlinter` does not yet declare the
`[importlinter:contract:order-submission-client-isolation]` section this file
asserts on, so `test_importlinter_declares_order_submission_client_isolation_contract`
fails today -- the expected Gate 1 RED state for issue #37. Every other test
in this module (the AST scanner itself, its seeded positive/negative cases,
and the real-tree sweep) is self-contained pure-stdlib `ast`/`configparser`
and already passes; only the contract-declaration test is RED, waiting on the
implementation step to add the section.
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest

#: Repo root, derived from this test file's own location
#: (`<root>/tests/architecture/test_import_boundaries.py`).
_REPO_ROOT = Path(__file__).resolve().parents[2]

_WINDBREAK_PACKAGE_DIR = _REPO_ROOT / "windbreak"
_ORDER_GATEWAY_PACKAGE_DIR = _WINDBREAK_PACKAGE_DIR / "order_gateway"
_CONNECTOR_PACKAGE_DIR = _WINDBREAK_PACKAGE_DIR / "connector"
#: The PAPER-mode composition root (issue #48): a legitimate importer of
#: `windbreak.connector.paper`, because `build_paper_deps` constructs a
#: `PaperExchange` in its PAPER factory. This is an intentional allowlist
#: extension, not a gate weakening: the boundary's intent -- keeping the paper
#: fake out of the RESEARCH/LIVE trading path -- is preserved because the
#: RESEARCH loop never imports `windbreak.scheduler` (it wires the PAPER tick via
#: a local import only when PAPER is actually activated), and the scheduler
#: imports paper solely inside that PAPER factory.
_SCHEDULER_PACKAGE_DIR = _WINDBREAK_PACKAGE_DIR / "scheduler"
_IMPORTLINTER_PATH = _REPO_ROOT / "plans" / "architecture" / ".importlinter"

#: The single module this boundary reserves to `order_gateway`/`connector`.
_FORBIDDEN_PAPER_MODULE = "windbreak.connector.paper"

#: The parent package a `from windbreak.connector import X` re-export loophole
#: goes through -- either the submodule itself as a bare symbol (`paper`) or
#: one of its two re-exported names (`PaperExchange`, `PaperOrderIntent`).
_PAPER_PARENT_MODULE = "windbreak.connector"
_PAPER_REEXPORTED_NAMES = frozenset({"PaperExchange", "PaperOrderIntent"})

#: The `.importlinter` contract section declaring this boundary.
_PAPER_CONTRACT_SECTION = "importlinter:contract:order-submission-client-isolation"


# --- AST boundary: zero forbidden connector.paper imports outside the two ------
# --- packages that legitimately need it (order_gateway, connector itself) -----
#
# Self-contained (stdlib `ast` only), mirroring
# tests/riskkernel/test_process_isolation.py's own import-boundary checker:
# `ast.walk` visits every node regardless of nesting, so an import inside
# `if TYPE_CHECKING:` is inspected exactly like a top-level one.


def _find_paper_imports(source: str) -> tuple[str, ...]:
    """Return every spelling of a forbidden `connector.paper` import found.

    Flags: a plain `import windbreak.connector.paper`; an absolute
    `from windbreak.connector.paper import X` (module itself); the
    submodule-as-symbol loophole `from windbreak.connector import paper`; the
    re-export loophole `from windbreak.connector import PaperExchange` (or
    `PaperOrderIntent`); and every *relative* import (`node.level > 0`),
    conservatively -- a `..`-hop could reach the forbidden module, and the
    codebase's own convention is absolute imports throughout.

    Args:
        source: Python source text to parse.

    Returns:
        The offending dotted names (or `.`-prefixed relative-import
        spellings) found, in AST-traversal order.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _FORBIDDEN_PAPER_MODULE or alias.name.startswith(
                    _FORBIDDEN_PAPER_MODULE + "."
                ):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            found.extend(_find_paper_imports_from(node))
    return tuple(found)


def _find_paper_imports_from(node: ast.ImportFrom) -> tuple[str, ...]:
    """Return the forbidden-import spellings carried by one `ImportFrom` node.

    Args:
        node: The `from ... import ...` node to inspect.

    Returns:
        The offending spellings found on this single node.
    """
    if node.level > 0:
        return ("." * node.level + (node.module or ""),)
    if node.module is None:
        return ()
    if node.module == _FORBIDDEN_PAPER_MODULE:
        return tuple(f"{node.module}.{alias.name}" for alias in node.names)
    if node.module == _PAPER_PARENT_MODULE:
        return tuple(
            f"{node.module}.{alias.name}"
            for alias in node.names
            if alias.name == "paper" or alias.name in _PAPER_REEXPORTED_NAMES
        )
    return ()


def test_zero_forbidden_paper_imports_outside_order_gateway_and_connector() -> None:
    """Every shipped `windbreak/**/*.py` module outside `order_gateway/`,
    `connector/`, and `scheduler/` themselves imports across the
    order-submission-client boundary cleanly -- zero hits. All three packages are
    legitimately exempt: `connector/` is where `PaperExchange` itself is defined
    (and `connector/__init__.py` already re-exports it), `order_gateway/` is the
    sole intended trading consumer (issue #37's `PaperSubmitter`), and
    `scheduler/` is the PAPER-mode composition root (issue #48) that constructs a
    `PaperExchange` only inside its `build_paper_deps` PAPER factory -- the
    boundary's intent (keeping the paper fake off the RESEARCH/LIVE path) holds
    because the RESEARCH loop never imports `windbreak.scheduler`.
    """
    violations: list[str] = []
    for path in sorted(_WINDBREAK_PACKAGE_DIR.rglob("*.py")):
        if (
            _ORDER_GATEWAY_PACKAGE_DIR in path.parents
            or _CONNECTOR_PACKAGE_DIR in path.parents
            or _SCHEDULER_PACKAGE_DIR in path.parents
        ):
            continue
        source = path.read_text(encoding="utf-8")
        violations.extend(
            f"{path.relative_to(_REPO_ROOT)}:{name}"
            for name in _find_paper_imports(source)
        )

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import windbreak.connector.paper\n",
        "from windbreak.connector.paper import PaperExchange\n",
        "from windbreak.connector import paper\n",
        "from windbreak.connector import PaperExchange\n",
        "from windbreak.connector import PaperOrderIntent\n",
        "from . import paper\n",
        "from ..connector import paper\n",
    ],
    ids=[
        "import-module",
        "from-module-import-symbol",
        "submodule-as-symbol",
        "reexport-PaperExchange",
        "reexport-PaperOrderIntent",
        "relative-dot-import",
        "relative-dotdot-import",
    ],
)
def test_ast_checker_flags_each_seeded_paper_import(source: str) -> None:
    """Each seeded synthetic source string is flagged as a forbidden import."""
    violations = _find_paper_imports(source)

    assert violations, f"expected a violation for: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "from windbreak.connector import FakeExchange\n",
        "from windbreak.connector.models import Fill\n",
    ],
    ids=["unrelated-connector-reexport", "unrelated-connector-submodule"],
)
def test_ast_checker_does_not_flag_an_unrelated_connector_import(source: str) -> None:
    """Importing an unrelated `connector` name or submodule is not itself a
    order-submission-client-boundary violation.
    """
    assert _find_paper_imports(source) == ()


# --- .importlinter: the config-file counterpart of the AST check ---------------


def test_importlinter_declares_order_submission_client_isolation_contract() -> None:
    """`.importlinter` names a contract forbidding `connector.paper` imports,
    so the CI-enforced `import-linter` gate (wired via scripts/check-all.sh,
    issue #91) enforces the same boundary the AST scanner above already
    enforces at the pytest layer.
    """
    parser = configparser.ConfigParser()
    read_files = parser.read(_IMPORTLINTER_PATH, encoding="utf-8")

    assert read_files, f"could not read {_IMPORTLINTER_PATH}"
    assert parser.has_section(_PAPER_CONTRACT_SECTION)
    forbidden_modules = parser.get(
        _PAPER_CONTRACT_SECTION, "forbidden_modules", fallback=""
    ).split()
    assert _FORBIDDEN_PAPER_MODULE in forbidden_modules
