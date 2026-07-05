"""Formatter-authority regression guard (issue #104).

Pins ruff-format as the *single* formatter authority by giving it real
source surface in exactly the two shapes that historically oscillated
between ruff-format and black's competing style opinions: a long
`assert expr, (f"...")` construct, and a long `Callable[...]` return
annotation. `test_toolchain_dir_is_ruff_format_stable` then asserts
`ruff format --check` is a no-op on this directory -- i.e. ruff-format is
idempotent here and never wants to "fix" what it just formatted.

Unlike `test_toolchain_pins.py`, this module is not required to start RED:
once ruff-format is wired up as the sole authority it is expected to pass
immediately. Its job is to catch a *regression* (e.g. black creeping back
in, or a ruff-format version bump changing its opinion on these
constructs) rather than to pin a not-yet-built feature.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_THIS_DIR = Path(__file__).resolve().parent


def test_long_assert_message_formats_stably() -> None:
    """A long `assert expr, (f"...")` construct is what oscillated ruff<->black.

    This pins the exact source shape so `ruff format --check` in
    `test_toolchain_dir_is_ruff_format_stable` has real regression surface
    for the parenthesized-message wrapping behavior.
    """
    expected_total = 52
    computed_total = sum(range(1, 10)) + 7

    assert computed_total == expected_total, (
        f"expected the computed total to equal {expected_total} after "
        f"summing 1..9 and adding 7, but got {computed_total} instead -- "
        "this message is intentionally long enough to force a formatter's "
        "line-wrapping decision around the trailing comma and parens"
    )


@pytest.fixture
def long_callable_return_fixture() -> Callable[
    [int, str, bool], dict[str, tuple[int, ...]]
]:
    """Provide a callable whose return-type annotation is intentionally long.

    Long `Callable[...]` annotations of this shape are exactly what
    diverged between ruff-format and black's parenthesization/wrapping
    rules; this fixture exists purely to give
    `test_toolchain_dir_is_ruff_format_stable` real code to format-check.

    Returns:
        A callable that folds its three arguments into a small mapping.
    """

    def _factory(count: int, label: str, flag: bool) -> dict[str, tuple[int, ...]]:
        return {label: tuple(range(count))} if flag else {label: ()}

    return _factory


def test_long_callable_return_fixture_behaves(
    long_callable_return_fixture: Callable[..., dict[str, tuple[int, ...]]],
) -> None:
    """The long-signature fixture still behaves like the callable it is."""
    result = long_callable_return_fixture(3, "x", True)

    assert result == {"x": (0, 1, 2)}


def test_toolchain_dir_is_ruff_format_stable() -> None:
    """`ruff format --check` is a no-op (idempotent) on this test directory.

    Fails loudly rather than skipping when ruff is missing: ruff is a
    required tool for this repo's formatting gate (issue #104 makes it the
    sole formatter authority), so a missing binary is itself a real,
    actionable failure -- not something to silently skip past.
    """
    ruff_path = shutil.which("ruff")
    assert ruff_path is not None, (
        "ruff is not installed/on PATH -- it is required as the sole "
        "formatter authority (issue #104) and this regression guard "
        "cannot run without it"
    )

    result = subprocess.run(
        [ruff_path, "format", "--check", str(_THIS_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
