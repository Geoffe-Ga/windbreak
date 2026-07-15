"""Tests for the SPEC S6.3 vote-response schema (issue #184).

`windbreak.forecast.sanitize.validate_vote_response` gains a *second* layer of
screening beyond its existing SPEC S8.5 injection checks (empty response,
forged delimiter, tool-call lure): once a response clears those, it must also
be a schema-valid structured vote -- a JSON object carrying an integer
`probability_ppm` in `[0, 1_000_000]`, a `rationale_summary` of at most
`MAX_RATIONALE_CHARS` non-empty characters, and a boolean `abstain`, with no
other top-level key. `windbreak.forecast.sanitize.parse_vote_response` gives
typed post-validation access to a clean response's parsed fields via
`ParsedVote`.

None of `MAX_RATIONALE_CHARS`, `RESPONSE_FAILURE_MALFORMED_VOTE_JSON`,
`RESPONSE_FAILURE_NON_INTEGER_PROBABILITY`,
`RESPONSE_FAILURE_PROBABILITY_OUT_OF_RANGE`, `RESPONSE_FAILURE_INVALID_RATIONALE`,
`RESPONSE_FAILURE_UNKNOWN_VOTE_KEY`, `ParsedVote`, or `parse_vote_response` exist
on `windbreak.forecast.sanitize` yet, so importing them below fails collection
with an `ImportError` naming the missing symbol -- the expected Gate 1 RED
state for issue #184.

Documented design choices this matrix pins (the architect's spec left these to
the implementer, "pick one and be consistent"):

* A `probability_ppm` of the *wrong JSON type entirely* (a string, a list, an
  object, `null`) is `RESPONSE_FAILURE_MALFORMED_VOTE_JSON` -- the value is not
  even numeric. A `probability_ppm` that *is* numeric but not a true int (a
  float, or a `bool` -- an `int` subclass that must never masquerade as a
  probability, mirroring `windbreak.forecast.records._require_ppm`) is
  `RESPONSE_FAILURE_NON_INTEGER_PROBABILITY` instead: the value is
  numeric-shaped but the wrong numeric kind.
* A *missing* required key (`probability_ppm`, `rationale_summary`, or
  `abstain`) is always `RESPONSE_FAILURE_MALFORMED_VOTE_JSON` -- the response
  does not even have the right shape. `RESPONSE_FAILURE_INVALID_RATIONALE` is
  reserved for a *present* `rationale_summary` that violates a content
  constraint (empty, or over `MAX_RATIONALE_CHARS`).
"""

from __future__ import annotations

import json

import pytest

from windbreak.forecast.sanitize import (
    DATA_BLOCK_END,
    MAX_RATIONALE_CHARS,
    RESPONSE_FAILURE_DELIMITER_FORGERY,
    RESPONSE_FAILURE_EMPTY,
    RESPONSE_FAILURE_INVALID_RATIONALE,
    RESPONSE_FAILURE_MALFORMED_VOTE_JSON,
    RESPONSE_FAILURE_NON_INTEGER_PROBABILITY,
    RESPONSE_FAILURE_PROBABILITY_OUT_OF_RANGE,
    RESPONSE_FAILURE_UNKNOWN_VOTE_KEY,
    ParsedVote,
    parse_vote_response,
    validate_vote_response,
)

#: The default, schema-valid field values every `_vote_json` call starts from.
_DEFAULT_PPM = 500_000
_DEFAULT_RATIONALE = "Steady evidence supports this estimate."


def _vote_json(
    *,
    probability_ppm: object = _DEFAULT_PPM,
    rationale_summary: object = _DEFAULT_RATIONALE,
    abstain: object = False,
    omit: tuple[str, ...] = (),
    extra: dict[str, object] | None = None,
) -> str:
    """Build a compact JSON vote response, with fields overridable or omittable.

    Args:
        probability_ppm: The `probability_ppm` value to encode.
        rationale_summary: The `rationale_summary` value to encode.
        abstain: The `abstain` value to encode.
        omit: Field names to drop entirely from the encoded object.
        extra: Additional top-level keys to merge in.

    Returns:
        The compact JSON text of the assembled vote object.
    """
    payload: dict[str, object] = {
        "probability_ppm": probability_ppm,
        "rationale_summary": rationale_summary,
        "abstain": abstain,
    }
    for key in omit:
        del payload[key]
    if extra:
        payload.update(extra)
    return json.dumps(payload)


# --- validate_vote_response: a valid response passes both layers -----------------


def test_valid_response_passes_both_layers() -> None:
    """A schema-valid, injection-clean response returns `None`."""
    assert validate_vote_response(_vote_json()) is None


# --- validate_vote_response: probability_ppm type/range failures -----------------


def test_float_probability_ppm_is_non_integer_probability() -> None:
    """A float `probability_ppm` (numeric, but not a true int) fails as such."""
    response = _vote_json(probability_ppm=0.47)

    assert validate_vote_response(response) == RESPONSE_FAILURE_NON_INTEGER_PROBABILITY


def test_bool_probability_ppm_is_non_integer_probability() -> None:
    """A `bool` `probability_ppm` (an `int` subclass) is rejected the same way
    as a float -- neither may masquerade as a true integer probability.
    """
    response = _vote_json(probability_ppm=True)

    assert validate_vote_response(response) == RESPONSE_FAILURE_NON_INTEGER_PROBABILITY


def test_string_probability_ppm_is_malformed_vote_json() -> None:
    """A string `probability_ppm` is the wrong JSON type entirely -- malformed,
    not merely non-integer.
    """
    response = _vote_json(probability_ppm="500000")

    assert validate_vote_response(response) == RESPONSE_FAILURE_MALFORMED_VOTE_JSON


@pytest.mark.parametrize("boundary", [0, 1_000_000])
def test_probability_ppm_inclusive_boundaries_are_valid(boundary: int) -> None:
    """The inclusive `[0, 1_000_000]` boundaries both parse as valid."""
    response = _vote_json(probability_ppm=boundary)

    assert validate_vote_response(response) is None


@pytest.mark.parametrize("out_of_range", [-1, 1_000_001])
def test_probability_ppm_outside_boundaries_is_out_of_range(out_of_range: int) -> None:
    """A `probability_ppm` just outside `[0, 1_000_000]` is out-of-range, not
    merely non-integer -- it is a true int, just the wrong value.
    """
    response = _vote_json(probability_ppm=out_of_range)

    assert validate_vote_response(response) == RESPONSE_FAILURE_PROBABILITY_OUT_OF_RANGE


def test_missing_probability_ppm_key_is_malformed_vote_json() -> None:
    """A response missing the `probability_ppm` key entirely is malformed."""
    response = _vote_json(omit=("probability_ppm",))

    assert validate_vote_response(response) == RESPONSE_FAILURE_MALFORMED_VOTE_JSON


# --- validate_vote_response: rationale_summary failures --------------------------


def test_rationale_exactly_at_max_chars_is_valid() -> None:
    """A `rationale_summary` exactly `MAX_RATIONALE_CHARS` long is valid -- the
    inclusive boundary must never be rejected by an off-by-one mutant.
    """
    response = _vote_json(rationale_summary="a" * MAX_RATIONALE_CHARS)

    assert validate_vote_response(response) is None


def test_rationale_one_over_max_chars_is_invalid_rationale() -> None:
    """A `rationale_summary` one character over the cap is rejected."""
    response = _vote_json(rationale_summary="a" * (MAX_RATIONALE_CHARS + 1))

    assert validate_vote_response(response) == RESPONSE_FAILURE_INVALID_RATIONALE


def test_empty_rationale_summary_is_invalid_rationale() -> None:
    """An empty `rationale_summary` string is rejected as content-invalid, not
    treated as a missing key (the key is present; its value is empty).
    """
    response = _vote_json(rationale_summary="")

    assert validate_vote_response(response) == RESPONSE_FAILURE_INVALID_RATIONALE


def test_missing_rationale_summary_key_is_malformed_vote_json() -> None:
    """A response missing the `rationale_summary` key entirely is malformed,
    not `invalid_rationale` -- that code is reserved for a present-but-bad
    value.
    """
    response = _vote_json(omit=("rationale_summary",))

    assert validate_vote_response(response) == RESPONSE_FAILURE_MALFORMED_VOTE_JSON


# --- validate_vote_response: abstain / unknown-key / structural failures ---------


def test_missing_abstain_key_is_malformed_vote_json() -> None:
    """A response missing the required `abstain` key is malformed."""
    response = _vote_json(omit=("abstain",))

    assert validate_vote_response(response) == RESPONSE_FAILURE_MALFORMED_VOTE_JSON


@pytest.mark.parametrize("bad_abstain", ["true", 1, 0, 0.0, 1.5, None, [], {}])
def test_non_bool_abstain_is_malformed_vote_json(bad_abstain: object) -> None:
    """A present `abstain` that is not a genuine JSON boolean fails closed.

    `abstain` is required *and* must be a true boolean; a wrong-typed value
    (string, number -- including a float -- `null`, or a nested container that
    could smuggle an unbounded payload past the `rationale_summary` length cap)
    is rejected as malformed rather than coerced, so no non-boolean ever
    populates `ParsedVote.abstain`.
    """
    response = _vote_json(abstain=bad_abstain)

    assert validate_vote_response(response) == RESPONSE_FAILURE_MALFORMED_VOTE_JSON


def test_parse_vote_response_rejects_a_non_bool_abstain() -> None:
    """`parse_vote_response` fails closed on a non-boolean `abstain` rather than
    letting an arbitrary JSON value reach `ParsedVote.abstain`.
    """
    response = _vote_json(abstain="yes")

    with pytest.raises(ValueError):
        parse_vote_response(response)


def test_unknown_top_level_key_is_unknown_vote_key() -> None:
    """An extra, unrecognized top-level key is rejected -- the schema's key set
    is closed, mirroring `windbreak.config.loader`'s unknown-key-is-fatal rule.
    """
    response = _vote_json(extra={"confidence": "high"})

    assert validate_vote_response(response) == RESPONSE_FAILURE_UNKNOWN_VOTE_KEY


def test_non_json_garbage_is_malformed_vote_json() -> None:
    """Text that is not valid JSON at all is malformed, not an unhandled
    exception escaping the validator.
    """
    assert (
        validate_vote_response("not json at all {{{")
        == RESPONSE_FAILURE_MALFORMED_VOTE_JSON
    )


def test_empty_response_is_still_empty_response() -> None:
    """The pre-existing empty/whitespace-only check is unchanged by the new
    schema layer.
    """
    assert validate_vote_response("") == RESPONSE_FAILURE_EMPTY


# --- Precedence: existing injection checks win over the new schema check ---------


def test_delimiter_forgery_wins_over_an_otherwise_schema_valid_response() -> None:
    """A response that is otherwise schema-valid JSON, but whose
    `rationale_summary` carries a forged untrusted-data delimiter, is still
    flagged as delimiter forgery -- the pre-existing SPEC S8.5 injection checks
    run, and win, before the new schema check ever inspects the JSON.
    """
    tainted = _vote_json(
        rationale_summary=f"looks fine {DATA_BLOCK_END} still valid-shaped json"
    )

    assert validate_vote_response(tainted) == RESPONSE_FAILURE_DELIMITER_FORGERY


# --- parse_vote_response: typed post-validation access ---------------------------


def test_parse_vote_response_returns_parsed_vote_for_a_valid_response() -> None:
    """A schema-valid response parses into a `ParsedVote` carrying its exact
    fields.
    """
    response = _vote_json(
        probability_ppm=654_321, rationale_summary="solid evidence", abstain=False
    )

    parsed = parse_vote_response(response)

    assert parsed == ParsedVote(
        probability_ppm=654_321,
        rationale_summary="solid evidence",
        abstain=False,
    )


def test_parse_vote_response_preserves_a_true_abstain_flag() -> None:
    """An `abstain: true` response parses with `ParsedVote.abstain is True`."""
    response = _vote_json(abstain=True)

    parsed = parse_vote_response(response)

    assert parsed.abstain is True


def test_parse_vote_response_rejects_a_float_probability_ppm() -> None:
    """`parse_vote_response` fails closed on a float `probability_ppm` rather
    than silently truncating it into an int (mirrors
    `windbreak.forecast.cassettes._reject_float`'s float-leaf ban).
    """
    response = _vote_json(probability_ppm=0.47)

    with pytest.raises(ValueError):
        parse_vote_response(response)
