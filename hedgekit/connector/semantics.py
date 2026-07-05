"""SPEC S6.2 balance-interpretation semantics: eight enums plus their record.

A venue's balance and order-accounting behavior is not self-evident from a
balance number alone -- whether an open order's collateral is already netted
out of the available balance, when a fee is debited, how a partial fill is
represented, and so on all change how a raw balance must be read. This module
pins those questions as eight closed enumerations, each carrying an explicit
``UNKNOWN`` member so an undocumented behavior is *recorded as unknown*, never
silently defaulted to a convenient guess.

:class:`BalanceSemantics` bundles one member from each enum into a frozen,
slotted record and exposes :meth:`BalanceSemantics.is_fully_known`, which is
True only when every field holds a non-``UNKNOWN`` member. The enum-member
*names* are the fixture encoding (a loader does ``EnumClass[name]``), so an
invented or typo'd name fails loudly rather than coercing to a default.

This module depends only on the standard library (``enum``/``dataclasses``) so
it stays acyclic: neither :mod:`hedgekit.connector.models` nor any adapter is
imported here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, fields


class OrderCollateralInTotal(enum.Enum):
    """Whether an open order's posted collateral is inside the *total* balance.

    Attributes:
        INCLUDED: The total balance includes collateral posted for open orders.
        EXCLUDED: The total balance excludes open-order collateral.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    INCLUDED = enum.auto()
    EXCLUDED = enum.auto()
    UNKNOWN = enum.auto()


class OrderCollateralInAvailable(enum.Enum):
    """How an open order's collateral affects the *available* balance.

    Attributes:
        DEDUCTED_FROM_AVAILABLE: Open-order collateral is subtracted from the
            available balance (so available already reflects the reservation).
        INCLUDED_IN_AVAILABLE: The available balance still counts collateral
            that is reserved against open orders.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    DEDUCTED_FROM_AVAILABLE = enum.auto()
    INCLUDED_IN_AVAILABLE = enum.auto()
    UNKNOWN = enum.auto()


class FeeDebitTiming(enum.Enum):
    """When a trading fee is debited from the account.

    Attributes:
        AT_EXECUTION: The fee is debited at fill time.
        AT_SETTLEMENT: The fee is debited when the market settles.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    AT_EXECUTION = enum.auto()
    AT_SETTLEMENT = enum.auto()
    UNKNOWN = enum.auto()


class FeeRounding(enum.Enum):
    """How a computed fee is rounded to a chargeable amount.

    Attributes:
        UP_TO_NEXT_CENT: The fee is rounded up to the next whole cent.
        EXACT: The fee is charged exactly, with no rounding.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    UP_TO_NEXT_CENT = enum.auto()
    EXACT = enum.auto()
    UNKNOWN = enum.auto()


class PartialFillRepresentation(enum.Enum):
    """How the account surface represents a partially filled order.

    Attributes:
        PER_FILL_RECORDS: Each partial fill appears as its own record.
        AGGREGATED: Partial fills are collapsed into one aggregate record.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    PER_FILL_RECORDS = enum.auto()
    AGGREGATED = enum.auto()
    UNKNOWN = enum.auto()


class CancelCollateralRelease(enum.Enum):
    """When collateral is released after an order is cancelled.

    Attributes:
        IMMEDIATE: Collateral is freed as soon as the cancel is acknowledged.
        DELAYED: Collateral is freed only after some later step.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    IMMEDIATE = enum.auto()
    DELAYED = enum.auto()
    UNKNOWN = enum.auto()


class UnsettledProceeds(enum.Enum):
    """How proceeds from a not-yet-settled position affect the balance.

    Attributes:
        EXCLUDED_UNTIL_CREDITED: Proceeds are withheld until the market credits
            them (never counted early).
        INCLUDED_IMMEDIATELY: Proceeds are reflected in the balance right away.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    EXCLUDED_UNTIL_CREDITED = enum.auto()
    INCLUDED_IMMEDIATELY = enum.auto()
    UNKNOWN = enum.auto()


class HaltedMarketBehavior(enum.Enum):
    """How the venue treats new orders while a market is halted.

    Attributes:
        NEW_ORDERS_REJECTED: New orders are refused during a halt.
        NEW_ORDERS_ACCEPTED: New orders are still accepted during a halt.
        UNKNOWN: The venue documents no answer; must never be inferred.
    """

    NEW_ORDERS_REJECTED = enum.auto()
    NEW_ORDERS_ACCEPTED = enum.auto()
    UNKNOWN = enum.auto()


@dataclass(frozen=True, slots=True)
class BalanceSemantics:
    """A venue's answers to the eight SPEC S6.2 balance-interpretation questions.

    Each field holds one member of its corresponding enum; a field left at that
    enum's ``UNKNOWN`` member records that the venue documents no answer.

    Attributes:
        open_order_collateral_in_total: Collateral's effect on the total balance.
        open_order_collateral_in_available: Collateral's effect on the available
            balance.
        fee_debit_timing: When a trading fee is debited.
        fee_rounding: How a computed fee is rounded.
        partial_fill_representation: How a partial fill is surfaced.
        cancel_collateral_release: When cancel frees collateral.
        unsettled_proceeds: How unsettled proceeds affect the balance.
        halted_market_behavior: How new orders are treated during a halt.
    """

    open_order_collateral_in_total: OrderCollateralInTotal
    open_order_collateral_in_available: OrderCollateralInAvailable
    fee_debit_timing: FeeDebitTiming
    fee_rounding: FeeRounding
    partial_fill_representation: PartialFillRepresentation
    cancel_collateral_release: CancelCollateralRelease
    unsettled_proceeds: UnsettledProceeds
    halted_market_behavior: HaltedMarketBehavior

    def is_fully_known(self) -> bool:
        """Return True iff no field is left at its enum's ``UNKNOWN`` member.

        The check iterates the dataclass fields generically, so a field added
        later cannot silently escape it: each field's value is compared by
        identity against its own enum class's ``UNKNOWN`` sentinel.

        Returns:
            True when every field holds a known (non-``UNKNOWN``) member.
        """
        members = (getattr(self, field.name) for field in fields(self))
        return all(member is not type(member).UNKNOWN for member in members)
