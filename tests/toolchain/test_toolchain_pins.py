"""Failing-first tests pinning the toolchain-alignment contract (issue #104).

These tests encode the target state for issue #104: a single top-level
`constraints-quality.txt` exact-pin file acts as the version authority for
every cross-context quality tool, ruff-format is the sole formatter
authority (black and isort are removed from every gate: pre-commit,
requirements-dev.txt, and pyproject.toml's `dev` extra), pre-commit hook
`rev`s track the constraints pins exactly (pre-commit cannot read a
constraints file, so its `rev`s and `additional_dependencies` must be kept
in lockstep by hand), and CI installs via `-c constraints-quality.txt` and
runs `./scripts/check-all.sh` instead of an unpinned `pip install ...`
line with silently swallowed failures.

None of this exists in the repository yet: `constraints-quality.txt` is
absent, black/isort are still wired into pre-commit, requirements-dev.txt,
and pyproject.toml, the pre-commit `rev`s are stale relative to nothing (no
constraints file to compare against), and `.github/workflows/ci.yml` still
has the legacy unpinned install. Every assertion below therefore fails for
the reason documented in its own docstring -- that is the expected Gate 1
RED state.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSTRAINTS_PATH = _REPO_ROOT / "constraints-quality.txt"
_REQUIREMENTS_DEV_PATH = _REPO_ROOT / "requirements-dev.txt"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_PRECOMMIT_PATH = _REPO_ROOT / ".pre-commit-config.yaml"
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Cross-context quality tools that must carry an exact pin in
#: constraints-quality.txt. Only names are asserted, never versions, so this
#: test never needs updating when a pin is bumped.
_REQUIRED_TOOL_NAMES = (
    "ruff",
    "mypy",
    "types-PyYAML",
    "types-requests",
    "bandit",
    "pip-audit",
    "radon",
    "xenon",
    "pre-commit",
    "mutmut",
    "pytest",
    "pytest-cov",
    "pytest-xdist",
    "pytest-asyncio",
    "pytest-mock",
    "pytest-timeout",
    "hypothesis",
    "coverage",
)

#: Maps a pre-commit repo URL substring to the constraints-quality.txt
#: package name whose exact pin its `rev` must equal.
_VERSION_AUTHORITATIVE_REPOS = {
    "astral-sh/ruff-pre-commit": "ruff",
    "pre-commit/mirrors-mypy": "mypy",
    "PyCQA/bandit": "bandit",
    "pypa/pip-audit": "pip-audit",
}

#: A strict exact-pin requirement line: `name==version`, optional extras.
_EXACT_PIN_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]*(?:\[[A-Za-z0-9_,\-]+\])?)"
    r"==(?P<version>[A-Za-z0-9_.\-]+)$"
)

#: Extracts a bare package name from the start of a requirements-style
#: line, stopping at the first version-specifier or extras character.
_NAME_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*")

#: The formatters issue #104 removes from every gate in favor of ruff-format.
_BANNED_FORMATTERS = {"black", "isort"}


def _normalize(name: str) -> str:
    """Normalize a package name for case/separator-insensitive comparison.

    Args:
        name: A raw package name (e.g. "types-PyYAML", "Pip_Audit").

    Returns:
        The name lower-cased with underscores folded to hyphens.
    """
    return name.strip().lower().replace("_", "-")


def _non_comment_lines(text: str) -> list[str]:
    """Return every non-blank, non-comment line, stripped.

    Args:
        text: The full contents of a requirements-style text file.

    Returns:
        Each meaningful line with surrounding whitespace removed.
    """
    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Drop a trailing inline comment (pip requires whitespace before the
        # `#`), so an annotated pin such as `ruff==0.15.1  # reason` is still
        # recognized as an exact pin.
        stripped = re.split(r"\s+#", stripped, maxsplit=1)[0].strip()
        lines.append(stripped)
    return lines


def _parse_constraints() -> dict[str, str]:
    """Parse constraints-quality.txt into a name -> version pin mapping.

    Returns:
        A mapping from normalized package name to its exact pinned version.

    Raises:
        FileNotFoundError: If constraints-quality.txt does not exist yet.
        AssertionError: If any meaningful line is not an exact `==` pin.
    """
    text = _CONSTRAINTS_PATH.read_text(encoding="utf-8")
    pins: dict[str, str] = {}
    for line in _non_comment_lines(text):
        match = _EXACT_PIN_PATTERN.match(line)
        assert match is not None, f"not an exact pin: {line!r}"
        pins[_normalize(match.group("name"))] = match.group("version")
    return pins


def _requirement_names(text: str) -> set[str]:
    """Extract the set of normalized package names from a requirements file.

    Args:
        text: Full contents of a requirements-style text file.

    Returns:
        Normalized package names referenced by any non-comment line.
    """
    names = set()
    for line in _non_comment_lines(text):
        match = _NAME_PREFIX_PATTERN.match(line)
        if match:
            names.add(_normalize(match.group(0)))
    return names


def _pyproject_dev_dependencies() -> list[str]:
    """Return the raw `[project.optional-dependencies].dev` entries.

    Returns:
        The dependency specifier strings declared for the `dev` extra.
    """
    with _PYPROJECT_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["optional-dependencies"]["dev"])


def _pyproject_dev_names() -> set[str]:
    """Return normalized package names from the `dev` optional-dependencies.

    Returns:
        Normalized package names extracted from each specifier string.
    """
    names = set()
    for entry in _pyproject_dev_dependencies():
        match = _NAME_PREFIX_PATTERN.match(entry.strip())
        if match:
            names.add(_normalize(match.group(0)))
    return names


def _load_precommit() -> dict[str, Any]:
    """Parse `.pre-commit-config.yaml` with `yaml.safe_load`.

    Returns:
        The parsed top-level pre-commit mapping.
    """
    with _PRECOMMIT_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _precommit_repo_entries() -> list[dict[str, Any]]:
    """Return the `repos:` list from the pre-commit config.

    Returns:
        Each repo mapping (with its `repo`, `rev`, and `hooks` keys).
    """
    return list(_load_precommit()["repos"])


def _precommit_hook_ids() -> set[str]:
    """Return every hook `id` declared across all pre-commit repos.

    Returns:
        The set of hook id strings from every repo's `hooks:` list.
    """
    ids = set()
    for repo in _precommit_repo_entries():
        for hook in repo.get("hooks", []):
            ids.add(hook["id"])
    return ids


def _find_repo(url_substring: str) -> dict[str, Any] | None:
    """Find the first pre-commit repo whose `repo` URL contains a substring.

    Args:
        url_substring: A distinguishing fragment of the repo URL, e.g.
            "astral-sh/ruff-pre-commit".

    Returns:
        The matching repo mapping, or None if no repo matches.
    """
    for repo in _precommit_repo_entries():
        if url_substring in repo.get("repo", ""):
            return repo
    return None


def _mypy_additional_dependencies() -> list[str]:
    """Return the mypy hook's `additional_dependencies` list.

    Returns:
        The raw specifier strings attached to the mypy hook.

    Raises:
        AssertionError: If no mirrors-mypy repo/hook is configured at all.
    """
    repo = _find_repo("mirrors-mypy")
    assert repo is not None, "no mirrors-mypy repo configured in pre-commit"
    for hook in repo["hooks"]:
        if hook["id"] == "mypy":
            return list(hook.get("additional_dependencies", []))
    raise AssertionError("mirrors-mypy repo has no mypy hook")


def test_constraints_quality_file_exists() -> None:
    """A top-level constraints-quality.txt exact-pin file must exist."""
    assert _CONSTRAINTS_PATH.is_file(), (
        f"{_CONSTRAINTS_PATH} does not exist yet -- issue #104 needs a "
        "single top-level exact-pin authority for every quality tool"
    )


def test_every_constraints_line_is_an_exact_pin() -> None:
    """Every non-comment line in constraints-quality.txt is a `==` pin.

    `_parse_constraints` asserts this internally (rejecting `>=`, `~=`,
    `<`, `>`, or unpinned lines) as it builds the mapping, so simply
    building it -- and confirming it produced at least one pin -- is the
    check.
    """
    pins = _parse_constraints()

    assert pins, "constraints-quality.txt produced no parsed pins"


def test_constraints_covers_every_required_tool_name() -> None:
    """Every cross-context quality tool has a pin, by name (not version)."""
    pins = _parse_constraints()

    missing = [name for name in _REQUIRED_TOOL_NAMES if _normalize(name) not in pins]
    assert not missing, f"constraints-quality.txt is missing pins for: {missing}"


@pytest.mark.parametrize(
    ("repo_url_substring", "constraints_name"),
    sorted(_VERSION_AUTHORITATIVE_REPOS.items()),
)
def test_precommit_rev_matches_constraints_pin(
    repo_url_substring: str, constraints_name: str
) -> None:
    """Version-authoritative pre-commit `rev`s equal their constraints pin."""
    pins = _parse_constraints()
    repo = _find_repo(repo_url_substring)

    assert repo is not None, f"no pre-commit repo matches {repo_url_substring!r}"
    rev = str(repo["rev"]).removeprefix("v")
    expected = pins[_normalize(constraints_name)]
    assert rev == expected


def test_mypy_hook_pins_types_stubs_to_constraints_exactly() -> None:
    """mypy's additional_dependencies pin types-PyYAML/types-requests exactly."""
    pins = _parse_constraints()
    deps = _mypy_additional_dependencies()

    dep_pins: dict[str, str] = {}
    for dep in deps:
        match = _EXACT_PIN_PATTERN.match(dep.strip())
        if match:
            dep_pins[_normalize(match.group("name"))] = match.group("version")

    for stub_name in ("types-PyYAML", "types-requests"):
        key = _normalize(stub_name)
        assert key in dep_pins, (
            f"mypy additional_dependencies has no exact `==` pin for {stub_name}"
        )
        assert dep_pins[key] == pins[key]


def test_black_and_isort_absent_from_precommit_hooks() -> None:
    """Neither black nor isort is configured as a pre-commit repo/hook."""
    hook_ids = _precommit_hook_ids()
    repo_urls = " ".join(repo.get("repo", "") for repo in _precommit_repo_entries())

    assert not (hook_ids & _BANNED_FORMATTERS)
    assert "psf/black" not in repo_urls
    assert "PyCQA/isort" not in repo_urls


def test_black_and_isort_absent_from_requirements_dev() -> None:
    """requirements-dev.txt references neither black nor isort."""
    text = _REQUIREMENTS_DEV_PATH.read_text(encoding="utf-8")

    names = _requirement_names(text)
    assert not (names & _BANNED_FORMATTERS)


def test_black_and_isort_absent_from_pyproject_dev_extra() -> None:
    """pyproject.toml's `[project.optional-dependencies].dev` has no black/isort."""
    names = _pyproject_dev_names()

    assert not (names & _BANNED_FORMATTERS)


def test_requirements_dev_references_constraints_file() -> None:
    """requirements-dev.txt installs are constrained by constraints-quality.txt."""
    text = _REQUIREMENTS_DEV_PATH.read_text(encoding="utf-8")

    assert "constraints-quality.txt" in text


def test_ci_references_constraints_file_and_check_all_script() -> None:
    """ci.yml installs with `-c constraints-quality.txt` and runs check-all.sh."""
    text = _CI_PATH.read_text(encoding="utf-8")

    assert "constraints-quality.txt" in text
    assert "./scripts/check-all.sh" in text


def test_ci_has_no_legacy_unpinned_install_line() -> None:
    """The old unpinned `pip install pytest ... mypy ...` line is gone."""
    text = _CI_PATH.read_text(encoding="utf-8")

    legacy = (
        "pip install pytest pytest-cov pytest-xdist ruff pylint mypy bandit pip-audit"
    )
    assert legacy not in text


def test_ci_requirements_install_has_no_silent_failure_suppression() -> None:
    """No `pip install -r requirements...` line is masked with `|| true`."""
    text = _CI_PATH.read_text(encoding="utf-8")

    offending = [
        line
        for line in text.splitlines()
        if "pip install -r requirements" in line and "|| true" in line
    ]
    assert not offending, f"install line(s) silently swallow failures: {offending}"
