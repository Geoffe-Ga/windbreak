"""Failing-first tests for scripts/lint_no_floats.py (issue #12, SPEC S17.3).

The AST float-lint is the enforcement mechanism behind SPEC S6.1's "no
floats in the money path" rule: `hedgekit/numeric`, `hedgekit/ledger`,
`hedgekit/riskkernel`, `hedgekit/connector`, `hedgekit/screener`,
`hedgekit/forecast`, `hedgekit/tokens`, and `hedgekit/selector` must never
contain a float literal,
a `float`
annotation (including forward-ref
string annotations), a true-division operator, or a `float(...)` cast. This
module loads the script directly by path
(`importlib.util.spec_from_file_location`) because it lives outside the
`hedgekit` package -- it is a repo-maintenance tool, not shipped code. The
script does not exist yet: that missing-file state *is* the RED milestone
for issue #12.

Issue #16 extends the money-path denylist with `hedgekit/connector` (the
exchange-facing numeric types: prices, quantities, balances) and
`hedgekit/screener` (jurisdiction/eligibility decisions derived from those
same values); issue #22 then appends `hedgekit/forecast` (the pipeline's
probability/money-bearing record fields); issue #31 appends `hedgekit/tokens`
(the shared approval-token package's money-bearing claims fields); issue #43
appends `hedgekit/selector` (the pure Trade Selector's fixed-point
price/edge/sizing paths); issue #48 appends `hedgekit/scheduler` (the always-on
PAPER loop's scaled-integer equity/floor sampling).
`EXPECTED_DENYLISTED_PACKAGES` below is updated to the nine entries the
implementations must append to the
script's own `DENYLISTED_PACKAGES`; until each append lands,
`test_denylisted_packages_constant` fails on a tuple mismatch -- the
expected Gate 1 RED state for each issue (layered on top of issue #12's own
missing-script RED state, which resolves independently).

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
#: cross-check once the module can be loaded. Issue #16 appends
#: `hedgekit/connector` and `hedgekit/screener` to the original three
#: money-path packages from issue #12. Issue #22 appends `hedgekit/forecast`
#: (the pipeline's probability/money-bearing record fields), bringing the
#: total to six. Issue #31 appends `hedgekit/tokens` (the shared approval-token
#: package, whose claims carry money-bearing scaled-integer fields), bringing
#: the total to seven. Issue #43 appends `hedgekit/selector` (the pure Trade
#: Selector, whose price/edge/sizing paths are fixed-point per SPEC S9.1),
#: bringing the total to eight. Issue #48 appends `hedgekit/scheduler` (the
#: always-on PAPER loop, whose equity/floor sampling is scaled-integer money),
#: bringing the total to nine.
EXPECTED_DENYLISTED_PACKAGES = (
    "hedgekit/numeric",
    "hedgekit/ledger",
    "hedgekit/riskkernel",
    "hedgekit/connector",
    "hedgekit/screener",
    "hedgekit/forecast",
    "hedgekit/tokens",
    "hedgekit/selector",
    "hedgekit/scheduler",
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
    """The script's denylist must cover exactly the nine money-path packages."""
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


# --- In-process CLI / file-IO coverage ------------------------------------------
#
# The subprocess-based `_run_cli` tests above exercise the CLI end-to-end but
# run the script in a *separate* interpreter, so they contribute no coverage to
# this test suite's measurement. The tests below drive the same public callables
# (`main`, `_lint_file`, `_expand_targets`, `_denylisted_files`) in-process so
# CI's `--cov=.` scope (which includes `scripts/`) actually measures them, and
# so their behaviour is pinned with real assertions rather than smoke calls.


def test_main_returns_1_and_prints_each_violation(
    lint_module: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main` on a dirty file returns 1 and prints one line per violation."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("x = 1.5\ny = float(2)\n", encoding="utf-8")

    exit_code = lint_module.main([str(bad_file)])

    assert exit_code == 1
    out_lines = capsys.readouterr().out.splitlines()
    assert len(out_lines) == 2
    assert any("FLOAT-001" in line for line in out_lines)
    assert any("FLOAT-004" in line for line in out_lines)
    assert all(str(bad_file) in line for line in out_lines)


def test_main_returns_0_and_prints_nothing_for_clean_file(
    lint_module: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main` on a clean file returns 0 with empty stdout."""
    clean_file = tmp_path / "clean.py"
    clean_file.write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )

    exit_code = lint_module.main([str(clean_file)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_main_no_args_scans_denylisted_packages(
    lint_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main([])` falls back to scanning DENYLISTED_PACKAGES (money path clean)."""
    exit_code = lint_module.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_main_expands_directory_argument(
    lint_module: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory argument is recursively expanded to its `*.py` files."""
    pkg = tmp_path / "pkg"
    nested = pkg / "nested"
    nested.mkdir(parents=True)
    (pkg / "clean.py").write_text("z: int = 3\n", encoding="utf-8")
    (nested / "dirty.py").write_text("w: float = 0\n", encoding="utf-8")

    exit_code = lint_module.main([str(pkg)])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FLOAT-002" in out
    assert "dirty.py" in out
    assert "clean.py" not in out


def test_lint_file_formats_path_line_col_code_message(
    lint_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """`_lint_file` renders `path:line:col CODE message` per violation."""
    target = tmp_path / "sample.py"
    target.write_text("value = 2.5\n", encoding="utf-8")

    lines = lint_module._lint_file(target)

    assert lines == [f"{target}:1:8 FLOAT-001 float literal is banned"]


def test_lint_file_returns_empty_for_clean_file(
    lint_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """`_lint_file` returns no lines when the file is float-free."""
    target = tmp_path / "ok.py"
    target.write_text("value: int = 2\n", encoding="utf-8")

    assert lint_module._lint_file(target) == []


def test_expand_targets_recurses_dirs_and_passes_files_through(
    lint_module: types.ModuleType,
    tmp_path: Path,
) -> None:
    """Directories expand to sorted `*.py`; explicit file paths pass through."""
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "notpython.txt").write_text("", encoding="utf-8")
    explicit = tmp_path / "explicit_file.py"
    explicit.write_text("", encoding="utf-8")

    expanded = lint_module._expand_targets([tmp_path, explicit])

    # Directory expansion is sorted and .py-only; the explicit path is appended.
    assert expanded == [
        tmp_path / "a.py",
        tmp_path / "b.py",
        explicit,
        explicit,
    ]


def test_denylisted_files_only_covers_existing_packages(
    lint_module: types.ModuleType,
) -> None:
    """Scan targets come only from denylisted packages that exist on disk."""
    files = lint_module._denylisted_files()

    repo_root = lint_module._REPO_ROOT
    numeric_dir = repo_root / "hedgekit/numeric"
    # hedgekit/numeric exists in this repo, so at least its files are returned.
    assert files, "expected at least the numeric package to be scanned"
    assert all(path.suffix == ".py" for path in files)
    assert any(numeric_dir in path.parents for path in files)
    # Every returned file must live under an existing denylisted package dir.
    existing_dirs = [
        repo_root / pkg
        for pkg in lint_module.DENYLISTED_PACKAGES
        if (repo_root / pkg).is_dir()
    ]
    assert all(
        any(directory in path.parents for directory in existing_dirs) for path in files
    )


def test_denylisted_files_skips_nonexistent_packages(
    lint_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denylisted prefix with no directory on disk is silently skipped."""
    monkeypatch.setattr(
        lint_module,
        "DENYLISTED_PACKAGES",
        ("hedgekit/definitely_not_a_real_package",),
    )

    assert lint_module._denylisted_files() == []


# --- Signature-annotation edge cases (branch coverage) --------------------------


def test_vararg_and_kwarg_float_annotations_are_flagged(
    lint_module: types.ModuleType,
) -> None:
    """`*args: float` and `**kwargs: float` annotations are both caught."""
    source = "def f(*args: float, **kwargs: float) -> None:\n    return None\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert len(violations) == 2
    assert {v.code for v in violations} == {"FLOAT-002"}


def test_unannotated_function_yields_no_violation(
    lint_module: types.ModuleType,
) -> None:
    """A function with no annotations at all produces nothing (skip branches)."""
    source = "def f(x, *args, **kwargs):\n    return x\n"

    assert lint_module.collect_violations(source, "example.py") == []


def test_float_named_via_subscript_call_is_not_a_cast(
    lint_module: types.ModuleType,
) -> None:
    """A call whose callee is neither Name nor Attribute is not a float cast.

    Exercises the final `return False` of `_names_float`: here the callee is a
    Subscript (`registry['float']()`), so no FLOAT-004 is emitted.
    """
    source = "registry = {}\nregistry['float']()\n"

    violations = lint_module.collect_violations(source, "example.py")

    assert all(v.code != "FLOAT-004" for v in violations)
