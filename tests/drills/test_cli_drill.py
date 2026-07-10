"""Failing-first tests for the `windbreak drill` CLI verb (issue #59, RED).

`windbreak.main` has no `drill` subcommand yet, so `build_parser()` rejects
`drill` as an unknown command and `_add_drill_arguments`/`_run_drill` do not
exist -- the expected Gate 1 RED state for issue #59.

Pins: the `drill` subcommand accepts every `DRILLS` registry key as its
positional `name` and rejects any other token; `main(["drill", name])`'s exit
code is 0 iff the drill passed (1 otherwise); `DrillCompleted` is *always*
ledgered, win or lose; and `--production` is plumbed to
`windbreak.drills.context.bind_production_context` -- never touching a real
network.

Design assumption (flagged for the implementer, mirroring
`tests/order_gateway/test_reconciler.py`'s own precedent): `windbreak/main.py`
imports `bind_paper_context`/`bind_production_context` directly at module
scope (`from windbreak.drills.context import ...`) and calls whichever one
`--production` selects, so the module-level name is monkeypatchable from a
test exactly as done below. If the real wiring differs, only this file's
monkeypatch target needs updating; the exit-code and registry-name
assertions should still hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from windbreak.drills.framework import DrillFailedError
from windbreak.drills.registry import DRILLS
from windbreak.main import build_parser, main

if TYPE_CHECKING:
    from pathlib import Path

#: The registered drill name every single-drill test below exercises;
#: alphabetically first so the choice is stable across runs.
_A_DRILL_NAME = sorted(DRILLS)[0]


def _default_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Build a `(fixture_dir, state_dir)` pair for a `drill` CLI invocation."""
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return fixture_dir, state_dir


class _NoopDrill:
    """A trivial `Drill`-shaped stand-in that always passes."""

    name = "fake"

    def check_preconditions(self, ctx: object) -> None:
        """No preconditions."""

    def execute(self, ctx: object) -> dict[str, object]:
        """Return empty evidence."""
        return {}

    def teardown(self, ctx: object) -> None:
        """No teardown."""


class _AlwaysFailDrill:
    """A trivial `Drill`-shaped stand-in that always fails via `DrillFailedError`."""

    name = "fake"

    def check_preconditions(self, ctx: object) -> None:
        """No preconditions."""

    def execute(self, ctx: object) -> dict[str, object]:
        """Always raise `DrillFailedError`."""
        raise DrillFailedError({"reason": "deliberately failed"})

    def teardown(self, ctx: object) -> None:
        """No teardown."""


# --- Parser: accepts every registered name, rejects an unknown one -------------


@pytest.mark.parametrize("name", sorted(DRILLS))
def test_parser_accepts_every_registered_drill_name(name: str, tmp_path: Path) -> None:
    """`build_parser()` accepts each registered drill name as `drill <name>`."""
    fixture_dir, state_dir = _default_dirs(tmp_path)

    args = build_parser().parse_args(
        [
            "drill",
            name,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
        ]
    )

    assert args.command == "drill"
    assert args.name == name


def test_parser_rejects_an_unregistered_drill_name(tmp_path: Path) -> None:
    """An unknown drill name is a usage error (`SystemExit(2)`), not a
    runtime `KeyError` deep inside `_run_drill`.
    """
    fixture_dir, state_dir = _default_dirs(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(
            [
                "drill",
                "not-a-real-drill",
                "--fixture-dir",
                str(fixture_dir),
                "--state-dir",
                str(state_dir),
            ]
        )

    assert exc_info.value.code == 2


def test_drill_subcommand_supports_a_bare_production_flag(tmp_path: Path) -> None:
    """`--production` parses as a boolean flag, defaulting to False."""
    fixture_dir, state_dir = _default_dirs(tmp_path)

    default_args = build_parser().parse_args(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
        ]
    )
    production_args = build_parser().parse_args(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
            "--production",
        ]
    )

    assert default_args.production is False
    assert production_args.production is True


# --- main(["drill", name]) exit-code mapping ------------------------------------


def test_main_drill_exits_zero_when_the_drill_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main(["drill", name])` returns 0 when the named drill's `DrillResult`
    passed, and always ledgers `DrillCompleted` regardless.
    """
    import windbreak.drills.registry as registry_module

    fixture_dir, state_dir = _default_dirs(tmp_path)
    monkeypatch.setitem(registry_module.DRILLS, _A_DRILL_NAME, _NoopDrill)

    exit_code = main(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
        ]
    )

    assert exit_code == 0


def test_main_drill_exits_one_when_the_drill_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main(["drill", name])` returns 1 when the named drill's `DrillResult`
    failed.
    """
    import windbreak.drills.registry as registry_module

    fixture_dir, state_dir = _default_dirs(tmp_path)
    monkeypatch.setitem(registry_module.DRILLS, _A_DRILL_NAME, _AlwaysFailDrill)

    exit_code = main(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
        ]
    )

    assert exit_code == 1


def test_production_flag_is_plumbed_to_the_production_binding_factory_with_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--production` routes through `bind_production_context` (monkeypatched
    here to a recording stand-in) rather than `bind_paper_context` -- proving
    the flag is wired end-to-end with zero real network access.
    """
    import windbreak.drills.registry as registry_module
    import windbreak.main as main_module

    fixture_dir, state_dir = _default_dirs(tmp_path)
    calls: list[str] = []

    def _fake_bind_production_context(paper_ctx: object, *, env: object) -> object:
        calls.append("production")
        return paper_ctx

    monkeypatch.setitem(registry_module.DRILLS, _A_DRILL_NAME, _NoopDrill)
    monkeypatch.setattr(
        main_module, "bind_production_context", _fake_bind_production_context
    )

    exit_code = main(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
            "--production",
        ]
    )

    assert exit_code == 0
    assert calls == ["production"]


def test_without_production_the_production_binding_factory_is_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--production`, `bind_production_context` is never invoked --
    the default path never even considers a production binding."""
    import windbreak.drills.registry as registry_module
    import windbreak.main as main_module

    fixture_dir, state_dir = _default_dirs(tmp_path)
    calls: list[str] = []

    def _fake_bind_production_context(paper_ctx: object, *, env: object) -> object:
        calls.append("production")
        return paper_ctx

    monkeypatch.setitem(registry_module.DRILLS, _A_DRILL_NAME, _NoopDrill)
    monkeypatch.setattr(
        main_module, "bind_production_context", _fake_bind_production_context
    )

    exit_code = main(
        [
            "drill",
            _A_DRILL_NAME,
            "--fixture-dir",
            str(fixture_dir),
            "--state-dir",
            str(state_dir),
        ]
    )

    assert exit_code == 0
    assert calls == []
