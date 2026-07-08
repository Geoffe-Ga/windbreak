"""Market-resolution outcomes for the evaluation harness (SPEC-EPIC_07, #49).

A *resolution* is the ground-truth answer a binary event market settled to:
``YES`` or ``NO``. The evaluation harness scores each forecast against the
resolution of the market it named, so this module owns the single small typed
vocabulary (:class:`ResolutionOutcome`) plus the loader
(:func:`resolutions_from_fixture`) that turns the raw JSON ``resolutions`` block
of a known-answer fixture into a ticker-keyed mapping of those outcomes.

This module is deliberately dependency-free within the package: it imports
nothing from :mod:`hedgekit.evaluation.registry` or
:mod:`hedgekit.evaluation.report`. That keeps the intra-package dependency
one-way (report -> registry -> resolution) and lets the registry reference
:class:`ResolutionOutcome` in type position without risking an import cycle.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

#: JSON key holding the list of ``{market_ticker, outcome}`` resolution entries.
_RESOLUTIONS_KEY = "resolutions"
#: JSON field naming the market a resolution belongs to.
_TICKER_FIELD = "market_ticker"
#: JSON field carrying the settled outcome token (``"yes"`` / ``"no"``).
_OUTCOME_FIELD = "outcome"


class ResolutionOutcome(enum.Enum):
    """The settled ground-truth outcome of a binary event market.

    A binary market resolves to exactly one of two states, encoded here by the
    lowercase JSON tokens the fixtures use so that ``ResolutionOutcome(token)``
    round-trips a raw string straight into the typed value.
    """

    YES = "yes"
    NO = "no"


def _outcome_from_token(token: str) -> ResolutionOutcome:
    """Parse a raw ``outcome`` token into a :class:`ResolutionOutcome`.

    Args:
        token: The raw outcome string from a fixture resolution entry.

    Returns:
        The matching :class:`ResolutionOutcome` member.

    Raises:
        ValueError: If ``token`` is not one of the known ``outcome`` values;
            the message names the ``outcome`` field for locatability.
    """
    try:
        return ResolutionOutcome(token)
    except ValueError as exc:
        raise ValueError(
            f"unknown resolution outcome: {token!r} "
            f"(expected one of {[member.value for member in ResolutionOutcome]})"
        ) from exc


def resolutions_from_fixture(
    fixture: Mapping[str, Any],
) -> Mapping[str, ResolutionOutcome]:
    """Build a ticker-keyed resolution mapping from a fixture payload.

    Reads the fixture's ``resolutions`` list -- each entry a
    ``{"market_ticker": ..., "outcome": ...}`` object -- and returns a mapping
    from each market ticker to its typed :class:`ResolutionOutcome`.

    Args:
        fixture: The decoded fixture payload, carrying a ``resolutions`` list.

    Returns:
        A mapping from ``market_ticker`` to its :class:`ResolutionOutcome`.

    Raises:
        ValueError: If an ``outcome`` token is unknown (message names
            ``outcome``), or if a ``market_ticker`` appears more than once
            (message names ``market_ticker``).
    """
    resolutions: dict[str, ResolutionOutcome] = {}
    for entry in fixture[_RESOLUTIONS_KEY]:
        ticker = entry[_TICKER_FIELD]
        outcome = _outcome_from_token(entry[_OUTCOME_FIELD])
        if ticker in resolutions:
            raise ValueError(f"duplicate market_ticker in resolutions: {ticker!r}")
        resolutions[ticker] = outcome
    return resolutions
