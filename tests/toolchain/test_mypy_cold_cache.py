"""Failing-first tests pinning the Gate-2 mypy cold-cache-parity contract.

Issue #179: `scripts/typecheck.sh` currently lets mypy write its
incremental on-disk cache to the default, persistent `.mypy_cache/`
directory at the repo root, and every local invocation reuses whatever
that directory already contains. CI, by contrast, always starts from an
empty cache (a fresh checkout has no `.mypy_cache/`). Because mypy's
incremental mode remembers which modules it already resolved certain
cross-module facts for -- notably `no-implicit-reexport` and
`attr-defined` -- a warm local cache can silently green a run that a
genuinely cold cache (i.e. what CI actually runs) would fail on. That
makes Gate 1 (`./scripts/check-all.sh`) a false-positive for exactly the
class of error CI is supposed to catch.

The target fix (implemented in a later step, not by this test module):
before invoking `mypy`, `scripts/typecheck.sh` sets
`MYPY_CACHE_DIR="$(mktemp -d)"`, `export`s it (mypy reads
`MYPY_CACHE_DIR` from its process environment when no explicit
`--cache-dir` flag is passed), and registers `trap 'rm -rf
"$MYPY_CACHE_DIR"' EXIT` so the per-run temp directory is always cleaned
up, regardless of how the script exits. The literal invocation `mypy
windbreak/ scripts/` -- separately pinned in lockstep with the
pre-commit mypy hook's scope by
`tests/toolchain/test_precommit_scope.py` -- must stay byte-identical;
this module re-asserts that substring so a cache-dir fix that
accidentally mangles the command also fails loudly here.

These assertions begin life as Gate 1 RED: the unmodified
`scripts/typecheck.sh` has none of the `MYPY_CACHE_DIR` / `mktemp -d` /
`export` / `trap ... EXIT` mechanism, so the static source-assertion
tests below fail against the present script. The behavioral test may
also fail RED today, because the unmodified script lets mypy create (or
touch) the repo-root `.mypy_cache/` directory rather than isolating its
cache away from it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TYPECHECK_SCRIPT_PATH = _REPO_ROOT / "scripts" / "typecheck.sh"
_MYPY_CACHE_DIR_PATH = _REPO_ROOT / ".mypy_cache"

#: The bin directory of the interpreter running this test. In the real
#: Gate-2 path `check-all.sh` prepends the shared `.venv/bin` to PATH
#: before invoking `test.sh` -> pytest, so pytest runs under that venv
#: interpreter and this resolves to `.venv/bin` -- exactly where `mypy`
#: lives -- on every developer machine and on CI alike (CI installs mypy
#: into the same interpreter's environment via requirements-dev). Derived
#: from `sys.executable` rather than hardcoded so it is portable and the
#: PATH-prepend branch below is exercised everywhere, not just on one box.
_INTERPRETER_BIN_DIR = Path(sys.executable).parent

#: Matches an assignment to MYPY_CACHE_DIR whose right-hand side calls
#: `mktemp -d`, e.g. `MYPY_CACHE_DIR="$(mktemp -d)"` or
#: `MYPY_CACHE_DIR=$(mktemp -d)`. Deliberately loose on quoting and
#: whitespace so it does not over-fit to one exact shell style.
_MKTEMP_ASSIGNMENT_PATTERN = re.compile(r"MYPY_CACHE_DIR\s*=.*mktemp\s+-d")

#: Matches an `export MYPY_CACHE_DIR` statement, whether or not a value is
#: assigned directly on the same `export` line.
_EXPORT_PATTERN = re.compile(r"export\s+MYPY_CACHE_DIR\b")

#: Matches a `trap ... EXIT` line whose trapped command references
#: MYPY_CACHE_DIR -- i.e. an EXIT trap that cleans up the per-run cache
#: dir specifically, not just any EXIT trap the script might register.
_EXIT_TRAP_PATTERN = re.compile(r"trap\s+.*MYPY_CACHE_DIR.*\bEXIT\b")

#: The exact mypy invocation guarded (in lockstep with the pre-commit
#: hook's scope) by test_precommit_scope.py; re-pinned here so this
#: feature cannot mangle it while wiring up MYPY_CACHE_DIR.
_PINNED_MYPY_COMMAND = "mypy windbreak/ scripts/"


def _read_typecheck_source() -> str:
    """Read scripts/typecheck.sh's full source text.

    Returns:
        The script's contents, decoded as UTF-8.
    """
    return _TYPECHECK_SCRIPT_PATH.read_text(encoding="utf-8")


def _snapshot_cache_tree() -> dict[str, float] | None:
    """Snapshot the repo-root `.mypy_cache/` tree as a path -> mtime map.

    Recurses into every descendant file and subdirectory (mypy stores its
    data under `.mypy_cache/<version>/...`), so a regression that writes
    *into* an existing cache tree is detected even when the top-level
    directory's own mtime does not change -- a stronger guard than
    snapshotting the top-level mtime alone.

    Returns:
        A mapping of each path (relative to the repo root, as a string) to
        its modification time, or None if `.mypy_cache/` does not exist.
    """
    if not _MYPY_CACHE_DIR_PATH.exists():
        return None
    return {
        str(path.relative_to(_REPO_ROOT)): path.stat().st_mtime
        for path in (_MYPY_CACHE_DIR_PATH, *_MYPY_CACHE_DIR_PATH.rglob("*"))
    }


def test_typecheck_script_assigns_mypy_cache_dir_from_mktemp() -> None:
    """The script must derive MYPY_CACHE_DIR from a fresh `mktemp -d` call.

    A per-run temporary directory (rather than a fixed, reused path)
    guarantees each invocation starts from a genuinely empty mypy cache,
    matching CI's cold-cache behavior instead of reusing whatever
    `.mypy_cache/` a prior local run left on disk.
    """
    source = _read_typecheck_source()

    assert _MKTEMP_ASSIGNMENT_PATTERN.search(source), (
        "scripts/typecheck.sh does not assign MYPY_CACHE_DIR from a "
        "`mktemp -d` call -- expected something like "
        'MYPY_CACHE_DIR="$(mktemp -d)"'
    )


def test_typecheck_script_exports_mypy_cache_dir() -> None:
    """MYPY_CACHE_DIR must be exported so the `mypy` child process sees it.

    mypy only honors `MYPY_CACHE_DIR` via its process environment (absent
    an explicit `--cache-dir` flag); a plain shell-local assignment
    without `export` would silently no-op, and mypy would fall back to
    its default `.mypy_cache/` location regardless of the assignment.
    """
    source = _read_typecheck_source()

    assert _EXPORT_PATTERN.search(source), (
        "scripts/typecheck.sh does not `export MYPY_CACHE_DIR` -- the "
        "assignment alone is invisible to the mypy child process"
    )


def test_typecheck_script_traps_exit_to_remove_the_temp_cache_dir() -> None:
    """An EXIT trap must remove the per-run MYPY_CACHE_DIR temp directory.

    Without cleanup, every local `typecheck.sh` run would leak a fresh
    `mktemp -d` directory under the system temp root on every invocation,
    regardless of whether the run passed, failed, or was interrupted.
    """
    source = _read_typecheck_source()

    assert _EXIT_TRAP_PATTERN.search(source), (
        "scripts/typecheck.sh does not register an EXIT trap referencing "
        "MYPY_CACHE_DIR -- expected something like "
        "trap 'rm -rf \"$MYPY_CACHE_DIR\"' EXIT"
    )


def test_typecheck_script_still_runs_the_pinned_mypy_command() -> None:
    """The literal `mypy windbreak/ scripts/` invocation must stay intact.

    Guards this cold-cache fix specifically against mangling the pinned
    command while wiring up MYPY_CACHE_DIR -- complementing (not
    replacing) `test_precommit_scope.py`'s lockstep guard, which pins the
    same substring against the pre-commit mypy hook's scope.
    """
    source = _read_typecheck_source()

    assert _PINNED_MYPY_COMMAND in source, (
        f"scripts/typecheck.sh no longer runs {_PINNED_MYPY_COMMAND!r} -- "
        "the cold-cache fix must not change the mypy invocation itself"
    )


def test_typecheck_script_run_does_not_touch_repo_root_mypy_cache() -> None:
    """Running the script must never create or modify the shared cache dir.

    Snapshots the repo-root `.mypy_cache/` tree (recursively, as a path ->
    mtime map, or None when absent) before invoking `bash
    scripts/typecheck.sh`, then asserts that snapshot is byte-for-byte
    unchanged afterward -- proving the run isolated its cache in a per-run
    temp directory instead of creating, deleting, or writing into the
    persistent one that local `mypy`/pre-commit runs also share. The
    recursive snapshot catches a regression that writes into a nested
    cache file without bumping the top-level directory's mtime.

    Fails loudly (never skips) if `mypy` is unavailable: mypy is a
    required Gate-2 tool, and a missing binary would make this test pass
    for the wrong reason (the script's "Warning: mypy not installed,
    skipping" early-exit path) rather than actually exercising the
    cold-cache isolation mechanism. Resolves mypy from the running
    interpreter's own bin dir (the shared `.venv/bin` in the real Gate-2
    path) so the test finds the same `mypy` the real Gate-2 run uses, and
    stays robust regardless of how the enclosing pytest process itself
    was launched.
    """
    env = dict(os.environ)
    interpreter_mypy = _INTERPRETER_BIN_DIR / "mypy"
    if interpreter_mypy.is_file():
        env["PATH"] = f"{_INTERPRETER_BIN_DIR}{os.pathsep}{env.get('PATH', '')}"

    mypy_on_path = shutil.which("mypy", path=env.get("PATH"))
    assert mypy_on_path is not None, (
        "mypy is not on PATH (even after prepending "
        f"{_INTERPRETER_BIN_DIR}) -- mypy is a required Gate-2 tool and "
        "this test cannot meaningfully exercise cold-cache isolation "
        "without it"
    )

    before = _snapshot_cache_tree()

    result = subprocess.run(
        ["bash", "scripts/typecheck.sh"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, (
        f"typecheck.sh exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    after = _snapshot_cache_tree()
    assert after == before, (
        "scripts/typecheck.sh created, deleted, or modified the repo-root "
        ".mypy_cache tree -- it must isolate its cache in a per-run "
        "MYPY_CACHE_DIR temp directory instead of touching the persistent "
        f"one.\nbefore={before}\nafter={after}"
    )
