"""Gate 1 RED tests for issue #47's correlation-bucket tagging (SPEC S9.9).

`hedgekit.selector.correlation` does not exist yet, so every test below fails
collection with ``ModuleNotFoundError: No module named
'hedgekit.selector.correlation'`` -- the expected Gate 1 RED state for this
issue's new tag data model, seed taxonomy, and bucket-exposure aggregation.
Once that module lands, the ``select()``-level tests further down still fail
on ``TypeError: __init__() got an unexpected keyword argument 'bucket_peers'``
until :class:`~hedgekit.selector.types.SelectorInputs` grows its new
``bucket_peers`` field and its ``correlation_tags`` field is retyped from
``tuple[str, ...]`` to ``tuple[CorrelationTag, ...]`` -- both are the correct
RED reason (missing symbols/fields for not-yet-wired behavior), never a typo.

This module pins the design contract the chief architect handed off:

    * :class:`~hedgekit.selector.correlation.CorrelationTag` -- a frozen,
      slotted ``(bucket_id, source, tagged_at)`` triple. ``source`` must be
      ``"llm"`` or ``"human"``; ``bucket_id`` must be one of the seven fixed
      seed-taxonomy ids or a ``geopolitics-<region>`` id with a non-empty
      region suffix -- anything else raises ``ValueError`` at construction.
    * :func:`~hedgekit.selector.correlation.effective_buckets` -- resolves a
      target market's own tags into its *effective* bucket ids: any human tag
      present supersedes every LLM tag (the LLM tags are still retained in the
      input tuple for the ledger, just excluded from the effective result).
    * :func:`~hedgekit.selector.correlation.aggregate_bucket_exposure` -- sums
      peer exposure per matching effective bucket and returns the maximum
      sum and the bucket id achieving it (lexicographically-smallest bucket
      id on ties), or ``(0, None)`` when the target has no effective buckets
      or no peer matches any of them.
    * ``select()`` wiring -- before clipping, the real bucket exposure is
      aggregated from ``inputs.correlation_tags`` (the target's own tags) plus
      ``inputs.bucket_peers``, overriding ``positions.bucket_exposure``, and
      the per-bucket cap is named ``"per_bucket:<bucket-id>"`` in the pinned
      sizing reason when it binds (superseding the bare ``"per_bucket"`` name
      pre-#47), while ``clip_to_caps``'s *default* ``bucket_cap_name`` keyword
      stays the bare ``"per_bucket"`` so no pre-#47 call site or golden
      changes shape.

Every expected number in the ``select()``-level tests is hand-derived in a
docstring directly above the assertion it backs, reusing the exact fused-
division formulas ``tests/selector/test_sizing_examples.py`` pins for
``kelly_size``/``clip_to_caps`` -- this module only reproduces that same
arithmetic by hand, in comments, to pin the expected results; it performs no
division itself (no float, no bare ``/``/``//`` anywhere, matching every other
selector test module).

Divergence note for the implementation specialist (see the handoff): today's
``clip_to_caps`` unconditionally renames the binding cap to
``"exchange_min_order"`` whenever the final, lot-floored size drops below one
whole contract -- *even when* a real notional cap (not merely a naturally
sub-lot raw size) is what drove it to exactly zero. The "third intent
rejected" scenario below requires the *cap's own* name
(``"per_bucket:fed-policy"``) to survive at ``final_centis=0``, so
``clip_to_caps`` must distinguish "a real cap forced zero" from "the raw size
was simply sub-lot to begin with" (the existing ``exchange_min_order`` test in
``test_sizing_examples.py`` covers the latter, unclipped-continuous-cap case
and must keep working unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from hedgekit.config.schema import RiskConfig
from hedgekit.connector.fees import FeeModel
from hedgekit.connector.models import OrderBookLevel, OrderBookSnapshot
from hedgekit.forecast.records import Citation, ForecastRecord
from hedgekit.numeric import ContractCentis, MoneyMicros, PricePips
from hedgekit.selector import SelectorInputs, select
from hedgekit.selector.correlation import (
    BUCKET_AI_REGULATION,
    BUCKET_COMPANY_SPECIFIC,
    BUCKET_FED_POLICY,
    BUCKET_INFLATION,
    BUCKET_LEGAL_CASE,
    BUCKET_US_ELECTION,
    BUCKET_WEATHER,
    GEOPOLITICS_PREFIX,
    SEED_BUCKETS,
    BucketExposureEntry,
    CorrelationTag,
    TagSource,
    aggregate_bucket_exposure,
    effective_buckets,
)
from hedgekit.selector.sizing import CapClipResult, clip_to_caps
from hedgekit.selector.types import (
    FeeModelInput,
    PositionReadModelInput,
    RiskConfigInput,
    SlippageModelInput,
)

#: A fixed reference instant every timestamp in this module is pinned to.
_INSTANT = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

_CITATION = Citation(
    url="https://example.com/correlation-bucket-example",
    content_hash="sha256:correlation-bucket-example-citation",
    quoted_text="Example quoted text supporting the correlation-bucket forecast.",
    publication_date=None,
    source_type="news_article",
)


# =============================================================================
# effective_buckets: LLM/human override resolution
# =============================================================================


def test_effective_buckets_llm_only_passthrough() -> None:
    """All-LLM tags pass through untouched (deduplicated, sorted) when no
    human tag exists.
    """
    tags = (
        CorrelationTag(bucket_id=BUCKET_INFLATION, source="llm", tagged_at=_INSTANT),
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
    )

    assert effective_buckets(tags) == (BUCKET_FED_POLICY, BUCKET_INFLATION)


def test_effective_buckets_human_only_passthrough() -> None:
    """All-human tags pass through untouched (deduplicated, sorted)."""
    tags = (
        CorrelationTag(bucket_id=BUCKET_WEATHER, source="human", tagged_at=_INSTANT),
        CorrelationTag(
            bucket_id=BUCKET_AI_REGULATION, source="human", tagged_at=_INSTANT
        ),
    )

    assert effective_buckets(tags) == (BUCKET_AI_REGULATION, BUCKET_WEATHER)


def test_effective_buckets_mixed_human_supersedes_llm_but_llm_tag_retained() -> None:
    """Mixed LLM+human tags: any human tag switches the effective buckets to
    the human tags' bucket ids ONLY -- the LLM tag's bucket is absent from the
    effective result, yet the original `CorrelationTag` for it is still
    present in the input tuple, preserving the full tagging ledger history.
    """
    llm_tag = CorrelationTag(bucket_id=BUCKET_WEATHER, source="llm", tagged_at=_INSTANT)
    human_tag = CorrelationTag(
        bucket_id=BUCKET_FED_POLICY, source="human", tagged_at=_INSTANT
    )
    tags = (llm_tag, human_tag)

    result = effective_buckets(tags)

    assert result == (BUCKET_FED_POLICY,)
    assert BUCKET_WEATHER not in result
    assert llm_tag in tags


def test_effective_buckets_duplicate_bucket_ids_collapse() -> None:
    """Two LLM tags naming the same bucket collapse to one effective id."""
    tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
    )

    assert effective_buckets(tags) == (BUCKET_FED_POLICY,)


def test_effective_buckets_lexicographic_ordering_of_multiple_buckets() -> None:
    """Three distinct effective buckets come back sorted lexicographically
    (`ai-regulation` < `us-election` < `weather`), regardless of input order.
    """
    tags = (
        CorrelationTag(bucket_id=BUCKET_WEATHER, source="llm", tagged_at=_INSTANT),
        CorrelationTag(
            bucket_id=BUCKET_AI_REGULATION, source="llm", tagged_at=_INSTANT
        ),
        CorrelationTag(bucket_id=BUCKET_US_ELECTION, source="llm", tagged_at=_INSTANT),
    )

    assert effective_buckets(tags) == (
        BUCKET_AI_REGULATION,
        BUCKET_US_ELECTION,
        BUCKET_WEATHER,
    )


def test_effective_buckets_empty_tuple_returns_empty_tuple() -> None:
    """An untagged market's empty tag tuple resolves to `()`."""
    assert effective_buckets(()) == ()


# =============================================================================
# CorrelationTag: construction validation
# =============================================================================


@pytest.mark.parametrize("bucket_id", sorted(SEED_BUCKETS))
def test_correlation_tag_accepts_every_seed_taxonomy_bucket_id(bucket_id: str) -> None:
    """Every one of the seven fixed seed-taxonomy ids constructs cleanly."""
    tag = CorrelationTag(bucket_id=bucket_id, source="llm", tagged_at=_INSTANT)

    assert tag.bucket_id == bucket_id


def test_correlation_tag_accepts_geopolitics_mideast() -> None:
    """A `geopolitics-<region>` id with a non-empty region constructs cleanly."""
    tag = CorrelationTag(
        bucket_id=f"{GEOPOLITICS_PREFIX}mideast", source="llm", tagged_at=_INSTANT
    )

    assert tag.bucket_id == "geopolitics-mideast"


def test_correlation_tag_accepts_geopolitics_taiwan() -> None:
    """A second, distinct `geopolitics-<region>` id also constructs cleanly."""
    tag = CorrelationTag(
        bucket_id=f"{GEOPOLITICS_PREFIX}taiwan", source="human", tagged_at=_INSTANT
    )

    assert tag.bucket_id == "geopolitics-taiwan"


def test_correlation_tag_rejects_bare_geopolitics_prefix_with_empty_region() -> None:
    """A bare `"geopolitics-"` id (empty region suffix) is not a valid bucket
    id and must raise `ValueError` at construction.
    """
    with pytest.raises(ValueError, match="bucket_id"):
        CorrelationTag(bucket_id=GEOPOLITICS_PREFIX, source="llm", tagged_at=_INSTANT)


def test_correlation_tag_rejects_a_non_taxonomy_bucket_id() -> None:
    """An id outside both the seed set and the `geopolitics-` pattern (e.g.
    the truncated `"fed"`) raises `ValueError`.
    """
    with pytest.raises(ValueError, match="bucket_id"):
        CorrelationTag(bucket_id="fed", source="llm", tagged_at=_INSTANT)


def test_correlation_tag_rejects_an_invalid_source() -> None:
    """A `source` outside `{"llm", "human"}` raises `ValueError`.

    `cast` (not `# type: ignore`) supplies the deliberately-invalid literal:
    this models untrusted, dynamically-arrived tagging data (e.g. deserialized
    from an upstream LLM-tagging payload) that the runtime validation, not the
    type checker, must catch.
    """
    with pytest.raises(ValueError, match="source"):
        CorrelationTag(
            bucket_id=BUCKET_FED_POLICY,
            source=cast("TagSource", "bot"),
            tagged_at=_INSTANT,
        )


# =============================================================================
# aggregate_bucket_exposure: region-parameterized bucket isolation
# =============================================================================


def test_aggregate_distinct_geopolitics_regions_do_not_cross_contaminate() -> None:
    """A peer tagged `geopolitics-mideast` contributes exactly zero to a
    `geopolitics-taiwan` target's aggregate: the two region-parameterized
    buckets are distinct ids, never merged.
    """
    target_buckets = effective_buckets(
        (
            CorrelationTag(
                bucket_id=f"{GEOPOLITICS_PREFIX}taiwan",
                source="llm",
                tagged_at=_INSTANT,
            ),
        )
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-MIDEAST",
        exposure_micros=MoneyMicros(500_000_000),
        tags=(
            CorrelationTag(
                bucket_id=f"{GEOPOLITICS_PREFIX}mideast",
                source="llm",
                tagged_at=_INSTANT,
            ),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(target_buckets, (peer,))

    assert total == 0
    assert bucket_id is None


def test_aggregate_bucket_exposure_matching_geopolitics_region_contributes() -> None:
    """A peer tagged the SAME `geopolitics-taiwan` region contributes its full
    exposure to the target's aggregate.
    """
    target_buckets = effective_buckets(
        (
            CorrelationTag(
                bucket_id=f"{GEOPOLITICS_PREFIX}taiwan",
                source="llm",
                tagged_at=_INSTANT,
            ),
        )
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-TAIWAN",
        exposure_micros=MoneyMicros(500_000_000),
        tags=(
            CorrelationTag(
                bucket_id=f"{GEOPOLITICS_PREFIX}taiwan",
                source="llm",
                tagged_at=_INSTANT,
            ),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(target_buckets, (peer,))

    assert total == 500_000_000
    assert bucket_id == "geopolitics-taiwan"


# =============================================================================
# aggregate_bucket_exposure: sum / max / tie-break / empty cases
# =============================================================================


def test_aggregate_bucket_exposure_single_bucket_sums_only_matching_peers() -> None:
    """A single-bucket target sums exactly the peers whose effective bucket
    matches, ignoring a peer tagged into an unrelated bucket entirely.
    """
    target_buckets = (BUCKET_FED_POLICY,)
    matching_peer_1 = BucketExposureEntry(
        market_ticker="PEER-1",
        exposure_micros=MoneyMicros(100_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    matching_peer_2 = BucketExposureEntry(
        market_ticker="PEER-2",
        exposure_micros=MoneyMicros(250_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    non_matching_peer = BucketExposureEntry(
        market_ticker="PEER-3",
        exposure_micros=MoneyMicros(999_000_000),
        tags=(
            CorrelationTag(bucket_id=BUCKET_WEATHER, source="llm", tagged_at=_INSTANT),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(
        target_buckets, (matching_peer_1, matching_peer_2, non_matching_peer)
    )

    assert total == 350_000_000
    assert bucket_id == BUCKET_FED_POLICY


def test_aggregate_bucket_exposure_multi_bucket_returns_the_max_bucket_and_its_id() -> (
    None
):
    """A multi-bucket target returns the maximum-exposure bucket's sum and id
    -- `inflation`'s 500_000_000 dominates `fed-policy`'s 100_000_000.
    """
    target_buckets = (BUCKET_FED_POLICY, BUCKET_INFLATION)
    fed_peer = BucketExposureEntry(
        market_ticker="PEER-FED",
        exposure_micros=MoneyMicros(100_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    inflation_peer = BucketExposureEntry(
        market_ticker="PEER-INFLATION",
        exposure_micros=MoneyMicros(500_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_INFLATION, source="llm", tagged_at=_INSTANT
            ),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(
        target_buckets, (fed_peer, inflation_peer)
    )

    assert total == 500_000_000
    assert bucket_id == BUCKET_INFLATION


def test_aggregate_bucket_exposure_lexicographic_tie_break_on_equal_sums() -> None:
    """Two buckets tie on exposure (300_000_000 each) -- the
    lexicographically-smallest bucket id wins the tie (`ai-regulation` <
    `us-election`).
    """
    target_buckets = (BUCKET_US_ELECTION, BUCKET_AI_REGULATION)
    election_peer = BucketExposureEntry(
        market_ticker="PEER-ELECTION",
        exposure_micros=MoneyMicros(300_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_US_ELECTION, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    ai_peer = BucketExposureEntry(
        market_ticker="PEER-AI",
        exposure_micros=MoneyMicros(300_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_AI_REGULATION, source="llm", tagged_at=_INSTANT
            ),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(
        target_buckets, (election_peer, ai_peer)
    )

    assert total == 300_000_000
    assert bucket_id == BUCKET_AI_REGULATION


def test_aggregate_bucket_exposure_untagged_target_returns_zero_and_none() -> None:
    """An untagged target (no effective buckets) aggregates to `(0, None)`
    regardless of how much peer exposure exists elsewhere.
    """
    peer = BucketExposureEntry(
        market_ticker="PEER-1",
        exposure_micros=MoneyMicros(999_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_LEGAL_CASE, source="llm", tagged_at=_INSTANT
            ),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure((), (peer,))

    assert total == 0
    assert bucket_id is None


def test_aggregate_bucket_exposure_no_matching_peers_returns_zero_and_none() -> None:
    """A tagged target with zero matching peers aggregates to `(0, None)`."""
    target_buckets = (BUCKET_COMPANY_SPECIFIC,)
    peer = BucketExposureEntry(
        market_ticker="PEER-1",
        exposure_micros=MoneyMicros(999_000_000),
        tags=(
            CorrelationTag(bucket_id=BUCKET_WEATHER, source="llm", tagged_at=_INSTANT),
        ),
    )

    total, bucket_id = aggregate_bucket_exposure(target_buckets, (peer,))

    assert total == 0
    assert bucket_id is None


# =============================================================================
# select()-level golden-harness scenarios: shared fixture setup
#
# equity_micros = 10_000_000_000 ($10,000); max_pos_bucket_pct_ppm = 100_000
# (the schema default, 10%) -> bucket ceiling = divide(10_000_000_000*100_000,
# 1_000_000, floor) = 1_000_000_000 micros ($1,000), exact (no remainder).
#
# A single, deep 5_000-pip ask (10_000_000 centis deep, far beyond anything
# sized below) gives exactly one marginal price for every fill in this
# section: executable_price_ppm = 5_000*100 = 500_000 ppm-of-$1, the "cap
# reference price" every notional cap clips against (see
# `hedgekit.selector._cap_reference_price_ppm`).
#
# `max_pos_market_pct_ppm` / `max_pos_event_pct_ppm` are widened to 1_000_000
# ppm (100% of equity) and `max_notional_per_day_micros` to $1,000,000 so
# every OTHER notional cap's headroom (>= 200,000,000 centis at this price)
# dwarfs anything sized below -- only `per_bucket` can ever bind.
#
# Kelly raw size, hand-derived via the pinned fused-division formula
# (`net_edge_ppm=200_000`, `min_net_edge_ppm=30_000`,
# `executable_price_ppm=500_000`, `kelly_fraction_ppm=100_000` [schema
# default], `dispersion_scale_ppm=1_000_000` [zero vote dispersion -> full
# scale], `above_floor_capital_micros=12_500_000_000`):
#
#     stake_numerator = 12_500_000_000 * 200_000 * 100_000 * 1_000_000
#                      = 250_000_000_000_000_000_000_000_000  (2.5e26)
#     stake_denominator = (1_000_000-500_000) * 10**12 = 5e17
#     stake_micros = floor(2.5e26 / 5e17) = 500_000_000  (exact: 2.5/5 * 1e9)
#     size_centis = floor(500_000_000*100 / 500_000)
#                 = floor(50_000_000_000 / 500_000) = 100_000  (exact)
#
# So raw = ContractCentis(100_000) in every scenario below; only the bucket
# cap's headroom differs, which is exactly the point of this shared setup.
# =============================================================================


def _positions(**overrides: object) -> PositionReadModelInput:
    """Build the shared-setup `PositionReadModelInput`.

    Args:
        **overrides: Field values overriding the defaults documented above.

    Returns:
        The constructed `PositionReadModelInput`. `bucket_exposure` is a
        placeholder here -- `select()` overrides it internally from
        `inputs.correlation_tags` + `inputs.bucket_peers` (SPEC S9.9).
    """
    defaults: dict[str, object] = {
        "snapshot_id": "positions-correlation-bucket-example",
        "equity_micros": MoneyMicros(10_000_000_000),
        "above_floor_capital_micros": MoneyMicros(12_500_000_000),
        "total_deploy_cap_micros": MoneyMicros(1_000_000_000_000),
        "market_exposure": MoneyMicros(0),
        "event_exposure": MoneyMicros(0),
        "bucket_exposure": MoneyMicros(0),
        "total_exposure": MoneyMicros(0),
        "notional_today": MoneyMicros(0),
    }
    defaults.update(overrides)
    return PositionReadModelInput(**defaults)


def _loose_risk_config(**overrides: object) -> RiskConfig:
    """Build a `RiskConfig` with every notional cap but `per_bucket` widened.

    Args:
        **overrides: Field values overriding the widened defaults below.

    Returns:
        The constructed `RiskConfig`.
    """
    defaults: dict[str, object] = {
        "max_pos_market_pct_ppm": 1_000_000,
        "max_pos_event_pct_ppm": 1_000_000,
        "max_notional_per_day_micros": 1_000_000_000_000,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _deep_book() -> OrderBookSnapshot:
    """Build the shared-setup order book: one deep 5_000-pip ask level.

    Returns:
        The constructed `OrderBookSnapshot`.
    """
    return OrderBookSnapshot(
        ticker="BUCKET-TICKER",
        yes_bids=(),
        yes_asks=(
            OrderBookLevel(price=PricePips(5_000), quantity=ContractCentis(10_000_000)),
        ),
        fetched_at=_INSTANT,
    )


def _bucket_forecast(**overrides: object) -> ForecastRecord:
    """Build the shared-setup `ForecastRecord`.

    probability_ppm=700_000 against the 500_000 ppm executable price gives a
    gross (and, with zero fee/slippage/research cost, net) edge of exactly
    200_000 ppm; ci=[600_000, 800_000] does not straddle 500_000, so
    `ci_straddles_executable_price` passes.

    Args:
        **overrides: Field values overriding the defaults below.

    Returns:
        The constructed, post-init-validated `ForecastRecord`.
    """
    defaults: dict[str, object] = {
        "forecast_id": "fc-bucket-0001",
        "market_ticker": "BUCKET-TICKER",
        "normalized_question_hash": "sha256:bucket-question",
        "probability_ppm": 700_000,
        "ci_low_ppm": 600_000,
        "ci_high_ppm": 800_000,
        "model_votes": (),
        "vote_dispersion_ppm": 0,
        "rationale_markdown": "n/a",
        "citations": (_CITATION,),
        "source_quality_notes": (),
        "research_cost_micros": 0,
        "triage_stage": "full",
        "created_at": _INSTANT,
        "forecast_horizon_hours": 48,
        "market_price_baseline_pips": 5_000,
        "baseline_quote_snapshot_id": "snap-bucket-0001",
        "coherence_group_sum_ppm": None,
        "coherence_flag": False,
        "abstention_reason": None,
        "eligible_for_live": True,
    }
    defaults.update(overrides)
    return ForecastRecord(**defaults)


def _bucket_inputs(
    *,
    correlation_tags: tuple[CorrelationTag, ...],
    bucket_peers: tuple[BucketExposureEntry, ...] = (),
) -> SelectorInputs:
    """Assemble the shared-setup `SelectorInputs` for one bucket scenario.

    Args:
        correlation_tags: The target market's own correlation tags.
        bucket_peers: The peer markets' bucket-exposure entries.

    Returns:
        The constructed `SelectorInputs`.
    """
    return SelectorInputs(
        forecast=_bucket_forecast(),
        calibration_map_version="calib-bucket-v1",
        order_book=_deep_book(),
        fee_model=FeeModelInput(
            model=FeeModel(
                schedule_id="bucket-fee-zero",
                maker_fee_ppm=0,
                taker_fee_ppm=0,
                settlement_fee_ppm=0,
            ),
            as_of=_INSTANT,
        ),
        slippage_model=SlippageModelInput(
            model_id="bucket-slippage-zero", per_contract_buffer_ppm=0
        ),
        positions=_positions(),
        risk_config=RiskConfigInput(
            config=_loose_risk_config(), config_hash="sha256:risk-bucket"
        ),
        correlation_tags=correlation_tags,
        bucket_peers=bucket_peers,
    )


# =============================================================================
# select()-level: first/second/third intent in a saturating bucket
# =============================================================================


def test_select_first_intent_with_no_peers_is_unclipped_by_the_bucket_cap() -> None:
    """No peers yet -> bucket exposure aggregates to 0 -> bucket headroom is
    the FULL ceiling.

    bucket cap = divide((1_000_000_000 - 0)*100, 500_000, floor)
               = divide(100_000_000_000, 500_000, floor) = 200_000 centis.
    raw (100_000, see the shared-setup derivation above) is well under
    200_000, and every other cap is widened past 100_000 too, so the raw size
    survives completely unclipped: `binding_cap=none`, one intent emitted at
    size 100_000.
    """
    target_tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
    )
    inputs = _bucket_inputs(correlation_tags=target_tags, bucket_peers=())

    decision = select(inputs)

    assert len(decision.intents) == 1
    assert decision.intents[0].size == ContractCentis(100_000)
    assert decision.reasons[-1] == (
        "sizing: raw_centis=100000 g_ppm=1000000 binding_cap=none final_centis=100000"
    )


def test_select_second_intent_is_clipped_by_the_saturating_peer_bucket_cap() -> None:
    """One `fed-policy` peer already carries 900_000_000 micros of exposure
    -> bucket headroom shrinks to 100_000_000.

    bucket cap = divide(100_000_000*100, 500_000, floor)
               = divide(10_000_000_000, 500_000, floor) = 20_000 centis.
    raw (100_000) exceeds this, so the emitted size clips to 20_000 (already
    an exact multiple of 100, so the exchange-min lot floor is a no-op):
    `binding_cap=per_bucket:fed-policy`. The post-intent bucket total --
    existing peer exposure plus this intent's own notional
    (`price.value * size.value` = 5_000*20_000 = 100_000_000, exact since a
    pips-times-centis product is exactly micros) -- lands exactly at, and
    never past, the 1_000_000_000 ceiling.
    """
    target_tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-FED-1",
        exposure_micros=MoneyMicros(900_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    inputs = _bucket_inputs(correlation_tags=target_tags, bucket_peers=(peer,))

    decision = select(inputs)

    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.size == ContractCentis(20_000)
    assert decision.reasons[-1] == (
        "sizing: raw_centis=100000 g_ppm=1000000 binding_cap=per_bucket:fed-policy "
        "final_centis=20000"
    )

    notional_added_micros = intent.price.value * intent.size.value
    bucket_total_after = 900_000_000 + notional_added_micros
    assert bucket_total_after == 1_000_000_000


def test_select_third_intent_is_rejected_when_bucket_headroom_is_fully_saturated() -> (
    None
):
    """A single `fed-policy` peer already carries the FULL 1_000_000_000
    ceiling -> bucket headroom is exactly 0 -> bucket cap is exactly 0.

    raw (100_000) clips all the way to 0: no intent is emitted, but the
    sizing reason still names the true binding cap (`per_bucket:fed-policy`),
    not a generic `exchange_min_order` -- see the module docstring's
    divergence note on why `clip_to_caps` must distinguish "a real cap forced
    zero" from "the raw size was simply sub-lot to begin with".
    """
    target_tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-FED-SATURATED",
        exposure_micros=MoneyMicros(1_000_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    inputs = _bucket_inputs(correlation_tags=target_tags, bucket_peers=(peer,))

    decision = select(inputs)

    assert decision.intents == ()
    assert decision.reasons[-1] == (
        "sizing: raw_centis=100000 g_ppm=1000000 binding_cap=per_bucket:fed-policy "
        "final_centis=0"
    )


# =============================================================================
# select()-level: human-tag override precedence
# =============================================================================


def test_select_human_bucket_governs_while_saturated_llm_bucket_ignored() -> None:
    """Target carries an LLM `fed-policy` tag AND a human `inflation` tag; a
    peer saturates `fed-policy` (1_000_000_000 exposure) but no peer touches
    `inflation`.

    Because a human tag is present, the target's ONLY effective bucket is
    `inflation` (SPEC S9.9 override): the saturated `fed-policy` bucket is
    irrelevant to this evaluation. `inflation`'s headroom is the full
    ceiling (no matching peer), so the same unclipped-100_000 arithmetic as
    the first-intent scenario applies: `binding_cap=none`.
    """
    target_tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
        CorrelationTag(bucket_id=BUCKET_INFLATION, source="human", tagged_at=_INSTANT),
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-FED-SATURATED",
        exposure_micros=MoneyMicros(1_000_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    inputs = _bucket_inputs(correlation_tags=target_tags, bucket_peers=(peer,))

    decision = select(inputs)

    assert len(decision.intents) == 1
    assert decision.intents[0].size == ContractCentis(100_000)
    assert decision.reasons[-1] == (
        "sizing: raw_centis=100000 g_ppm=1000000 binding_cap=none final_centis=100000"
    )


def test_select_human_bucket_saturated_is_clipped_even_though_llm_bucket_is_empty() -> (
    None
):
    """Mirror of the case above: the target's effective (human) bucket is
    `inflation`, and it is `inflation` -- not the empty `fed-policy` -- that a
    peer saturates (900_000_000 exposure).

    Identical headroom/cap arithmetic to the second-intent scenario
    (headroom 100_000_000 -> cap 20_000 centis): the human override still
    clips correctly when its OWN bucket is the contested one,
    `binding_cap=per_bucket:inflation`.
    """
    target_tags = (
        CorrelationTag(bucket_id=BUCKET_FED_POLICY, source="llm", tagged_at=_INSTANT),
        CorrelationTag(bucket_id=BUCKET_INFLATION, source="human", tagged_at=_INSTANT),
    )
    peer = BucketExposureEntry(
        market_ticker="PEER-INFLATION-1",
        exposure_micros=MoneyMicros(900_000_000),
        tags=(
            CorrelationTag(
                bucket_id=BUCKET_INFLATION, source="llm", tagged_at=_INSTANT
            ),
        ),
    )
    inputs = _bucket_inputs(correlation_tags=target_tags, bucket_peers=(peer,))

    decision = select(inputs)

    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.size == ContractCentis(20_000)
    assert decision.reasons[-1] == (
        "sizing: raw_centis=100000 g_ppm=1000000 binding_cap=per_bucket:inflation "
        "final_centis=20000"
    )


# =============================================================================
# Regression guard: clip_to_caps's default bucket_cap_name is unchanged
# =============================================================================


def test_clip_to_caps_default_bucket_cap_name_is_still_the_bare_per_bucket() -> None:
    """`clip_to_caps`'s DEFAULT `bucket_cap_name` keyword (this issue's new,
    backward-compatible parameter) must still render the bare `"per_bucket"`
    name when the bucket cap is the unique binder, so every pre-#47 call site
    and golden that never passes `bucket_cap_name` keeps naming the cap
    `"per_bucket"` byte-for-byte -- this is exactly
    `test_sizing_examples.py::test_clip_to_caps_per_bucket_headroom_uniquely_binds`'s
    own scenario, reproduced here as this module's own regression guard.

    equity=$1,000, `max_pos_bucket_pct_ppm`=100_000 (10%, schema default) ->
    ceiling $100; `bucket_exposure`=$99 -> headroom $1.

    cap_size_centis = divide(1_000_000*100, 450_000, floor)
                    = divide(100_000_000, 450_000, floor)
    450_000*222 = 99_900_000; remainder = 100_000 (< 450_000) -> 222, floors
    to 200.
    """
    positions = PositionReadModelInput(
        snapshot_id="positions-bucket-cap-name-regression",
        equity_micros=MoneyMicros(1_000_000_000),
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
        total_deploy_cap_micros=MoneyMicros(1_000_000_000_000),
        market_exposure=MoneyMicros(0),
        event_exposure=MoneyMicros(0),
        bucket_exposure=MoneyMicros(99_000_000),
        total_exposure=MoneyMicros(0),
        notional_today=MoneyMicros(0),
    )
    risk_config = RiskConfig()
    order_book = OrderBookSnapshot(
        ticker="BUCKET-CAP-NAME-REGRESSION",
        yes_bids=(),
        yes_asks=(
            OrderBookLevel(price=PricePips(4_500), quantity=ContractCentis(1_000_000)),
        ),
        fetched_at=_INSTANT,
    )

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(size=ContractCentis(200), binding_cap="per_bucket")
