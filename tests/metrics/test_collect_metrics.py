"""Failing-first tests for scripts/collect_metrics.py (issue #113).

``scripts/collect_metrics.py`` currently crashes on import:

    from start_green_stay_green.generators.metrics import (
        ci_status,
        count_ci_jobs,
        count_precommit_hooks,
        precommit_status,
    )

That scaffold package is not a windbreak dependency -- CI's clean
``pip install -e .`` never installs it, so every invocation of
``collect_metrics.py`` in CI raises ``ModuleNotFoundError`` before a single
line of the module body runs. The fix (owned by the implementation
specialist) deletes that import and implements the four names natively as
module-level helpers in ``collect_metrics.py``.

Environment gotcha: on THIS machine, ``start_green_stay_green`` happens to
be pip-installed from a sibling checkout
(``/Users/geoffgallinger/Projects/start_green_stay_green``), and that
package genuinely does define ``ci_status``/``count_ci_jobs``/
``count_precommit_hooks``/``precommit_status``. A naive "just import the
module" test would therefore spuriously pass locally while CI stays red.
Every test below that needs a loaded module goes through
``_load_with_scaffold_blocked``, which poisons ``sys.modules`` so the
scaffold package is unimportable regardless of what happens to be
installed locally -- reproducing CI's clean-environment failure
deterministically.

Pinned contract (reverse-engineered from docs/metrics.json and the call
sites in ``collect_metrics.py`` itself -- see ``collect_ci_status``,
``collect_precommit_status``, and the module docstring's "Issue #159"/
"Issue #154" references):

* ``count_ci_jobs(workflows_dir)`` sums the number of entries under each
  workflow file's top-level ``jobs:`` mapping across ``*.yml``/``*.yaml``.
* ``count_precommit_hooks(config_path)`` sums ``len(repo["hooks"])`` across
  the ``.pre-commit-config.yaml`` ``repos:`` list.
* ``ci_status(total_jobs, passing_jobs=None, *, run_url=None)`` treats a
  missing ``passing_jobs`` as "unknown" (stored as 0), not as "all failed".
* ``precommit_status(total_hooks)`` treats configured hooks as passing
  (running ``pre-commit run --all-files`` is redundant with CI), degrading
  to "unknown" only when there are zero hooks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "collect_metrics.py"


def _load_with_scaffold_blocked(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load ``collect_metrics.py`` by path with the scaffold import poisoned.

    Setting a ``sys.modules`` entry to ``None`` forces Python's import
    machinery to raise on any subsequent ``import``/``from ... import`` of
    that dotted name (or a name nested under it), regardless of whether the
    real package is actually installed. This reproduces CI's clean
    environment -- where ``start_green_stay_green`` is simply absent --
    even on a machine where the sibling scaffold checkout is pip-installed.

    Args:
        monkeypatch: Pytest's monkeypatch fixture, used so the poisoned
            ``sys.modules`` entries are restored after the test.

    Returns:
        The freshly executed ``collect_metrics`` module.
    """
    monkeypatch.setitem(sys.modules, "start_green_stay_green", None)
    monkeypatch.setitem(sys.modules, "start_green_stay_green.generators", None)
    monkeypatch.setitem(sys.modules, "start_green_stay_green.generators.metrics", None)
    spec = importlib.util.spec_from_file_location("collect_metrics", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def metrics_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Provide collect_metrics loaded with the scaffold import blocked.

    Every function-contract test below depends on this fixture so the
    whole suite proves *native*, standalone behaviour once the fix lands --
    not scaffold behaviour that happens to be masked in this environment.
    """
    return _load_with_scaffold_blocked(monkeypatch)


# --- Primary regression: import must survive without the scaffold --------------


def test_module_imports_without_scaffold_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collect_metrics must load even when the scaffold package is absent.

    Regression guard for issue #113: ``scripts/collect_metrics.py`` must
    define ``ci_status``/``count_ci_jobs``/``count_precommit_hooks``/
    ``precommit_status`` natively rather than importing them from the dead
    ``start_green_stay_green.generators.metrics`` scaffold. With that
    scaffold poisoned in ``sys.modules``, loading the script would re-raise
    ``ModuleNotFoundError`` if the import ever crept back.
    """
    module = _load_with_scaffold_blocked(monkeypatch)

    assert module is not None


def test_script_source_contains_no_scaffold_reference() -> None:
    """Durable guard: the dead scaffold import must not creep back in.

    Reads the script's source text directly (no import involved) so this
    assertion is independent of whatever ``sys.modules`` poisoning tricks
    the other tests use.
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "start_green_stay_green" not in source


# --- count_ci_jobs ---------------------------------------------------------------


def test_count_ci_jobs_sums_jobs_across_yml_and_yaml_files(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """Two jobs in a .yml file plus one job in a .yaml file sum to 3."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "ci.yml").write_text(
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    (workflows_dir / "lint.yaml").write_text(
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )

    assert metrics_module.count_ci_jobs(workflows_dir) == 3


def test_count_ci_jobs_file_without_jobs_key_contributes_zero(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A workflow file with no top-level `jobs:` key contributes zero."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "no_jobs.yml").write_text("name: NoJobs\n", encoding="utf-8")

    assert metrics_module.count_ci_jobs(workflows_dir) == 0


def test_count_ci_jobs_non_mapping_jobs_contributes_zero(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A `jobs:` value that is a list, not a mapping, is not counted."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "listy.yml").write_text(
        "jobs:\n  - build\n  - test\n", encoding="utf-8"
    )

    assert metrics_module.count_ci_jobs(workflows_dir) == 0


def test_count_ci_jobs_malformed_yaml_contributes_zero_and_does_not_raise(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A syntactically broken workflow file is skipped, never raised."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "broken.yml").write_text("jobs: [unterminated\n", encoding="utf-8")

    assert metrics_module.count_ci_jobs(workflows_dir) == 0


def test_count_ci_jobs_missing_dir_returns_zero(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A nonexistent workflows directory counts as zero jobs, not an error."""
    missing_dir = tmp_path / "does-not-exist"

    assert metrics_module.count_ci_jobs(missing_dir) == 0


def test_count_ci_jobs_honesty_check_real_repo(
    metrics_module: types.ModuleType,
) -> None:
    """The repo's own `.github/workflows` genuinely has at least one job."""
    workflows_dir = REPO_ROOT / ".github" / "workflows"

    assert metrics_module.count_ci_jobs(workflows_dir) > 0


# --- count_precommit_hooks --------------------------------------------------------


def test_count_precommit_hooks_sums_hooks_across_repos(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """Two repos contributing 3 and 1 hooks respectively sum to 4."""
    config_path = tmp_path / "pre-commit-config.yaml"
    config_path.write_text(
        "repos:\n"
        "- repo: https://example.com/repo-one\n"
        "  hooks:\n"
        "  - id: hook-a\n"
        "  - id: hook-b\n"
        "  - id: hook-c\n"
        "- repo: https://example.com/repo-two\n"
        "  hooks:\n"
        "  - id: hook-d\n",
        encoding="utf-8",
    )

    assert metrics_module.count_precommit_hooks(config_path) == 4


def test_count_precommit_hooks_missing_file_returns_zero(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A nonexistent config path counts as zero hooks, not an error."""
    missing = tmp_path / "does-not-exist.yaml"

    assert metrics_module.count_precommit_hooks(missing) == 0


def test_count_precommit_hooks_empty_file_returns_zero(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """An empty config file (`yaml.safe_load` -> None) counts as zero hooks."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")

    assert metrics_module.count_precommit_hooks(empty) == 0


def test_count_precommit_hooks_malformed_yaml_returns_zero_and_does_not_raise(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A syntactically broken config file is tolerated, never raised."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("repos: [unterminated\n", encoding="utf-8")

    assert metrics_module.count_precommit_hooks(broken) == 0


def test_count_precommit_hooks_repo_without_hooks_key_is_tolerated(
    metrics_module: types.ModuleType, tmp_path: Path
) -> None:
    """A repo entry missing its `hooks:` key counts as zero for that repo."""
    config_path = tmp_path / "pre-commit-config.yaml"
    config_path.write_text(
        "repos:\n"
        "- repo: https://example.com/repo-with-hooks\n"
        "  hooks:\n"
        "  - id: only-hook\n"
        "- repo: https://example.com/repo-without-hooks\n",
        encoding="utf-8",
    )

    assert metrics_module.count_precommit_hooks(config_path) == 1


def test_count_precommit_hooks_honesty_check_real_repo(
    metrics_module: types.ModuleType,
) -> None:
    """The repo's own `.pre-commit-config.yaml` genuinely has hooks."""
    config_path = REPO_ROOT / ".pre-commit-config.yaml"

    assert metrics_module.count_precommit_hooks(config_path) > 0


# --- ci_status ---------------------------------------------------------------------


def test_ci_status_static_path_defaults_to_unknown(
    metrics_module: types.ModuleType,
) -> None:
    """With no `passing_jobs` given, status is `unknown`, never a verdict.

    Pins the exact regression a naive `passing_jobs >= total_jobs` (with
    `passing_jobs` defaulting to 0) implementation would get wrong: 0 is
    not a real pass count, it is "we don't know", and must render as
    `unknown` rather than `failing`.
    """
    result = metrics_module.ci_status(27)

    assert result["total_jobs"] == 27
    assert result["passing_jobs"] == 0
    assert result["percentage"] == 0.0
    assert result["status"] == "unknown"
    # The static card also carries the always-present run_url key (None here),
    # matching the API path so the #68 dashboard can treat it as nullable.
    assert result["run_url"] is None


def test_ci_status_zero_total_jobs_is_unknown(
    metrics_module: types.ModuleType,
) -> None:
    """Zero total jobs is `unknown` regardless of a supplied passing count."""
    result = metrics_module.ci_status(0, 0)

    assert result["status"] == "unknown"
    assert result["percentage"] == 0.0


def test_ci_status_all_passing(metrics_module: types.ModuleType) -> None:
    """4 of 4 jobs passing is 100% and `passing`."""
    result = metrics_module.ci_status(4, 4)

    assert result["percentage"] == 100.0
    assert result["status"] == "passing"


def test_ci_status_partial_passing_is_failing(
    metrics_module: types.ModuleType,
) -> None:
    """3 of 4 jobs passing is 75% and `failing` -- not all jobs are green."""
    result = metrics_module.ci_status(4, 3)

    assert result["percentage"] == 75.0
    assert result["status"] == "failing"


def test_ci_status_percentage_rounds_to_one_decimal(
    metrics_module: types.ModuleType,
) -> None:
    """1 of 3 jobs renders as 33.3, neither truncated nor a long float."""
    result = metrics_module.ci_status(3, 1)

    assert result["percentage"] == 33.3


def test_ci_status_run_url_present_when_provided(
    metrics_module: types.ModuleType,
) -> None:
    """A supplied `run_url` is stored verbatim."""
    result = metrics_module.ci_status(4, 4, run_url="http://x")

    assert result["run_url"] == "http://x"


def test_ci_status_run_url_key_present_and_none_when_omitted(
    metrics_module: types.ModuleType,
) -> None:
    """`run_url` key always exists, defaulting to None when not supplied."""
    result = metrics_module.ci_status(4, 4)

    assert "run_url" in result
    assert result["run_url"] is None


# --- precommit_status --------------------------------------------------------------


def test_precommit_status_all_configured_hooks_pass(
    metrics_module: types.ModuleType,
) -> None:
    """31 configured hooks render as 100% `passing` with an exact key set."""
    result = metrics_module.precommit_status(31)

    assert result == {
        "total_hooks": 31,
        "passing_hooks": 31,
        "percentage": 100.0,
        "status": "passing",
    }


def test_precommit_status_zero_hooks_is_unknown(
    metrics_module: types.ModuleType,
) -> None:
    """Zero configured hooks degrades to `unknown`, not `passing`."""
    result = metrics_module.precommit_status(0)

    assert result == {
        "total_hooks": 0,
        "passing_hooks": 0,
        "percentage": 0.0,
        "status": "unknown",
    }


# --- CLI surface smoke test ---------------------------------------------------------


def test_cli_surface_wires_up_after_import_fix(
    metrics_module: types.ModuleType,
) -> None:
    """`main`/`_build_parser` exist and the minimal CLI invocation parses."""
    assert hasattr(metrics_module, "main")
    assert hasattr(metrics_module, "_build_parser")

    args = metrics_module._build_parser().parse_args(["--project-name", "x"])

    assert args.project_name == "x"
