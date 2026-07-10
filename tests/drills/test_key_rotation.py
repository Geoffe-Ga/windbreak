"""Failing-first tests for the `key-rotation` drill (issue #59, RED).

`windbreak.drills.key_rotation` does not exist yet, so the import below fails
collection with `ModuleNotFoundError` -- the expected Gate 1 RED state for
issue #59.

Pins two layers: the public `rotate_keys` mapping-rotation helper (tested
directly against the mapping it returns, since the assembled
`KeyRotationDrill`'s own `DrillResult.evidence` must never carry raw key
material -- only variable names/booleans/fingerprints), and the drill
itself, whose evidence hygiene this file separately asserts.

Fake credentials only: `"0"*64` (obviously-fake, low-entropy hex) and
`"fake-key-not-real"` -- never a real secret.

Design assumption (flagged for the implementer): `rotate_keys(env, *,
keys)` returns a *new* mapping with each named variable replaced by a
freshly generated, same-shape value (hex for the signing key so
`SigningKeyHandle.from_env` still loads it), never mutating `env` in place;
`KeyRotationDrill.execute` calls `rotate_keys` internally, then verifies
`SigningKeyHandle.from_env` loads against the rotated mapping and
`run_preflight`'s `exit_code` is `0` against it, reporting only
`rotated_vars`/`signing_key_loadable`/`preflight_exit_code` in its evidence
-- never the rotated mapping itself.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import FIXED_EPOCH_S, InMemoryDrillLedgerWriter
from windbreak.drills import key_rotation
from windbreak.drills.context import DrillContext
from windbreak.drills.framework import DrillPreconditionError
from windbreak.drills.key_rotation import KeyRotationDrill, rotate_keys
from windbreak.riskkernel.signing import SigningKeyHandle

if TYPE_CHECKING:
    from pathlib import Path

_APPROVAL_TOKEN_VAR = "WINDBREAK_APPROVAL_TOKEN_KEY"
_TRADE_KEY_VAR = "WINDBREAK_TRADE_KEY"

_OLD_SIGNING_KEY = "0" * 64
_OLD_TRADE_KEY = "fake-key-not-real"


def _fake_env() -> dict[str, str]:
    """Build an obviously-fake pre-rotation environment mapping."""
    return {_APPROVAL_TOKEN_VAR: _OLD_SIGNING_KEY, _TRADE_KEY_VAR: _OLD_TRADE_KEY}


# --- rotate_keys(): the old value is absent everywhere in the result -----------


def test_rotate_keys_replaces_every_rotated_variable_with_a_new_value() -> None:
    """`rotate_keys` replaces each targeted variable's value; the mapping's
    other keys are left present (though also targeted here, both change).
    """
    env = _fake_env()

    rotated = rotate_keys(env, keys=(_APPROVAL_TOKEN_VAR, _TRADE_KEY_VAR))

    assert set(rotated) == set(env)
    assert rotated[_APPROVAL_TOKEN_VAR] != _OLD_SIGNING_KEY
    assert rotated[_TRADE_KEY_VAR] != _OLD_TRADE_KEY


def test_rotate_keys_old_value_is_absent_from_every_value_in_the_result() -> None:
    """Neither old value survives anywhere in the rotated mapping's values --
    not even under a different key.
    """
    env = _fake_env()

    rotated = rotate_keys(env, keys=(_APPROVAL_TOKEN_VAR, _TRADE_KEY_VAR))

    assert _OLD_SIGNING_KEY not in rotated.values()
    assert _OLD_TRADE_KEY not in rotated.values()


def test_rotate_keys_new_signing_key_is_valid_hex_of_the_required_length() -> None:
    """The rotated signing-key value is still valid hex decoding to at least
    32 bytes -- `SigningKeyHandle` requires this, so a rotation can never
    silently produce an unusable key.
    """
    env = _fake_env()

    rotated = rotate_keys(env, keys=(_APPROVAL_TOKEN_VAR,))

    SigningKeyHandle.from_env(environ=rotated, var=_APPROVAL_TOKEN_VAR)


def test_rotate_keys_does_not_mutate_the_input_mapping() -> None:
    """`rotate_keys` returns a new mapping; the caller's original `env` is
    left untouched (so a caller can still diff old vs. new).
    """
    env = _fake_env()
    original = dict(env)

    rotate_keys(env, keys=(_APPROVAL_TOKEN_VAR,))

    assert env == original


def test_rotate_keys_leaves_an_untargeted_variable_untouched() -> None:
    """A variable not named in `keys` survives the rotation unchanged."""
    env = _fake_env()

    rotated = rotate_keys(env, keys=(_APPROVAL_TOKEN_VAR,))

    assert rotated[_TRADE_KEY_VAR] == _OLD_TRADE_KEY


# --- KeyRotationDrill: preflight passes against the rotated environment -------


def _build_ctx(tmp_path: Path, env: dict[str, str]) -> DrillContext:
    """Build a `DrillContext` carrying `env` as its injected environment."""
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env=env,
        exchange=None,
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )


def test_key_rotation_drill_passes_and_preflight_exit_code_is_zero(
    tmp_path: Path,
) -> None:
    """`KeyRotationDrill().run(ctx)` passes, and its evidence records a clean
    (`0`) preflight exit code against the rotated environment.
    """
    ctx = _build_ctx(tmp_path, _fake_env())
    drill = KeyRotationDrill()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.evidence["preflight_exit_code"] == 0


def test_key_rotation_drill_evidence_contains_no_key_material(tmp_path: Path) -> None:
    """The drill's evidence never contains the old signing-key/trade-key
    material as a substring anywhere in its JSON serialization -- only
    variable names, booleans, and preflight's integer exit code.
    """
    ctx = _build_ctx(tmp_path, _fake_env())
    drill = KeyRotationDrill()

    result = drill.run(ctx)

    serialized = json.dumps(result.evidence)
    assert _OLD_SIGNING_KEY not in serialized
    assert _OLD_TRADE_KEY not in serialized


def test_key_rotation_drill_reports_the_rotated_variable_names(tmp_path: Path) -> None:
    """The drill's evidence names which variables it rotated (never their
    values).
    """
    ctx = _build_ctx(tmp_path, _fake_env())
    drill = KeyRotationDrill()

    result = drill.run(ctx)

    assert set(result.evidence["rotated_vars"]) >= {_APPROVAL_TOKEN_VAR}


# --- Negative / fault-injection: FAILURE branches (issue #59 Gate 1 coverage) --


def test_key_rotation_precondition_raises_when_approval_token_var_is_absent(
    tmp_path: Path,
) -> None:
    """`check_preconditions` raises `DrillPreconditionError` when the
    approval-token variable is absent from the injected environment --
    there is nothing to rotate.
    """
    ctx = _build_ctx(tmp_path, {})
    drill = KeyRotationDrill()

    with pytest.raises(DrillPreconditionError):
        drill.check_preconditions(ctx)


def test_signing_key_loadable_returns_false_for_a_non_hex_rotated_value() -> None:
    """`_signing_key_loadable` returns `False` (never raises) when the
    rotated approval-token value is not valid hex -- the admissible-shape
    audit's own failure signal, exercised directly against a hand-crafted
    rotated mapping.
    """
    drill = KeyRotationDrill()

    loadable = drill._signing_key_loadable({_APPROVAL_TOKEN_VAR: "not-hex-zz"})

    assert loadable is False


def test_key_rotation_drill_fails_when_the_rotated_key_is_not_valid_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken token-generation collaborator (`secrets.token_hex` mocked to
    mint a non-hex value) makes the rotated signing key inadmissible, and the
    drill grades `passed=False` -- never a silently-passed bad rotation.
    """
    monkeypatch.setattr(
        key_rotation.secrets, "token_hex", lambda _nbytes: "not-valid-hex-zz"
    )
    ctx = _build_ctx(tmp_path, _fake_env())
    drill = KeyRotationDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence["signing_key_loadable"] is False


def test_key_rotation_drill_fails_when_preflight_does_not_grade_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken preflight collaborator (mocked to report a non-zero exit
    code) fails the drill even though the rotated signing key itself loads
    fine -- the drill never grades `passed=True` on a dirty preflight
    posture.
    """

    class _DirtyReport:
        """A `PreflightReport` double reporting a non-zero exit code."""

        exit_code = 1

    monkeypatch.setattr(key_rotation, "run_preflight", lambda **_kwargs: _DirtyReport())
    ctx = _build_ctx(tmp_path, _fake_env())
    drill = KeyRotationDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence["preflight_exit_code"] == 1
