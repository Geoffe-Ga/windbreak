"""Failing-first tests for windbreak.numeric.rounding (issue #12, SPEC S6.1).

`divide()` is windbreak's only sanctioned integer division: it is always
paired with an explicit `RoundingDirection` so every conservative-rounding
decision in the codebase is visible at the call site rather than implied
by a bare `//`. `OVERSTATE_COST` always rounds toward +infinity (sign-safe
ceiling, `-(-n // d)`); `UNDERSTATE_EQUITY` always rounds toward -infinity
(Python's native floor, `n // d`) -- regardless of the sign of numerator
or denominator. No float appears anywhere in this module or its tests.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from windbreak.numeric.rounding import RoundingDirection, divide

_OVER = RoundingDirection.OVERSTATE_COST
_UNDER = RoundingDirection.UNDERSTATE_EQUITY

#: Nonzero denominators for hypothesis, bounded to keep examples readable.
_NONZERO_DENOMINATOR = st.integers(min_value=-10_000, max_value=10_000).filter(
    lambda n: n != 0
)


def test_rounding_direction_has_exactly_two_members() -> None:
    """The enum is closed: exactly OVERSTATE_COST and UNDERSTATE_EQUITY."""
    assert {member.name for member in RoundingDirection} == {
        "OVERSTATE_COST",
        "UNDERSTATE_EQUITY",
    }


# --- Zero-denominator guard ---------------------------------------------------


@pytest.mark.parametrize("rounding", [_OVER, _UNDER])
@pytest.mark.parametrize("numerator", [0, 1, -1, 1000])
def test_divide_by_zero_raises_zero_division_error(numerator, rounding) -> None:
    """Zero denominator must fail loudly -- never silently return 0 or inf."""
    with pytest.raises(ZeroDivisionError):
        divide(numerator, 0, rounding=rounding)


def test_divide_rounding_is_keyword_only() -> None:
    """`rounding` is a required keyword-only argument, never positional."""
    with pytest.raises(TypeError):
        divide(7, 2, RoundingDirection.OVERSTATE_COST)


def test_divide_rounding_is_required() -> None:
    """Omitting `rounding` entirely must fail loudly, not default silently."""
    with pytest.raises(TypeError):
        divide(7, 2)


# --- Pinned concrete cases (mutation-resistant exact values) -----------------


@pytest.mark.parametrize(
    "numerator,denominator,expected_over,expected_under",
    [
        (7, 2, 4, 3),  # true quotient 3.5: ceil 4, floor 3
        (-7, 2, -3, -4),  # true quotient -3.5: ceil -3, floor -4
        (7, -2, -3, -4),  # true quotient -3.5: ceil -3, floor -4
        (-7, -2, 4, 3),  # true quotient 3.5: ceil 4, floor 3
        (6, 2, 3, 3),  # exact division: both directions agree
        (-6, 2, -3, -3),  # exact division, negative numerator
        (-6, -2, 3, 3),  # exact division, both negative
        (0, 5, 0, 0),  # zero numerator is always exact
    ],
)
def test_divide_pinned_cases(
    numerator, denominator, expected_over, expected_under
) -> None:
    """Concrete sign/remainder combinations chosen to kill off-by-one and
    sign-flip mutants in the ceil/floor implementation."""
    assert divide(numerator, denominator, rounding=_OVER) == expected_over
    assert divide(numerator, denominator, rounding=_UNDER) == expected_under


# --- Hypothesis properties ----------------------------------------------------


@given(
    numerator=st.integers(min_value=-10_000, max_value=10_000),
    denominator=_NONZERO_DENOMINATOR,
)
def test_divide_matches_ceil_floor_formulas(numerator: int, denominator: int) -> None:
    """OVERSTATE_COST is sign-safe ceiling; UNDERSTATE_EQUITY is Python floor.

    These are exactly the formulas SPEC'd for the conservative-rounding
    contract, expressed without any float arithmetic.
    """
    over = divide(numerator, denominator, rounding=_OVER)
    under = divide(numerator, denominator, rounding=_UNDER)

    assert over == -(-numerator // denominator)
    assert under == numerator // denominator


@given(
    numerator=st.integers(min_value=-10_000, max_value=10_000),
    denominator=_NONZERO_DENOMINATOR,
)
def test_divide_over_and_under_differ_by_remainder_presence(
    numerator: int, denominator: int
) -> None:
    """OVERSTATE_COST >= UNDERSTATE_EQUITY always; the gap is exactly 1 when
    the division is inexact, and 0 when it divides evenly."""
    over = divide(numerator, denominator, rounding=_OVER)
    under = divide(numerator, denominator, rounding=_UNDER)
    gap = over - under

    assert over >= under
    if numerator % denominator == 0:
        assert gap == 0
    else:
        assert gap == 1


@given(
    quotient=st.integers(min_value=-1_000, max_value=1_000),
    denominator=_NONZERO_DENOMINATOR,
)
def test_divide_exact_division_identity(quotient: int, denominator: int) -> None:
    """When `d` evenly divides `n`, both rounding directions agree exactly."""
    numerator = quotient * denominator

    over = divide(numerator, denominator, rounding=_OVER)
    under = divide(numerator, denominator, rounding=_UNDER)

    assert over == quotient
    assert under == quotient
