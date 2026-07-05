"""Static guard: the ledger package's SQL never mutates or deletes rows.

Enforces the append-only invariant at the source level -- independent of
runtime behavior -- so a future edit can't quietly introduce an UPDATE,
DELETE, REPLACE, or DROP statement against the ledger table. Deliberately
reads the *expected* implementation module paths (rather than globbing
what happens to exist) so a not-yet-implemented module fails this test
with a clear `FileNotFoundError` instead of silently scanning zero files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: SQL tokens forbidden anywhere in the ledger package's source, since the
#: ledger is append-only: rows are inserted and read, never changed or
#: removed.
_FORBIDDEN_SQL_TOKENS = ("UPDATE", "DELETE", "REPLACE", "DROP")

#: The ledger package's implementation modules, named explicitly (not
#: globbed) so a missing module fails loudly rather than being silently
#: skipped by an empty glob result.
_LEDGER_SOURCE_FILENAMES = ("__init__.py", "events.py", "store.py", "rebuild.py")

_LEDGER_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "hedgekit" / "ledger"


def _ledger_source_paths() -> list[Path]:
    """Return the full paths to every expected ledger source module."""
    return [_LEDGER_PACKAGE_DIR / name for name in _LEDGER_SOURCE_FILENAMES]


def test_all_expected_ledger_source_modules_exist() -> None:
    """Every implementation module this suite scans is present on disk."""
    for path in _ledger_source_paths():
        assert path.is_file(), f"expected ledger source file to exist: {path}"


@pytest.mark.parametrize("token", _FORBIDDEN_SQL_TOKENS)
def test_ledger_source_contains_no_forbidden_sql_token(token: str) -> None:
    """No ledger source file contains an UPDATE/DELETE/REPLACE/DROP SQL token."""
    pattern = re.compile(rf"\b{token}\b", re.IGNORECASE)

    offending = [
        str(path)
        for path in _ledger_source_paths()
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert not offending, f"forbidden SQL token {token!r} found in: {offending}"
