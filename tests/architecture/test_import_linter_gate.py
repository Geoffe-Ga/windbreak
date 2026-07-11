"""Failing-first tests wiring `import-linter` into the quality gate as
defense-in-depth for the existing pure-`ast` boundary checks (issue #91).

`tests/architecture/test_import_boundaries.py` and
`tests/riskkernel/test_process_isolation.py` already enforce every SPEC S5.2/
S5.3/S8.3 boundary at the pytest layer via hand-rolled `ast` walkers. Those
remain the PRIMARY, stronger gate (they catch relative-import and re-export
loopholes a `forbidden_modules` contract cannot express). This module adds a
SECOND, independent enforcement path -- `lint-imports` run as an actual CI
gate -- so a future refactor that slips past one checker still trips the
other.

RED-today inventory (see each test's docstring for the specific reason):

- `test_requirements_dev_references_import_linter` (in
  `tests/toolchain/test_toolchain_pins.py`, extended by this issue) and
  `test_constraints_covers_every_required_tool_name` -- `import-linter` is
  not yet pinned/listed anywhere.
- `test_architecture_script_exists_and_is_executable`,
  `test_architecture_script_invokes_lint_imports_or_run_check`,
  `test_check_all_wires_architecture_script_into_run_check` --
  `scripts/architecture.sh` does not exist yet and `scripts/check-all.sh`
  has no `run_check` line for it.
- `test_architecture_script_fails_loudly_without_lint_imports_on_path` --
  same: the script that would print the "not found" message doesn't exist.
- `test_lint_imports_passes_against_the_real_repo_configuration` -- RED
  until the implementation adds `allow_indirect_imports = True` to each
  `forbidden` contract in `plans/architecture/.importlinter`. Verified
  empirically (import-linter 2.13): as-is, `lint-imports` follows the FULL
  TRANSITIVE import graph and reports 3 broken contracts (e.g.
  `windbreak.forecast.canary -> windbreak.alerts -> ... -> windbreak.config`),
  even though every DIRECT import respects the boundary (which is all the
  `ast` checkers above inspect). `allow_indirect_imports = True` restricts
  each contract to direct imports only, mirroring that `ast` semantics
  exactly.
- The `source_modules` enumeration drift-guard tests -- both
  `signing-key-isolation` and `order-submission-client-isolation` are
  currently missing `windbreak.__main__` (the `python -m windbreak` entry
  point) from their `source_modules` lists. The drift-guard now enumerates
  bare top-level MODULES dynamically (`_top_level_module_names()`), not just
  package directories, so a future bare module is auto-covered too instead
  of depending on a hand-maintained allowlist.

The hermetic synthetic-package tests
(`test_lint_imports_flags_a_deliberate_direct_forbidden_import` and its GREEN
companion) are self-contained proof that `lint-imports` genuinely evaluates
the import graph rather than trivially always failing or always passing; they
do not depend on any repo wiring and should already pass once import-linter
itself is installed.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: Repo root, derived from this test file's own location
#: (`<root>/tests/architecture/test_import_linter_gate.py`).
_REPO_ROOT = Path(__file__).resolve().parents[2]

_WINDBREAK_PACKAGE_DIR = _REPO_ROOT / "windbreak"
_IMPORTLINTER_PATH = _REPO_ROOT / "plans" / "architecture" / ".importlinter"
_IMPORTLINTER_RELPATH = Path("plans", "architecture", ".importlinter")
_ARCHITECTURE_SCRIPT = _REPO_ROOT / "scripts" / "architecture.sh"
_CHECK_ALL_SCRIPT = _REPO_ROOT / "scripts" / "check-all.sh"
_RUN_CHECK_SCRIPT_REL = "plans/architecture/run-check.sh"
_LINT_IMPORTS_INVOCATION = "lint-imports --config plans/architecture/.importlinter"

_SIGNING_CONTRACT = "importlinter:contract:signing-key-isolation"
_ORDER_SUBMISSION_CONTRACT = "importlinter:contract:order-submission-client-isolation"

#: Packages deliberately exempt from each drift-guarded contract's
#: `source_modules` -- because they legitimately need the forbidden import,
#: not because they were merely forgotten. Mirrors the allowlists already
#: documented in `plans/architecture/.importlinter`'s own comments.
_SIGNING_EXEMPT_PACKAGES = frozenset({"riskkernel"})
_ORDER_SUBMISSION_EXEMPT_PACKAGES = frozenset(
    {"order_gateway", "connector", "scheduler"}
)

#: Top-level `windbreak/*.py` MODULES already listed in both contracts today
#: (`main`, `logging_setup`), used by the regression-guard below, kept
#: distinct from the DYNAMIC `_top_level_module_names()` enumeration so a
#: future edit can't silently shrink the enumeration to make the
#: regression-guard pass without the drift-guard actually failing.
_ALREADY_PRESENT_MODULES = frozenset({"main", "logging_setup"})

#: Contract name embedded in the synthetic hermetic-test `.importlinter`
#: config below, asserted against `lint-imports`' own output.
_SYNTHETIC_CONTRACT_NAME = "Sandbox must not import secret"


# --- Helpers -------------------------------------------------------------


def _top_level_package_names() -> frozenset[str]:
    """Enumerate first-level `windbreak/<name>/__init__.py` packages.

    Only directories carrying an `__init__.py` count as packages; bare
    top-level modules (`main.py`, `logging_setup.py`, `__main__.py`) are
    excluded here and covered instead by `_top_level_module_names()`.

    Returns:
        The bare (undotted) names of every first-level `windbreak` package,
        e.g. `{"alerts", "connector", "riskkernel", ...}`.
    """
    return frozenset(
        child.name
        for child in _WINDBREAK_PACKAGE_DIR.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    )


def _top_level_module_names() -> frozenset[str]:
    """Enumerate bare top-level `windbreak/*.py` modules (no subpackage).

    Dynamic counterpart to `_top_level_package_names()`: walks every
    `windbreak/*.py` file directly (not `windbreak/*/__init__.py`) and
    returns its stem, excluding `__init__` itself. This automatically picks
    up any future bare module -- e.g. `windbreak/__main__.py`, the
    `python -m windbreak` entry point -- without needing a hand-maintained
    allowlist edited alongside it.

    Returns:
        The bare (undotted) stems of every top-level `windbreak/*.py` module
        that is not `__init__`, e.g. `{"main", "logging_setup", "__main__"}`.
    """
    return frozenset(
        child.stem
        for child in _WINDBREAK_PACKAGE_DIR.glob("*.py")
        if child.stem != "__init__"
    )


def _contract_source_modules(section: str) -> frozenset[str]:
    """Parse one `.importlinter` contract section's `source_modules` list.

    Args:
        section: The `configparser` section name, e.g.
            `"importlinter:contract:signing-key-isolation"`.

    Returns:
        The whitespace-separated `source_modules` entries, verbatim
        (e.g. `{"windbreak.alerts", "windbreak.config", ...}`).
    """
    parser = configparser.ConfigParser()
    read_files = parser.read(_IMPORTLINTER_PATH, encoding="utf-8")
    assert read_files, f"could not read {_IMPORTLINTER_PATH}"
    raw = parser.get(section, "source_modules", fallback="")
    return frozenset(raw.split())


def _read_text_or_fail(path: Path) -> str:
    """Read a file's text, failing the test loudly (not erroring) if absent.

    Args:
        path: The file expected to exist.

    Returns:
        The file's full text content.
    """
    if not path.is_file():
        pytest.fail(f"{path} does not exist yet -- issue #91 RED")
    return path.read_text(encoding="utf-8")


def _lint_imports_executable() -> str:
    """Resolve the `lint-imports` console script.

    Returns:
        The absolute resolved path when found on `PATH`, else the bare
        command name (letting `subprocess.run` attempt its own `PATH`
        lookup, so a legitimately-missing tool still fails loudly rather
        than being silently skipped).
    """
    return shutil.which("lint-imports") or "lint-imports"


def _run_lint_imports(
    cwd: Path, *, config_relpath: Path | str = ".importlinter"
) -> subprocess.CompletedProcess[str]:
    """Run `lint-imports --config <cwd>/<config_relpath>` from `cwd`.

    Args:
        cwd: Working directory containing both the package tree to check
            and the `.importlinter` config file; also prepended to
            `PYTHONPATH` so the checked package is importable.
        config_relpath: The config file's path, relative to `cwd`.

    Returns:
        The completed subprocess result (never raises on nonzero exit).
    """
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{cwd}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(cwd)
    )
    executable = _lint_imports_executable()
    try:
        return subprocess.run(
            [executable, "--config", str(cwd / config_relpath)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except FileNotFoundError:
        pytest.fail(
            "lint-imports not found on PATH -- issue #91 needs it pinned in "
            "requirements-dev.txt and the shared venv reprovisioned "
            "(scripts/provision-venv.sh)"
        )


def _write_boundarypkg(tmp_path: Path, *, sandbox_source: str) -> None:
    """Write a minimal synthetic `boundarypkg` package + its own
    `.importlinter` config into `tmp_path`, for a fully hermetic
    `lint-imports` run independent of the real `windbreak` tree.

    Args:
        tmp_path: The pytest-provided temporary directory.
        sandbox_source: The source text for `boundarypkg/sandbox.py`.
    """
    package_dir = tmp_path / "boundarypkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "sandbox.py").write_text(sandbox_source, encoding="utf-8")
    (package_dir / "secret.py").write_text("TOKEN = 'shh'\n", encoding="utf-8")

    config = f"""\
[importlinter]
root_package = boundarypkg

[importlinter:contract:sandbox-boundary]
name = {_SYNTHETIC_CONTRACT_NAME}
type = forbidden
source_modules =
    boundarypkg.sandbox
forbidden_modules =
    boundarypkg.secret
allow_indirect_imports = True
"""
    (tmp_path / ".importlinter").write_text(config, encoding="utf-8")


# --- 1. Toolchain wiring: scripts/architecture.sh + check-all.sh ---------


def test_architecture_script_exists_and_is_executable() -> None:
    """`scripts/architecture.sh` exists and carries the executable bit, so
    `check-all.sh` (and a developer invoking it directly) can run it exactly
    like every other `scripts/*.sh` quality gate.
    """
    assert _ARCHITECTURE_SCRIPT.is_file(), (
        f"{_ARCHITECTURE_SCRIPT} does not exist yet -- issue #91 RED"
    )
    assert os.access(_ARCHITECTURE_SCRIPT, os.X_OK), (
        f"{_ARCHITECTURE_SCRIPT} exists but is not executable"
    )


def test_architecture_script_invokes_lint_imports_or_run_check() -> None:
    """`scripts/architecture.sh` actually runs the import-linter check,
    either by delegating to `plans/architecture/run-check.sh` or invoking
    `lint-imports --config plans/architecture/.importlinter` directly.
    """
    text = _read_text_or_fail(_ARCHITECTURE_SCRIPT)

    assert _RUN_CHECK_SCRIPT_REL in text or _LINT_IMPORTS_INVOCATION in text


def test_check_all_wires_architecture_script_into_run_check() -> None:
    """`check-all.sh`'s `run_check` dispatcher includes an `architecture.sh`
    entry, so `./scripts/check-all.sh` -- Gate 1 -- fails the moment a
    boundary contract breaks, exactly like every other quality gate.
    """
    text = _CHECK_ALL_SCRIPT.read_text(encoding="utf-8")

    architecture_lines = [
        line
        for line in text.splitlines()
        if "run_check" in line and "architecture.sh" in line
    ]
    assert architecture_lines, (
        "scripts/check-all.sh has no run_check line invoking architecture.sh"
    )


@pytest.mark.timeout(30)
def test_architecture_script_fails_loudly_without_lint_imports_on_path() -> None:
    """Running `scripts/architecture.sh` with `lint-imports` unreachable on
    `PATH` exits nonzero and prints a clear "not found"/"not installed"
    message, instead of silently succeeding or crashing with a bare
    traceback -- matching the fail-loud convention already used by
    `plans/architecture/run-check.sh`.
    """
    bash_path = shutil.which("bash") or "/bin/bash"
    env = {"PATH": "/usr/bin:/bin"}

    result = subprocess.run(
        [bash_path, str(_ARCHITECTURE_SCRIPT)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    combined_output = (result.stdout + result.stderr).lower()

    assert result.returncode != 0, (
        "expected a nonzero exit with lint-imports unreachable, got 0: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "not found" in combined_output or "not installed" in combined_output, (
        f"expected a 'not found'/'not installed' message, got: {combined_output!r}"
    )


# --- 2. Hermetic proof: lint-imports genuinely evaluates the graph -------


@pytest.mark.timeout(30)
def test_lint_imports_flags_a_deliberate_direct_forbidden_import(
    tmp_path: Path,
) -> None:
    """A hermetic synthetic package with a deliberate DIRECT forbidden
    import (`boundarypkg.sandbox` -> `boundarypkg.secret`) fails its
    `forbidden` contract (nonzero exit, "BROKEN"/contract name in output),
    proving `lint-imports` actually evaluates the graph.
    """
    _write_boundarypkg(tmp_path, sandbox_source="import boundarypkg.secret\n")

    result = _run_lint_imports(tmp_path)

    assert result.returncode != 0, (
        f"expected nonzero exit for a violating import, got 0: {result.stdout}"
    )
    combined = result.stdout + result.stderr
    assert "BROKEN" in combined or _SYNTHETIC_CONTRACT_NAME in combined, (
        f"expected the broken contract to be named in output, got: {combined!r}"
    )


@pytest.mark.timeout(30)
def test_lint_imports_passes_when_the_forbidden_import_is_removed(
    tmp_path: Path,
) -> None:
    """Companion GREEN case: the same synthetic package WITHOUT the
    forbidden import passes cleanly (exit 0) -- proof `lint-imports` doesn't
    always fail regardless of input.
    """
    _write_boundarypkg(tmp_path, sandbox_source="VALUE = 1\n")

    result = _run_lint_imports(tmp_path)

    assert result.returncode == 0, (
        f"expected exit 0 for a clean package, got {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )


# --- 3. Acceptance: the real repo config passes ---------------------------


@pytest.mark.timeout(60)
def test_lint_imports_passes_against_the_real_repo_configuration() -> None:
    """The real `plans/architecture/.importlinter`, run against the actual
    `windbreak` tree, is fully KEPT (exit 0).

    RED today (verified empirically against import-linter 2.13): as-is,
    every `forbidden` contract follows the FULL TRANSITIVE import graph, so
    3 contracts report BROKEN even though every DIRECT import respects the
    boundary (e.g. `windbreak.forecast.canary` transitively reaches
    `windbreak.config` via `windbreak.alerts`, despite never importing it
    directly). The implementation step adds `allow_indirect_imports = True`
    to each `forbidden` contract, restricting evaluation to direct imports
    only -- exactly the semantics the pure-`ast` checkers already enforce.
    This also guards against a typo'd module name, since `lint-imports`
    errors on any module it cannot resolve.
    """
    result = _run_lint_imports(_REPO_ROOT, config_relpath=_IMPORTLINTER_RELPATH)

    assert result.returncode == 0, (
        "lint-imports did not exit 0 against the real repo config:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- 4. Enumeration drift-guard: every non-exempt package is covered ------


def test_signing_key_isolation_source_modules_cover_every_nonexempt_package() -> None:
    """Every `windbreak` top-level package/module except `riskkernel` (the
    key's owner) is a `signing-key-isolation` forbidden source, so a newly
    added package OR bare module is automatically covered instead of
    silently falling outside the boundary. Bare top-level modules
    (`main`, `logging_setup`, `__main__`) have no per-contract exemption
    set today, so all of them are required. RED today: `windbreak.__main__`
    is missing (the `python -m windbreak` entry point).
    """
    required = (
        _top_level_package_names() | _top_level_module_names()
    ) - _SIGNING_EXEMPT_PACKAGES
    source_modules = _contract_source_modules(_SIGNING_CONTRACT)

    missing = sorted(
        f"windbreak.{name}"
        for name in required
        if f"windbreak.{name}" not in source_modules
    )
    assert not missing, f"{_SIGNING_CONTRACT} source_modules is missing: {missing}"


def test_order_submission_source_modules_cover_every_nonexempt_package() -> None:
    """Every `windbreak` top-level package/module except `order_gateway`,
    `connector`, and `scheduler` (the documented PAPER-mode allowlist, issue
    #48) is an `order-submission-client-isolation` forbidden source. Bare
    top-level modules (`main`, `logging_setup`, `__main__`) have no
    per-contract exemption set today, so all of them are required. RED
    today: `windbreak.__main__` is missing (the `python -m windbreak` entry
    point).
    """
    required = (
        _top_level_package_names() | _top_level_module_names()
    ) - _ORDER_SUBMISSION_EXEMPT_PACKAGES
    source_modules = _contract_source_modules(_ORDER_SUBMISSION_CONTRACT)

    missing = sorted(
        f"windbreak.{name}"
        for name in required
        if f"windbreak.{name}" not in source_modules
    )
    assert not missing, (
        f"{_ORDER_SUBMISSION_CONTRACT} source_modules is missing: {missing}"
    )


@pytest.mark.parametrize("section", [_SIGNING_CONTRACT, _ORDER_SUBMISSION_CONTRACT])
def test_already_present_modules_remain_in_source_modules(section: str) -> None:
    """Regression-guard (currently GREEN, kept alongside the drift-guard
    above): the two top-level MODULES already enumerated in both contracts
    (`windbreak.main`, `windbreak.logging_setup`) stay present -- a future
    edit to add the missing packages can't accidentally drop these too.
    """
    source_modules = _contract_source_modules(section)

    missing = sorted(
        f"windbreak.{name}"
        for name in _ALREADY_PRESENT_MODULES
        if f"windbreak.{name}" not in source_modules
    )
    assert not missing, f"{section} source_modules dropped: {missing}"
