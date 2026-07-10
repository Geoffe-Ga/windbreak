"""Live-vs-paper execution-quality records and the slippage-ratio series (#58).

This module owns SPEC §17.4's execution-quality comparison: for every real live
fill it re-derives the *paper* fill-model cost over the same recorded book (via
:func:`windbreak.connector.fills.walk_taker_fill`) so a fill's realized cost can
be measured against what the pessimistic paper model would have charged. The
per-fill :class:`ExecutionQualityRecord` carries both costs and a derived
``slippage_micros = actual_cost_micros - modeled_cost_micros``.

:func:`live_slippage_ratio` folds a set of records into the headline
cost-side ratio ``ceil(sum(actual) * 1_000_000 / sum(modeled))`` in ppm, rounded
up (``OVERSTATE_COST``) so realized slippage is never understated; an empty
record set is empty-but-valid and returns the
:data:`~windbreak.evaluation.cohorts.UNDEFINED` sentinel rather than raising,
mirroring ``traded_vs_skipped_brier_delta``'s empty-cohort handling.

:class:`ExecutionQualityRecorded` is the ledger event carrying one record's full
shape; it derives its base :class:`~windbreak.ledger.events.Event` fields through
a LOCAL :func:`_derive_typed_event` (the deliberate house pattern used in
:mod:`windbreak.evaluation.preregistration` / :mod:`windbreak.evaluation.crosscheck`),
so this issue never touches the ledger's central ``EVENT_TYPES`` map.

Every division routes through the sanctioned
:func:`windbreak.numeric.rounding.divide` with an explicit
:class:`~windbreak.numeric.rounding.RoundingDirection`; there is no ``float`` and
no bare ``/`` anywhere here (the evaluation package is float-denylisted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from windbreak.connector.fills import walk_taker_fill
from windbreak.evaluation.cohorts import UNDEFINED, UndefinedBrier
from windbreak.ledger.events import Event
from windbreak.numeric.rounding import RoundingDirection, divide

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from windbreak.connector.fees import FeeModel
    from windbreak.connector.models import OrderBookLevel
    from windbreak.numeric import ContractCentis, PricePips

#: One whole ratio expressed in ppm (1.0 == 1_000_000 ppm), and the scaling
#: factor lifting a cost ratio into ppm space.
_PPM_SCALE = 1_000_000

#: Payload schema version stamped on this module's events. Replicated locally
#: (rather than imported from :mod:`windbreak.ledger.events`'s private copy, out
#: of this issue's scope) so a payload-shape change here can be versioned
#: without reaching across the package boundary.
_SCHEMA_VERSION = 1

#: The integer record fields that must reject a ``bool`` masquerading as an int,
#: mirroring ``FixtureForecast.__post_init__``'s guard.
_INT_FIELD_NAMES: tuple[str, ...] = (
    "filled_centis",
    "actual_cost_micros",
    "modeled_cost_micros",
    "created_sequence",
)


@dataclass(frozen=True, slots=True)
class ExecutionQualityRecord:
    """One live fill compared against its reproduced paper-model cost.

    Attributes:
        fill_id: The venue's fill identifier.
        market_ticker: The market the fill executed in.
        side: The fill's side (e.g. ``"YES"``/``"NO"``).
        filled_centis: The quantity filled, in contract-centis.
        actual_cost_micros: The real observed cost of the fill, in micros.
        modeled_cost_micros: The paper fill-model cost reproduced over the same
            recorded book, in micros.
        model_version: The paper fill-model version the modeled cost was
            produced under (SPEC §17.4).
        created_sequence: The record's creation sequence on the append-only
            ledger, used to order the rolling window.
    """

    fill_id: str
    market_ticker: str
    side: str
    filled_centis: int
    actual_cost_micros: int
    modeled_cost_micros: int
    model_version: str
    created_sequence: int

    def __post_init__(self) -> None:
        """Reject any ``bool`` masquerading as one of the integer fields.

        Raises:
            TypeError: If ``filled_centis``, ``actual_cost_micros``,
                ``modeled_cost_micros``, or ``created_sequence`` is a ``bool``
                (an ``int`` subclass that must not slip through) or is not an
                ``int`` at all; the message names the offending field.
        """
        for name in _INT_FIELD_NAMES:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{name} requires a non-bool int, got {type(value).__name__}"
                )

    @property
    def slippage_micros(self) -> int:
        """Return the derived slippage ``actual_cost_micros - modeled_cost_micros``.

        Returns:
            The signed cost slippage in micros; positive when the fill cost more
            than the paper model, negative when it filled cheaper.
        """
        return self.actual_cost_micros - self.modeled_cost_micros


class ObservedFill(Protocol):
    """The minimal live-fill observation :func:`compare_fill_to_model` reads.

    A structural protocol (not a concrete class) so any object carrying these
    fields -- the two :func:`~windbreak.connector.fills.walk_taker_fill` needs
    plus the identity fields to build an :class:`ExecutionQualityRecord` -- is
    accepted without this module importing a concrete fill type.

    Attributes:
        fill_id: The venue's fill identifier.
        market_ticker: The market the fill executed in.
        side: The fill's side.
        limit: The order's limit price, in pips.
        requested: The requested size, in contract-centis.
        actual_cost_micros: The real observed cost of the fill, in micros.
        model_version: The paper fill-model version to stamp on the record.
        created_sequence: The record's creation sequence.
    """

    fill_id: str
    market_ticker: str
    side: str
    limit: PricePips
    requested: ContractCentis
    actual_cost_micros: int
    model_version: str
    created_sequence: int


def compare_fill_to_model(
    fill: ObservedFill,
    book_levels: Sequence[OrderBookLevel],
    fee_model: FeeModel,
    *,
    haircut_ppm: int,
    max_participation_ppm: int,
) -> ExecutionQualityRecord:
    """Compare one live fill against the paper model over the same recorded book.

    Re-derives the paper fill-model cost with
    :func:`windbreak.connector.fills.walk_taker_fill` over the identical book,
    fee model, and pessimism knobs the live fill executed against, so
    ``modeled_cost_micros`` is independently reproducible from
    ``book_cost + fee + haircut``. This is COMPARE-ONLY: it never mutates the
    fills module or the book.

    Args:
        fill: The observed live fill to compare.
        book_levels: The recorded book side the live fill executed against.
        fee_model: The fee schedule applied to each modeled slice.
        haircut_ppm: The slippage haircut on the modeled fee, in ppm.
        max_participation_ppm: The participation cap, in ppm (SPEC S9.5).

    Returns:
        The :class:`ExecutionQualityRecord` joining the fill's real cost to its
        reproduced paper-model cost.
    """
    reference = walk_taker_fill(
        book_levels,
        fill.limit,
        fill.requested,
        fee_model,
        haircut_ppm=haircut_ppm,
        max_participation_ppm=max_participation_ppm,
    )
    return ExecutionQualityRecord(
        fill_id=fill.fill_id,
        market_ticker=fill.market_ticker,
        side=fill.side,
        filled_centis=reference.filled.value,
        actual_cost_micros=fill.actual_cost_micros,
        modeled_cost_micros=reference.total_cost.value,
        model_version=fill.model_version,
        created_sequence=fill.created_sequence,
    )


class ZeroModeledCostError(ValueError):
    """Raised when a non-empty window's modeled-cost sum is exactly ``0``.

    That sum is :func:`live_slippage_ratio`'s denominator, so a zero leaves the
    ratio undefined and the raw fold fails closed rather than dividing by zero.
    Subclasses :class:`ValueError` so every existing ``ValueError`` handler (and
    direct callers using ``pytest.raises(ValueError, ...)``) still catches it,
    while adapters that must degrade this degenerate window to the
    :data:`~windbreak.evaluation.cohorts.UNDEFINED` sentinel can catch it
    narrowly without swallowing unrelated invalid-input ``ValueError``s.
    """


def live_slippage_ratio(
    records: Sequence[ExecutionQualityRecord],
) -> int | UndefinedBrier:
    """Return the cost-side live-vs-paper slippage ratio over ``records``, in ppm.

    The ratio is
    ``ceil(sum(actual_cost_micros) * 1_000_000 / sum(modeled_cost_micros))``,
    rounded up (``OVERSTATE_COST``) so realized slippage is never understated --
    the ceiling is applied to the aggregate ratio itself, independent of the
    sign of any individual record's slippage. An empty record set is
    empty-but-valid and returns the
    :data:`~windbreak.evaluation.cohorts.UNDEFINED` sentinel (never an
    exception), mirroring ``traded_vs_skipped_brier_delta``.

    Args:
        records: The execution-quality records to fold; the caller has already
            applied any rolling-window truncation.

    Returns:
        The slippage ratio in ppm, or :data:`~windbreak.evaluation.cohorts.UNDEFINED`
        for an empty record set.

    Raises:
        ZeroModeledCostError: If ``records`` is non-empty but its modeled-cost
            sum is exactly ``0`` -- the ratio's denominator, leaving the ratio
            undefined. Fails closed with a clear message rather than letting
            ``divide``'s bare :class:`ZeroDivisionError` escape, mirroring
            ``calibration_slope`` / ``calibration_intercept``'s zero-variance
            guard. Subclasses :class:`ValueError`, so direct callers catching
            ``ValueError`` still fail closed. (The empty-record set stays the
            ``UNDEFINED`` sentinel path above.)
    """
    if not records:
        return UNDEFINED
    actual_sum = sum(record.actual_cost_micros for record in records)
    modeled_sum = sum(record.modeled_cost_micros for record in records)
    if modeled_sum == 0:
        raise ZeroModeledCostError(
            "modeled cost sum is zero; live slippage ratio is undefined"
        )
    return divide(
        actual_sum * _PPM_SCALE,
        modeled_sum,
        rounding=RoundingDirection.OVERSTATE_COST,
    )


def require_model_version(
    records: Iterable[ExecutionQualityRecord], expected_version: str
) -> None:
    """Fail closed if any record's model version disagrees with the plan's.

    A recorded fill whose ``model_version`` does not match the gate plan's
    ``paper_fill_model_version`` cannot be compared against that model's cost
    (SPEC §17.4), so the divergence monitor rejects the whole run before
    ledgering anything.

    Args:
        records: The execution-quality records to validate.
        expected_version: The plan's pinned paper fill-model version.

    Raises:
        ValueError: If any record's ``model_version`` differs from
            ``expected_version``; the message names the ``model_version`` field
            and both values.
    """
    for record in records:
        if record.model_version != expected_version:
            raise ValueError(
                "execution record model_version mismatch: "
                f"{record.model_version!r} != plan {expected_version!r}"
            )


def _derive_typed_event(event: Event, payload: dict[str, object]) -> None:
    """Populate the derived :class:`~windbreak.ledger.events.Event` fields.

    Replicates :mod:`windbreak.ledger.events`'s private derivation locally (that
    module's ``EVENT_TYPES`` map is out of this issue's scope): sets
    ``event_type`` to the concrete class name, ``payload_schema_version`` to this
    module's schema version, and ``payload`` to the assembled dict, via
    ``object.__setattr__`` because the events are frozen.

    Args:
        event: The freshly constructed typed event to populate.
        payload: The type-specific payload assembled by the subclass.
    """
    object.__setattr__(event, "event_type", type(event).__name__)
    object.__setattr__(event, "payload_schema_version", _SCHEMA_VERSION)
    object.__setattr__(event, "payload", payload)


@dataclass(frozen=True)
class ExecutionQualityRecorded(Event):
    """Records one execution-quality comparison into the ledger.

    Attributes:
        record: The :class:`ExecutionQualityRecord` whose full shape is
            projected into the payload, so a reader can reconstruct the exact
            record from the ledger alone.
    """

    record: ExecutionQualityRecord
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "fill_id": self.record.fill_id,
            "market_ticker": self.record.market_ticker,
            "side": self.record.side,
            "filled_centis": self.record.filled_centis,
            "actual_cost_micros": self.record.actual_cost_micros,
            "modeled_cost_micros": self.record.modeled_cost_micros,
            "slippage_micros": self.record.slippage_micros,
            "model_version": self.record.model_version,
            "created_sequence": self.record.created_sequence,
        }
        _derive_typed_event(self, payload)
