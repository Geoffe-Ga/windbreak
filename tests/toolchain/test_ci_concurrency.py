"""Tests pinning the CI concurrency contract (P0: cancelled merge-commit runs).

The Ralph loop pushes a ``chore(ralph): record completion`` commit to main
~15 seconds after every squash merge. Both pushes shared the ci.yml
concurrency group ``CI-refs/heads/main`` with ``cancel-in-progress: true``,
so the merge commit's CI run (Quality x3, Chaos, Build) was cancelled on
every single PR merge. ``main-red-alarm`` only fired on a ``failure``
conclusion, so those ``cancelled`` runs were invisible: main looked green
while its merge commits were never actually verified.

Contract guarded here:

- ci.yml cancels superseded in-progress runs ONLY for pull_request events
  (stale-head dedup on a PR is desirable; cancelling main runs is not).
- Non-PR (push) runs use a unique per-run concurrency group, so a run on
  main is never cancelled in progress nor replaced while queued.
- main-red-alarm treats ``cancelled`` and ``timed_out`` CI conclusions on
  main as red, not just ``failure`` (defense in depth: any non-success
  completion of CI on main must raise the alarm).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_ALARM_PATH = _REPO_ROOT / ".github" / "workflows" / "main-red-alarm.yml"

#: The expression that must gate every cancellation decision in ci.yml.
_PR_GATE = "github.event_name == 'pull_request'"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Parse a GitHub Actions workflow file with ``yaml.safe_load``.

    Args:
        path: Absolute path to the workflow YAML file.

    Returns:
        The parsed top-level workflow mapping.
    """
    with path.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


def _ci_concurrency() -> dict[str, Any]:
    """Return ci.yml's top-level ``concurrency`` mapping.

    Returns:
        The ``concurrency`` mapping (``group`` / ``cancel-in-progress``).

    Raises:
        AssertionError: If ci.yml declares no top-level concurrency block.
    """
    workflow = _load_workflow(_CI_PATH)
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), (
        "ci.yml must declare a top-level `concurrency:` mapping -- without "
        "one, redundant PR runs pile up; with an unconditional one, main "
        "runs get cancelled"
    )
    return concurrency


def test_ci_cancel_in_progress_only_for_pull_requests() -> None:
    """``cancel-in-progress`` is gated on the event being a pull_request.

    A literal ``true`` cancelled the in-flight run for the merge commit on
    main whenever Ralph's record-completion push arrived seconds later,
    leaving every merged PR's main run with cancelled jobs.
    """
    cancel = _ci_concurrency()["cancel-in-progress"]

    assert cancel is not True, (
        "ci.yml `cancel-in-progress: true` is unconditional -- it cancels "
        "in-progress runs on main whenever a follow-up push lands; gate it "
        f"on `{_PR_GATE}`"
    )
    assert isinstance(cancel, str) and _PR_GATE in cancel, (
        f"ci.yml `cancel-in-progress` must be an expression gated on "
        f"`{_PR_GATE}`, got: {cancel!r}"
    )


def test_ci_push_runs_use_a_per_run_concurrency_group() -> None:
    """Non-PR runs get a unique per-run group so they can never be culled.

    Even with ``cancel-in-progress`` gated on pull_request, push runs
    sharing one queued slot per group means a burst of pushes to main
    replaces (cancels) intermediate *pending* runs. Keying the non-PR group
    on ``github.run_id`` makes every push run independent, so every commit
    that lands on main is fully verified.
    """
    group = _ci_concurrency()["group"]

    assert isinstance(group, str)
    assert "github.run_id" in group, (
        "ci.yml concurrency `group` must fall back to `github.run_id` for "
        f"non-pull_request events, got: {group!r}"
    )
    assert _PR_GATE in group, (
        "ci.yml concurrency `group` must key on the PR ref only for "
        f"pull_request events (gate: `{_PR_GATE}`), got: {group!r}"
    )


def test_main_red_alarm_fires_on_any_non_success_conclusion() -> None:
    """The alarm condition covers failure, cancelled, and timed_out.

    A cancelled CI run on main means the commit was NOT verified; treating
    only ``failure`` as red let every cancelled merge-commit run pass
    silently while the branch looked green.
    """
    workflow = _load_workflow(_ALARM_PATH)
    condition = workflow["jobs"]["alarm"]["if"]

    assert isinstance(condition, str)
    for conclusion in ("failure", "cancelled", "timed_out"):
        assert conclusion in condition, (
            f"main-red-alarm must treat a `{conclusion}` CI conclusion on "
            f"main as red; alarm `if` is: {condition!r}"
        )
