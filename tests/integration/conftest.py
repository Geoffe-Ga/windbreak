"""Shared fixtures for `tests/integration/test_paper_loop.py` (issue #48, RED).

`hedgekit.scheduler.loop` does not exist yet, so any fixture/test here that
actually calls `build_paper_deps`/`run_single_tick` fails at call time with
`ModuleNotFoundError: No module named 'hedgekit.scheduler'` -- the expected
Gate 1 RED state for issue #48. This module itself imports only already-shipped
machinery, so it collects cleanly.

Fixture-design notes (mirroring the precedents this suite draws on):

* `books_dir` reuses the shared `tests/fixtures/books/deep_walk` fixture
  (issue #19's own `MKT-DEEP` single-ticker book) rather than a new fixture
  directory -- no new large fixture is committed for this issue.
* `cassette_path` is an *empty* recorded cassette (`{}`), written fresh per
  test into `tmp_path` (never committed), mirroring
  `tests/forecast/conftest.py`'s record-then-replay-over-`tmp_path` pattern.
  It is empty because the offline research double this suite wires
  (`NullSearchTransport`, below) never finds a candidate URL to fetch, so
  `hedgekit.forecast.pipeline.collect_model_votes` -- the sole stage touching
  the LLM transport seam -- is never reached (the pipeline abstains on zero
  verified citations first, `ABSTENTION_NO_VERIFIED_CITATIONS`); an empty
  cassette therefore still proves "replay never touches the network" (a
  `CassetteMissError` would fail loudly if the pipeline ever *did* reach the
  transport unexpectedly).
* No fixed key material is exposed here at all: the fill-leg scenario in
  `test_paper_loop.py` mints its double's token against
  `deps.verification_key` itself (the ephemeral key `build_paper_deps`
  actually wired the gateway with), per the issue's own "genuinely minted
  token" instruction -- never a key this conftest independently invents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hedgekit.config.schema import CapitalConfig, HedgekitConfig, RiskConfig

#: The shared `deep_walk` books fixture (issue #19): sole ticker `MKT-DEEP`.
_BOOKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "books" / "deep_walk"

#: A fixed, non-advancing "current instant" every deps-builder call in this
#: suite agrees on, so two independently-built `PaperTickDeps` (over separate
#: ledger paths) observe an identical clock reading -- the load-bearing input
#: the determinism scenario's two-run comparison depends on.
FIXED_NOW_EPOCH_S = 1_735_000_000


class NullSearchTransport:
    """A `SearchTransport`/`FetchTransport` double finding nothing, ever.

    The offline default this suite wires into the forecast pipeline's
    research stage: no live network, and (per `bounded_web_research`'s own
    documented contract) zero candidates for a subquestion means zero
    citations gathered for it, never an `EgressDeniedError` or a live fetch.
    Also implements `fetch` (raising, never returning content) purely so this
    one double satisfies both structural transport protocols; `search`
    always returning `()` means `fetch` is never actually reached.
    """

    def search(self, query: str) -> tuple[str, ...]:
        """Return no candidate URLs, unconditionally.

        Args:
            query: The (unused) subquestion text.

        Returns:
            An empty tuple, always.
        """
        del query
        return ()

    def fetch(self, url: str) -> str:
        """Never actually called (see the class docstring); raises defensively.

        Args:
            url: The (unused) URL that would have been fetched.

        Raises:
            AssertionError: Always -- reaching this is itself a test bug.
        """
        raise AssertionError(
            f"NullSearchTransport.fetch unexpectedly called for {url!r}"
        )


@pytest.fixture
def books_dir() -> Path:
    """Return the shared `deep_walk` books-fixture directory."""
    return _BOOKS_DIR


@pytest.fixture
def cassette_path(tmp_path: Path) -> Path:
    """Provide an empty recorded-cassette file under `tmp_path`.

    See the module docstring's "cassette_path" note for why an empty
    (`{}`) cassette is the correct offline double here.
    """
    path = tmp_path / "cassette.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    """Provide a fresh, not-yet-created report output directory."""
    return tmp_path / "reports"


def ledger_path_for(tmp_path: Path, name: str = "ledger.db") -> Path:
    """Return a fresh ledger-database path under `tmp_path / name`.

    Args:
        tmp_path: The pytest-provided scratch directory.
        name: The ledger filename; distinct names let one test build two
            independent `PaperTickDeps` (e.g. for the two-run determinism
            comparison) without sharing a database file.

    Returns:
        The path a fresh `SqliteLedgerStore` should be opened at.
    """
    return tmp_path / name


@pytest.fixture
def paper_config() -> HedgekitConfig:
    """Provide a PAPER-ceilinged config with a permissive-but-real risk profile.

    `mode_ceiling="paper"` is the SPEC S16 token `Mode.from_config` maps to
    `Mode.PAPER`. The risk thresholds are left at their SPEC §16 defaults
    (`RiskConfig()`) rather than artificially loosened: this suite's
    real-kernel-tick scenario does not depend on the selector actually
    emitting an intent (see `test_paper_loop.py`'s module docstring for why
    that is a genuinely open, orthogonal economic-modeling question the
    stock forecast pipeline's fixed per-forecast research-cost amortization
    raises), so there is no reason to hand-tune the thresholds toward a
    particular outcome here.
    """
    return HedgekitConfig(
        mode_ceiling="paper",
        capital=CapitalConfig(floor_micros=0),
        risk=RiskConfig(),
    )


@pytest.fixture
def research_tools_factory(tmp_path: Path):
    """Provide a factory building an offline `ResearchTools` over `NullSearchTransport`.

    Deferred `hedgekit.forecast.sandbox` import (mirrors
    `tests/forecast/conftest.py::research_tools_factory`) so this conftest
    keeps collecting cleanly regardless of that package's own state.
    """

    def _build() -> object:
        from hedgekit.forecast.sandbox import build_research_tools

        cache_dir = tmp_path / "research-cache"
        return build_research_tools(
            allowed_hosts=frozenset({"research.local"}),
            cache_dir=cache_dir,
            search_transport=NullSearchTransport(),
            fetch_transport=NullSearchTransport(),  # never called; see module docstring
        )

    return _build


def read_event_type_payload_pairs(records: list[object]) -> list[tuple[str, dict]]:
    """Project ledger records into `(event_type, payload_data)` pairs.

    Strips every wall-clock/sequence/hash-chain field so two independently
    built ledgers (over separate paths, but identical inputs and clock) can
    be compared for *decision-content* determinism without requiring the
    underlying `SqliteLedgerStore`'s own `created_at` timestamps to agree
    byte-for-byte.

    Args:
        records: The `LedgerRecord` sequence from `store.read_all()`.

    Returns:
        One `(event_type, payload_data)` pair per record, in ledger order.
    """
    pairs = []
    for record in records:
        data = json.loads(record.payload_json)["data"]  # type: ignore[attr-defined]
        pairs.append((record.event_type, data))  # type: ignore[attr-defined]
    return pairs
