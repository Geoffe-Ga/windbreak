"""Golden determinism harness for hedgekit.selector.select (issue #43).

The whole point of this module: `select` + `serialize_decision` over a fixed
input must produce byte-identical output every time it is called, in-process
or from a brand-new interpreter, regardless of `PYTHONHASHSEED`. Four layers
of proof, from weakest to strongest:

1. `test_select_is_byte_identical_on_recorded_inputs` -- the issue's own
   verbatim claim: two in-process calls over the same recorded bundle agree.
2. `test_stub_returns_zero_intents_with_reason` -- pins the *shape* of the
   issue-#43 stub (zero intents, a `"stub: ..."` reason) so a later issue
   cannot quietly start fabricating intents before the real selection logic
   (SPEC S9.2-S9.5, issues #44-#47) exists.
3. `test_serialized_output_matches_committed_golden` -- compares against a
   *committed* golden file, so a silent change to field order/formatting is
   caught even if it happens to still be self-consistent.
4. `test_fresh_interpreter_produces_identical_bytes` -- runs `select` in a
   brand-new `python -c` subprocess (a different `PYTHONHASHSEED` than this
   process) and diffs its stdout bytes against the in-process serialization,
   ruling out any accidental dependence on hash-seed-influenced iteration
   order (e.g. an un-sorted dict/set) that a same-process comparison could
   never expose.

Golden newline convention (hook-stability): ``serialize_decision`` returns
canonical JSON with **no** trailing newline, but this repo's own
``end-of-file-fixer`` pre-commit hook requires every committed text file to
end in exactly one ``"\n"``. To keep a freshly regenerated golden stable
under that hook (rather than exempting the fixtures, which would weaken the
hook), each ``fixtures/*.golden`` file stores the serialized bytes **plus a
single trailing ``"\n"``**. The comparison below re-appends that newline to
the live serialized output before diffing, and
`test_committed_golden_is_hook_stable` pins the invariant so a regenerated
golden is always exactly ``serialize_decision(...) + "\n"``.

Golden-file regeneration: the two `fixtures/*.golden` files committed
alongside this module hold the correct issue-#43 **stub** output -- an empty
`intents` list and the single `"stub: ..."` reason. They will need
regeneration only once a later issue (#44+) makes `select` emit non-empty
intents and thereby changes the serialized bytes. Regenerate each from an
actual `select`/`serialize_decision` run -- never hand-fabricate the bytes,
and always append the single trailing newline the convention requires:

    python -c "
    from tests.selector.fixture_loader import load_inputs
    from hedgekit.selector import select, serialize_decision
    path = 'tests/selector/fixtures/bundle_a'
    bytes_ = serialize_decision(select(load_inputs(path + '.json')))
    open(path + '.golden', 'w').write(bytes_ + '\n')
    "

(substitute ``bundle_b`` for the second file).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hedgekit.selector import select, serialize_decision
from tests.selector.fixture_loader import load_inputs

if TYPE_CHECKING:
    from hedgekit.selector import SelectorInputs

#: This package's own committed bundle fixtures.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: The repository root, computed from this file's location
#: (`tests/selector/test_determinism_golden.py` -> `tests/selector` ->
#: `tests` -> repo root), so the fresh-interpreter subprocess below can
#: import both `hedgekit` and `tests.selector.fixture_loader` regardless of
#: its own working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Hard timeout for the fresh-interpreter subprocess (house rule: no
#: unbounded waits). A bare `python -c` importing a handful of modules and
#: running a stub selector comfortably finishes in well under this budget.
_SUBPROCESS_TIMEOUT_S = 30

#: Forced subprocess `PYTHONHASHSEED`, deliberately different from whatever
#: (random, by default) seed this parent test process is running under --
#: proving byte-identical output does not depend on hash-seed-influenced
#: iteration order.
_SUBPROCESS_HASHSEED = "12345"

#: All committed bundle names this module's parametrized tests iterate over.
_BUNDLE_NAMES: tuple[str, ...] = ("bundle_a", "bundle_b")


@pytest.mark.parametrize("bundle_name", _BUNDLE_NAMES)
def test_select_is_byte_identical_on_recorded_inputs(bundle_name: str) -> None:
    """`serialize_decision(select(bundle)) == serialize_decision(select(bundle))`.

    The issue's own verbatim determinism claim: calling `select` twice
    in-process over the identical recorded bundle must yield byte-identical
    serialized output.
    """
    inputs = load_inputs(_FIXTURES_DIR / f"{bundle_name}.json")

    assert serialize_decision(select(inputs)) == serialize_decision(select(inputs))


def test_stub_returns_zero_intents_with_reason(
    recorded_inputs_bundle_a: SelectorInputs,
) -> None:
    """The stub `select` returns zero intents and a `"stub: ..."` reason.

    Pins the not-yet-implemented stub's exact contract: it must never raise
    and never fabricate an intent -- only explain itself via `reasons`.
    """
    decision = select(recorded_inputs_bundle_a)

    assert decision.intents == ()
    assert decision.reasons
    assert decision.reasons[0].startswith("stub:")


@pytest.mark.parametrize("bundle_name", _BUNDLE_NAMES)
def test_serialized_output_matches_committed_golden(bundle_name: str) -> None:
    """`serialize_decision(select(bundle)) + "\\n"` equals the committed golden.

    See this module's docstring for the newline convention and the exact
    regeneration command: the golden stores the serialized bytes plus one
    trailing newline (so it is stable under `end-of-file-fixer`), so the live
    serialized output is compared with that newline re-appended.
    """
    inputs = load_inputs(_FIXTURES_DIR / f"{bundle_name}.json")
    golden_path = _FIXTURES_DIR / f"{bundle_name}.golden"

    actual = serialize_decision(select(inputs))

    assert actual + "\n" == golden_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bundle_name", _BUNDLE_NAMES)
def test_committed_golden_is_hook_stable(bundle_name: str) -> None:
    """Each committed golden ends in exactly one newline and no more.

    Pins the newline convention documented in this module's docstring: a
    golden is exactly ``serialize_decision(...) + "\\n"``. This guarantees a
    freshly regenerated golden survives this repo's `end-of-file-fixer`
    pre-commit hook unchanged -- ending in exactly one trailing ``"\\n"`` --
    so the fixtures never need a hook exemption and CI's "Pre-commit (all
    files)" step can never be tripped by a golden's missing/extra newline.
    """
    golden_text = (_FIXTURES_DIR / f"{bundle_name}.golden").read_text(encoding="utf-8")

    assert golden_text.endswith("\n")
    assert not golden_text.endswith("\n\n")
    assert golden_text.removesuffix("\n") == golden_text.rstrip("\n")


@pytest.mark.parametrize("bundle_name", _BUNDLE_NAMES)
def test_fresh_interpreter_produces_identical_bytes(bundle_name: str) -> None:
    """A brand-new interpreter (different `PYTHONHASHSEED`) reproduces the
    identical serialized bytes this process produces in-process.

    Rules out any accidental dependence on hash-seed-influenced iteration
    order (e.g. an un-sorted dict/set surviving into the serialized form).
    """
    bundle_path = _FIXTURES_DIR / f"{bundle_name}.json"
    in_process_bytes = serialize_decision(select(load_inputs(bundle_path))).encode(
        "utf-8"
    )

    snippet = (
        "import sys\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "from tests.selector.fixture_loader import load_inputs\n"
        "from hedgekit.selector import select, serialize_decision\n"
        f"inputs = load_inputs({str(bundle_path)!r})\n"
        "sys.stdout.write(serialize_decision(select(inputs)))\n"
    )
    subprocess_env = dict(os.environ)
    subprocess_env["PYTHONHASHSEED"] = _SUBPROCESS_HASHSEED

    result = subprocess.run(
        [sys.executable, "-c", snippet],
        env=subprocess_env,
        capture_output=True,
        text=False,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=True,
    )

    assert result.stdout == in_process_bytes
