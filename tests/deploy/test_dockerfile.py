"""Failing-first tests for the container image skeleton (issue #15, RED).

`Dockerfile` does not exist yet at the repository root, so `_read_dockerfile`
raises `FileNotFoundError` and every test below fails for that reason. Once
the implementation specialist adds it, these tests pin two SPEC-relevant
guarantees: the image drops root privilege via a non-root `USER` directive,
and its `CMD` actually launches `hedgekit run` (not a shell, not left unset).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE_PATH = _REPO_ROOT / "Dockerfile"


def _read_dockerfile() -> str:
    """Read the repository-root `Dockerfile` as text.

    Returns:
        The Dockerfile's full text contents.

    Raises:
        FileNotFoundError: If no `Dockerfile` exists at the repo root yet.
    """
    return _DOCKERFILE_PATH.read_text(encoding="utf-8")


def _instruction_lines(text: str, keyword: str) -> list[str]:
    """Return every non-comment Dockerfile line starting with `keyword`.

    Args:
        text: The full Dockerfile text.
        keyword: The Dockerfile instruction keyword to match (e.g. "USER").

    Returns:
        Each matching line, stripped of leading/trailing whitespace.
    """
    matches = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split(maxsplit=1)[0].upper() == keyword:
            matches.append(stripped)
    return matches


def test_dockerfile_exists_at_repo_root() -> None:
    """A `Dockerfile` is present at the repository root."""
    assert _DOCKERFILE_PATH.is_file()


def test_declares_a_non_root_user() -> None:
    """At least one `USER` directive switches away from root.

    A container that never drops root is a privilege-escalation risk should
    the process be compromised; `USER root` (or its numeric UID `0`) does
    not count as dropping privilege.
    """
    text = _read_dockerfile()

    user_lines = _instruction_lines(text, "USER")
    assert user_lines, "Dockerfile has no USER directive"

    last_user = user_lines[-1].split(maxsplit=1)[1].strip()
    assert last_user not in {"root", "0"}


def test_cmd_invokes_hedgekit_run() -> None:
    """The image's `CMD` launches `hedgekit run`, not a bare shell or nothing."""
    text = _read_dockerfile()

    cmd_lines = _instruction_lines(text, "CMD")
    assert cmd_lines, "Dockerfile has no CMD directive"

    last_cmd = cmd_lines[-1]
    assert "hedgekit" in last_cmd
    assert "run" in last_cmd
