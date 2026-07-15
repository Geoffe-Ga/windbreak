"""Failing-first tests for `windbreak.reports.weekly` (issue #48, RED).

`windbreak/reports/` does not exist yet, so every import below fails
collection with `ModuleNotFoundError: No module named 'windbreak.reports'` --
the expected Gate 1 RED state for issue #48.

Pins:

- `write_weekly_stub(output_dir, *, today)` writes a dated
  `weekly-YYYY-MM-DD.md` file with section headers and "No data yet." bodies,
  creating `output_dir` if absent.
- `maybe_write_weekly(output_dir, *, today)` is idempotent per ISO week: two
  calls whose `today` falls in the same ISO calendar week write exactly one
  file; a `today` in a *different* ISO week writes a second, distinct file.

Issue #188 widens both functions' `body` parameter from `str | None` to
`str | Callable[[], str] | None`: a callable factory is invoked only on the
genuine write path (never when this ISO week's file already exists), so an
expensive body -- a whole-ledger evaluation/cost-meter fold -- is built only
when it will actually be persisted.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: A fixed Wednesday, so `+timedelta(days=n)` for small `n` stays in the same
#: ISO week (Mon-Sun) unless the test explicitly crosses a week boundary.
_A_WEDNESDAY = date(2026, 1, 7)


def test_write_weekly_stub_creates_output_dir_if_absent(tmp_path: Path) -> None:
    """`write_weekly_stub` creates `output_dir` when it does not yet exist."""
    from windbreak.reports.weekly import write_weekly_stub

    output_dir = tmp_path / "reports"
    assert not output_dir.exists()

    write_weekly_stub(output_dir, today=_A_WEDNESDAY)

    assert output_dir.is_dir()


def test_write_weekly_stub_filename_is_dated_weekly_markdown(tmp_path: Path) -> None:
    """The written file is named `weekly-YYYY-MM-DD.md`, dated `today`."""
    from windbreak.reports.weekly import write_weekly_stub

    path = write_weekly_stub(tmp_path, today=_A_WEDNESDAY)

    assert path.name == "weekly-2026-01-07.md"
    assert path.parent == tmp_path
    assert path.exists()


def test_write_weekly_stub_body_has_section_headers_and_no_data_yet(
    tmp_path: Path,
) -> None:
    """The written body carries markdown section headers, each with a
    "No data yet." placeholder -- there is no real data to report yet.
    """
    from windbreak.reports.weekly import write_weekly_stub

    path = write_weekly_stub(tmp_path, today=_A_WEDNESDAY)

    body = path.read_text(encoding="utf-8")
    assert body.count("#") >= 1
    assert "No data yet." in body


def test_write_weekly_stub_overwrites_on_a_repeated_call_for_the_same_date(
    tmp_path: Path,
) -> None:
    """Calling `write_weekly_stub` twice for the identical `today` is a plain
    overwrite (unconditional -- unlike `maybe_write_weekly`'s idempotence),
    never an error or a second file.
    """
    from windbreak.reports.weekly import write_weekly_stub

    first = write_weekly_stub(tmp_path, today=_A_WEDNESDAY)
    second = write_weekly_stub(tmp_path, today=_A_WEDNESDAY)

    assert first == second
    assert len(list(tmp_path.glob("weekly-*.md"))) == 1


def test_maybe_write_weekly_writes_exactly_one_file_per_iso_week(
    tmp_path: Path,
) -> None:
    """Two calls whose `today` falls in the same ISO week write one file."""
    from datetime import timedelta

    from windbreak.reports.weekly import maybe_write_weekly

    maybe_write_weekly(tmp_path, today=_A_WEDNESDAY)
    maybe_write_weekly(
        tmp_path, today=_A_WEDNESDAY + timedelta(days=2)
    )  # same ISO week

    assert len(list(tmp_path.glob("weekly-*.md"))) == 1


def test_maybe_write_weekly_writes_a_second_file_for_a_later_iso_week(
    tmp_path: Path,
) -> None:
    """A `today` one ISO week later writes a second, distinct file."""
    from datetime import timedelta

    from windbreak.reports.weekly import maybe_write_weekly

    maybe_write_weekly(tmp_path, today=_A_WEDNESDAY)
    maybe_write_weekly(tmp_path, today=_A_WEDNESDAY + timedelta(days=7))

    assert len(list(tmp_path.glob("weekly-*.md"))) == 2


def test_maybe_write_weekly_returns_the_written_or_existing_path(
    tmp_path: Path,
) -> None:
    """`maybe_write_weekly` returns a real, existing path either way (freshly
    written, or the already-written-this-week file left untouched).
    """
    from windbreak.reports.weekly import maybe_write_weekly

    first_path = maybe_write_weekly(tmp_path, today=_A_WEDNESDAY)
    second_path = maybe_write_weekly(tmp_path, today=_A_WEDNESDAY)

    assert first_path.exists()
    assert second_path.exists()
    assert first_path == second_path


# ---------------------------------------------------------------------------
# Callable `body` factory (issue #188): built only on the genuine write path.
# ---------------------------------------------------------------------------


def test_write_weekly_stub_invokes_a_callable_body_exactly_once(tmp_path: Path) -> None:
    """`write_weekly_stub` (the unconditional-write primitive) accepts a
    zero-arg callable `body` and invokes it exactly once, writing its
    returned string -- never the callable object itself.
    """
    from windbreak.reports.weekly import write_weekly_stub

    calls: list[int] = []

    def _factory() -> str:
        calls.append(1)
        return "# stub body\n"

    path = write_weekly_stub(tmp_path, today=_A_WEDNESDAY, body=_factory)

    assert len(calls) == 1
    assert path.read_text(encoding="utf-8") == "# stub body\n"


def test_maybe_write_weekly_invokes_a_callable_body_exactly_once_on_a_genuine_write(
    tmp_path: Path,
) -> None:
    """A callable `body` factory is invoked exactly once when
    `maybe_write_weekly` actually writes this ISO week's first file.
    """
    from windbreak.reports.weekly import maybe_write_weekly

    calls: list[int] = []

    def _factory() -> str:
        calls.append(1)
        return "# built body\n"

    path = maybe_write_weekly(tmp_path, today=_A_WEDNESDAY, body=_factory)

    assert len(calls) == 1
    assert path.read_text(encoding="utf-8") == "# built body\n"


def test_maybe_write_weekly_never_invokes_a_callable_body_when_this_weeks_file_exists(
    tmp_path: Path,
) -> None:
    """When this ISO week's file already exists, a callable `body` factory is
    NOT invoked at all -- the entire point of deferring the body to a
    callable is to skip paying for an expensive fold on an idempotent no-op
    tick.

    The first call also passes a callable (not a plain `str`), so this test
    starts on the same genuine-write path as the test above and fails there
    today (`TypeError`) rather than passing trivially: today's
    `maybe_write_weekly` already skips touching `body` at all on its
    already-exists short-circuit, so a version of this test whose *first*
    call used a plain `str` body would pass before callable support exists
    at all -- exactly the "passes before the code exists" trap this suite
    must avoid.
    """
    from datetime import timedelta

    from windbreak.reports.weekly import maybe_write_weekly

    first_calls: list[int] = []

    def _first_factory() -> str:
        first_calls.append(1)
        return "# first body\n"

    first_path = maybe_write_weekly(tmp_path, today=_A_WEDNESDAY, body=_first_factory)
    assert len(first_calls) == 1
    assert first_path.read_text(encoding="utf-8") == "# first body\n"

    second_calls: list[int] = []

    def _second_factory() -> str:
        second_calls.append(1)
        return "# should never be built\n"

    second_path = maybe_write_weekly(
        tmp_path, today=_A_WEDNESDAY + timedelta(days=1), body=_second_factory
    )

    assert second_calls == []
    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") == "# first body\n"
