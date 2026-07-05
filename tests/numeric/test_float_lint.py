"""Failing-first tests for scripts/lint_no_floats.py (issue #12, SPEC S17.3).

The AST float-lint is the enforcement mechanism behind SPEC S6.1's "no
floats in the money path" rule: `hedgekit/numeric`, `hedgekit/ledger`, and
`hedgekit/riskkernel` must never contain a float literal, a `float`
annotation (including forward-ref string annotations), a true-division
operator, or a `float(...)` cast. This module loads the script directly
by path (`importlib.util.spec_from_file_location`) because it lives
outside the `hedgekit` package -- it is a repo-maintenance tool, not
shipped code. The script does not exist yet: that missing-file state
*is* the RED milestone for issue #12.

Pinned violation codes (the implementer must emit exactly these):
    FLOAT-001  float literal            (e.g. `x = 1.5`)
    FLOAT-002  float annotation         (arg / return / AnnAssign / string)
    FLOAT-003  true division            (`/` or `/=`; `//` and divmod() are fine)
    FLOAT-004  float(...) cast          (including `builtins.float(...)`)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_SCRIPT_PATH = REPO_ROOT / "scripts" / "lint_no_floats.py"

#: Mirrors the script's own DENYLISTED_PACKAGES constant, for a direct
#: cross-check once the module can be loaded.
EXPECTED_DENYLISTED_PACKAGES = (
    "hedgekit/numeric",
    "hedgekit/ledger",
    "hedgekit/riskkernel",
)


def _load_lint_module() -> types.ModuleType:
    """Load scripts/lint_no_floats.py by file path and execute it.

    Raises FileNotFoundError today, since the script has not been
    written yet -- that is the expected RED failure for issue #12.
    """
    spec = importlib.util.spec_from_file_location("lint_no_floats", LINT_SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lint_module() -> types.ModuleType:
    """Provide the freshly loaded lint script module for each test."""
    return _load_lint_module()


def test_denylisted_packages_constant(lint_module: types.ModuleType) -> None:
    """The script's denylist must cover exactly the three money-path packages."""
    assert lint_module.DENYLISTED_PACKAGES == EXPECTED_DENYLISTED_PACKAGES


# --- Detection: float literal -------------------------------------------------


def test_detects_float_literal(lint_module: types.ModuleType) -> None:
    violations = lint_module.collect_violations("value = 1.5\n", "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-001"


# --- Detection: float annotations ---------------------------------------------


def test_detects_float_argument_annotation(lint_module: types.ModuleType) -> None:
    source = "def f(x: float) -> None:\n    return None\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-002"


def test_detects_float_return_annotation(lint_module: types.ModuleType) -> None:
    source = "def g() -> float:\n    return 0\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-002"


def test_detects_float_ann_assign(lint_module: types.ModuleType) -> None:
    source = "y: float = 0\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-002"


def test_detects_string_literal_float_annotation(lint_module: types.ModuleType) -> None:
    """Forward-ref style `x: "float"` must be caught, not just bare `float`."""
    source = "def h(x: 'float') -> None:\n    return None\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-002"


# --- Detection: true division --------------------------------------------------


def test_detects_true_division_but_not_floor_division(
    lint_module: types.ModuleType,
) -> None:
    source = "a = 1 // 2\nb = 4 / 2\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].code == "FLOAT-003"


def test_does_not_flag_divmod(lint_module: types.ModuleType) -> None:
    source = "q, r = divmod(7, 2)\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert violations == []


def test_detects_augmented_true_division(lint_module: types.ModuleType) -> None:
    source = "a = 4\na /= 2\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].code == "FLOAT-003"


def test_does_not_flag_augmented_floor_division(lint_module: types.ModuleType) -> None:
    source = "a = 4\na //= 2\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert violations == []


# --- Detection: float(...) casts -----------------------------------------------


def test_detects_float_cast(lint_module: types.ModuleType) -> None:
    source = "n = float('1.5')\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 1
    assert violations[0].code == "FLOAT-004"


def test_detects_builtins_qualified_float_cast(lint_module: types.ModuleType) -> None:
    source = "import builtins\nn = builtins.float(1)\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].code == "FLOAT-004"


# --- Clean source ---------------------------------------------------------------


def test_clean_source_yields_no_violations(lint_module: types.ModuleType) -> None:
    source = (
        "import math\n"
        "\n"
        "\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def ratio(a: int, b: int) -> int:\n"
        "    return a // b\n"
    )

    violations = lint_module.collect_violations(source, "example.py")

    assert violations == []


# --- CLI: dual invocation modes ---------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the lint script as a subprocess, from the repo root.

    Asserts the script file exists first, so a not-yet-implemented
    script fails with an unambiguous "not yet implemented" message
    rather than an opaque non-zero exit code from the interpreter.
    """
    assert LINT_SCRIPT_PATH.is_file(), f"lint script missing: {LINT_SCRIPT_PATH}"
    return subprocess.run(
        [sys.executable, str(LINT_SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exits_1_and_prints_violation_for_bad_file(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("x = 1.5\n")

    result = _run_cli(str(bad_file))

    assert result.returncode == 1
    assert "FLOAT-001" in result.stdout
    assert f"{bad_file}:1" in result.stdout


def test_cli_exits_0_for_clean_file(tmp_path: Path) -> None:
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")

    result = _run_cli(str(clean_file))

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_full_scan_mode_real_repo_denylisted_packages_are_clean() -> None:
    """No path args -> scan DENYLISTED_PACKAGES; today's stubs are float-free.

    This test remains meaningful after the RED->GREEN transition too: once
    types.py/rounding.py exist under hedgekit/numeric/, this is the same
    check that guards them (and hedgekit/ledger, hedgekit/riskkernel) in CI.
    """
    result = _run_cli()

    assert result.returncode == 0, result.stdout
