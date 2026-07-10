"""Characterization tests for scripts/analyze_mutations.py (issue #68).

`scripts/analyze_mutations.py::analyze_cache` reads a mutmut-shaped sqlite
cache and prints human-readable statistics: a total mutant count, a
per-status breakdown, a computed mutation score, and (when there are
survived mutants) a "most survived" file ranking and a sample listing.

These tests pin the CURRENT, observable behavior of `analyze_cache` --
its printed output for a known, hand-computed cache -- so the upcoming
SQL-literal refactor (which changes how the query strings are built but
must not change what they select) has a green baseline to hold. Nothing
here asserts on the internal query-string construction; every assertion
is against `capsys`-captured stdout or an observable `SystemExit`, so the
tests stay valid regardless of how the SQL is assembled internally.

Schema built for these tests mirrors the three tables the module's SQL
joins depend on: `SourceFile(id, filename)`, `Line(id, sourcefile,
line_number)`, `Mutant(id, line, status)`, joined via `m.line = l.id AND
l.sourcefile = sf.id`.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "analyze_mutations.py"

#: The two source files seeded into the fixture cache. Chosen so that
#: alphabetical order ("bar" < "foo") differs from "most survived" order
#: (foo has more survived mutants than bar), letting tests distinguish the
#: two independent orderings `analyze_cache` uses.
_FOO_FILE = "windbreak/foo.py"
_BAR_FILE = "windbreak/bar.py"

#: (filename, line_number, status) triples seeded into the fixture cache.
#:
#: windbreak/foo.py: 5 mutants across 3 lines --
#:   line 10: 1 ok_killed, 1 bad_survived
#:   line 20: 1 bad_survived, 1 untested
#:   line 30: 1 ok_suspicious
#: windbreak/bar.py: 3 mutants across 2 lines --
#:   line 5:  1 ok_killed, 1 bad_timeout
#:   line 15: 1 bad_survived
#:
#: Totals: 8 mutants; ok_killed=2, bad_survived=3, untested=1,
#: ok_suspicious=1, bad_timeout=1. tested_total = 8 - 1 = 7.
#: score = 2 / 7 * 100 = 28.571...% -> "28.6%".
_SEED_MUTANTS: tuple[tuple[str, int, str], ...] = (
    (_FOO_FILE, 10, "ok_killed"),
    (_FOO_FILE, 10, "bad_survived"),
    (_FOO_FILE, 20, "bad_survived"),
    (_FOO_FILE, 20, "untested"),
    (_FOO_FILE, 30, "ok_suspicious"),
    (_BAR_FILE, 5, "ok_killed"),
    (_BAR_FILE, 5, "bad_timeout"),
    (_BAR_FILE, 15, "bad_survived"),
)


def _load_analyze_mutations_module() -> types.ModuleType:
    """Load `scripts/analyze_mutations.py` by path as a fresh module.

    Returns:
        The executed `analyze_mutations` module, exposing `analyze_cache`.
    """
    spec = importlib.util.spec_from_file_location("analyze_mutations", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_cache_db(db_path: Path, mutants: tuple[tuple[str, int, str], ...]) -> None:
    """Create a minimal mutmut-shaped sqlite cache at `db_path`.

    Builds the three tables `analyze_cache`'s SQL joins depend on --
    `SourceFile`, `Line`, `Mutant` -- from a flat list of (filename,
    line_number, status) triples. Filenames and (filename, line_number)
    pairs are deduplicated automatically, mirroring how a real mutmut
    cache reuses one `SourceFile`/`Line` row per distinct file/line across
    its many mutants.

    Args:
        db_path: Where to create the sqlite database file.
        mutants: Each entry is one mutant: its source filename, the
            1-based line number it mutates, and its mutmut status string.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE SourceFile (id INTEGER PRIMARY KEY, filename TEXT)")
        conn.execute(
            "CREATE TABLE Line (id INTEGER PRIMARY KEY, sourcefile INTEGER, "
            "line_number INTEGER)"
        )
        conn.execute(
            "CREATE TABLE Mutant (id INTEGER PRIMARY KEY, line INTEGER, status TEXT)"
        )

        file_ids: dict[str, int] = {}
        line_ids: dict[tuple[str, int], int] = {}

        for filename, line_number, status in mutants:
            if filename not in file_ids:
                cursor = conn.execute(
                    "INSERT INTO SourceFile (filename) VALUES (?)", (filename,)
                )
                new_file_id = cursor.lastrowid
                assert new_file_id is not None
                file_ids[filename] = new_file_id

            line_key = (filename, line_number)
            if line_key not in line_ids:
                cursor = conn.execute(
                    "INSERT INTO Line (sourcefile, line_number) VALUES (?, ?)",
                    (file_ids[filename], line_number),
                )
                new_line_id = cursor.lastrowid
                assert new_line_id is not None
                line_ids[line_key] = new_line_id

            conn.execute(
                "INSERT INTO Mutant (line, status) VALUES (?, ?)",
                (line_ids[line_key], status),
            )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def analyze_mutations() -> types.ModuleType:
    """Provide a freshly loaded `analyze_mutations` module."""
    return _load_analyze_mutations_module()


@pytest.fixture
def seeded_cache(tmp_path: Path) -> Path:
    """Provide a mutmut-shaped sqlite cache seeded with `_SEED_MUTANTS`."""
    db_path = tmp_path / ".mutmut-cache"
    _build_cache_db(db_path, _SEED_MUTANTS)
    return db_path


# --- analyze_cache, unfiltered -----------------------------------------------------


def test_analyze_cache_unfiltered_prints_total_mutant_count(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The total mutant count across both seeded files is 8."""
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert re.search(r"Total mutants:\s*8\b", output)


def test_analyze_cache_unfiltered_prints_per_status_counts(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every seeded status is counted correctly: 2/3/1/1/1."""
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert re.search(r"ok_killed:\s*2\b", output)
    assert re.search(r"bad_survived:\s*3\b", output)
    assert re.search(r"untested:\s*1\b", output)
    assert re.search(r"ok_suspicious:\s*1\b", output)
    assert re.search(r"bad_timeout:\s*1\b", output)


def test_analyze_cache_unfiltered_computes_mutation_score_excluding_untested(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Score = killed / (total - untested) * 100 = 2 / 7 * 100 = 28.6%.

    Pins the specific business rule that untested mutants are excluded
    from the score denominator (a mutant mutmut never ran cannot count as
    a kill or a survival).
    """
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert re.search(r"Mutation Score:\s*28\.6%", output)
    assert re.search(r"Required:\s*80%", output)


def test_analyze_cache_unfiltered_prints_breakdown_percentages_of_tested(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breakdown percentages are relative to the 7 tested (non-untested) mutants."""
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert re.search(r"Killed:\s*2\s*\(28\.6% of tested\)", output)
    assert re.search(r"Survived:\s*3\s*\(42\.9% of tested\)", output)
    assert re.search(r"Suspicious:\s*1\s*\(14\.3%\)", output)
    assert re.search(r"Timeout:\s*1\s*\(14\.3%\)", output)
    assert re.search(r"Untested:\s*1\b", output)


def test_analyze_cache_unfiltered_warns_when_below_minimum_score(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """28.6% is below the 80% minimum, so the gap-to-target warning prints.

    gap = int(0.80 * 7 - 2) = int(5.6 - 2) = int(3.6) = 3.
    """
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert "Need to kill 3 more mutants" in output
    assert "reach 80%" in output


def test_analyze_cache_unfiltered_ranks_files_by_survived_count_descending(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """foo.py (2 survived) ranks ahead of bar.py (1 survived), unambiguously.

    The two files have different survived counts (2 vs 1), so the ranking
    is deterministic regardless of any tie-breaking behavior in the
    underlying `ORDER BY count DESC` query.
    """
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert "Files with Most Survived Mutants" in output
    ranking_section = output.split("Files with Most Survived Mutants")[1]
    foo_index = ranking_section.index(_FOO_FILE)
    bar_index = ranking_section.index(_BAR_FILE)
    assert foo_index < bar_index


def test_analyze_cache_unfiltered_samples_survived_mutants_ordered_by_filename(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sample section lists survived mutants ordered by filename, then line.

    Alphabetically, "windbreak/bar.py" precedes "windbreak/foo.py", which
    is the opposite of the "most survived" ranking order asserted above --
    confirming the two sections use genuinely independent sort keys.
    """
    analyze_mutations.analyze_cache(seeded_cache)

    output = capsys.readouterr().out

    assert "Sample of survived mutants" in output
    sample_section = output.split("Sample of survived mutants")[1]
    assert re.search(rf"{re.escape(_BAR_FILE)}:15\b", sample_section)
    assert re.search(rf"{re.escape(_FOO_FILE)}:10\b", sample_section)
    assert re.search(rf"{re.escape(_FOO_FILE)}:20\b", sample_section)
    bar_index = sample_section.index(_BAR_FILE)
    foo_index = sample_section.index(_FOO_FILE)
    assert bar_index < foo_index


# --- analyze_cache, filtered --------------------------------------------------------


def test_analyze_cache_filtered_restricts_totals_to_matching_file(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filtering to "bar.py" restricts every count to bar.py's 3 mutants.

    bar.py contributes 1 ok_killed, 1 bad_timeout, 1 bad_survived, and no
    untested/ok_suspicious mutants at all -- so those two statuses must be
    entirely absent from the filtered output, and foo.py must not appear.
    """
    analyze_mutations.analyze_cache(seeded_cache, filter_file="bar.py")

    output = capsys.readouterr().out

    assert "filtered: bar.py" in output
    assert re.search(r"Total mutants:\s*3\b", output)
    assert re.search(r"ok_killed:\s*1\b", output)
    assert re.search(r"bad_timeout:\s*1\b", output)
    assert re.search(r"bad_survived:\s*1\b", output)
    assert "ok_suspicious" not in output
    assert re.search(r"Untested:\s*0\b", output)
    assert _FOO_FILE not in output


def test_analyze_cache_filtered_computes_score_over_filtered_subset_only(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filtered score = 1 killed / 3 tested (0 untested) * 100 = 33.3%."""
    analyze_mutations.analyze_cache(seeded_cache, filter_file="bar.py")

    output = capsys.readouterr().out

    assert re.search(r"Mutation Score:\s*33\.3%", output)


def test_analyze_cache_filtered_by_full_relative_path_matches_same_subset(
    analyze_mutations: types.ModuleType,
    seeded_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filtering by the full stored filename produces the same restricted result.

    Pins the `LIKE '%<filter>'` suffix-match semantics: an exact full-path
    filter is just as valid as a bare basename filter.
    """
    analyze_mutations.analyze_cache(seeded_cache, filter_file=_BAR_FILE)

    output = capsys.readouterr().out

    assert re.search(r"Total mutants:\s*3\b", output)
    assert _FOO_FILE not in output


# --- analyze_cache, missing cache file -----------------------------------------------


def test_analyze_cache_missing_file_exits_with_status_one(
    analyze_mutations: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A nonexistent cache path prints an error to stderr and exits(1)."""
    missing_cache = tmp_path / "does-not-exist.mutmut-cache"

    with pytest.raises(SystemExit) as exc_info:
        analyze_mutations.analyze_cache(missing_cache)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert str(missing_cache) in captured.err
    assert "Cache file not found" in captured.err


def test_module_is_importable_and_exposes_minimum_mutation_score(
    analyze_mutations: types.ModuleType,
) -> None:
    """Sanity guard: the module loads and defines the 80% quality threshold.

    Guards against a future refactor accidentally renaming/removing the
    module-level `MINIMUM_MUTATION_SCORE` constant that every score
    comparison and warning message depends on.
    """
    assert analyze_mutations.MINIMUM_MUTATION_SCORE == 80


def test_script_source_still_defines_analyze_cache_with_documented_signature() -> None:
    """Durable guard: `analyze_cache`'s public signature name/order is stable.

    Reads the script's source text directly (independent of any import
    mechanics) so this assertion is immune to whatever `sys.modules`
    tricks the fixture above uses.
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert re.search(
        r"def analyze_cache\(\s*cache_path:\s*Path,\s*top_files:\s*int\s*=\s*20,"
        r"\s*filter_file:\s*str\s*\|\s*None\s*=\s*None,?\s*\)\s*->\s*None:",
        source,
    )
    assert sys.version_info >= (3, 10), "PEP 604 `str | None` requires Python 3.10+"
