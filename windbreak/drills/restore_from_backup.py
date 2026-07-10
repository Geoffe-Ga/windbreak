"""The ``restore-from-backup`` drill (issue #59).

Proves that restoring the hash-chained ledger from a backup reproduces the exact
same derived operational state. The drill copies the backup ledger into a fresh
scratch directory (simulating a restore), verifies the copy's hash chain, then
rebuilds *both* the original and the copy into two further scratch directories
and asserts every derived read-model file is byte-identical between them.

A corrupt backup (a tampered byte) surfaces as a
:class:`~windbreak.ledger.store.ChainIntegrityError` from ``verify_chain``,
which the drill turns into a :class:`~windbreak.drills.framework.DrillFailedError`
carrying the offending ``sequence_number`` -- never a silently-passed corrupt
restore. The drill composes only the shipped
:class:`~windbreak.ledger.store.SqliteLedgerStore` verification and
:func:`~windbreak.ledger.rebuild.rebuild`; it adds no new backup logic.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, ClassVar, cast

from windbreak.drills.framework import Drill, DrillFailedError, DrillPreconditionError
from windbreak.ledger.rebuild import rebuild
from windbreak.ledger.store import ChainIntegrityError, SqliteLedgerStore

if TYPE_CHECKING:
    from pathlib import Path

    from windbreak.drills.context import DrillContext

#: The filename the backup ledger is read from under ``ctx.fixture_dir``.
_LEDGER_DB_FILENAME = "ledger.db"


class RestoreFromBackupDrill(Drill):
    """Restore the ledger from a backup and prove derived state is identical."""

    name: ClassVar[str] = "restore-from-backup"

    def check_preconditions(self, ctx: object) -> None:
        """Verify the backup ledger fixture exists before restoring it.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to inspect.

        Raises:
            DrillPreconditionError: If the backup ledger fixture is absent.
        """
        context = cast("DrillContext", ctx)
        backup = context.fixture_dir / _LEDGER_DB_FILENAME
        if not backup.exists():
            raise DrillPreconditionError(
                f"restore-from-backup requires a backup ledger at {backup}"
            )

    def execute(self, ctx: object) -> dict[str, object]:
        """Restore the backup, verify its chain, and diff the rebuilt state.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to run
                against.

        Returns:
            Evidence recording the byte-identical read-model comparison.

        Raises:
            DrillFailedError: If the backup's chain is broken (carrying the offending
                ``sequence_number``), or the rebuilt read models diverge.
        """
        context = cast("DrillContext", ctx)
        backup = context.fixture_dir / _LEDGER_DB_FILENAME
        restored = context.tmp_dir_factory() / _LEDGER_DB_FILENAME
        shutil.copy2(backup, restored)
        self._verify_chain(restored)
        original_models = context.tmp_dir_factory()
        restored_models = context.tmp_dir_factory()
        rebuild(backup, original_models)
        rebuild(restored, restored_models)
        return self._diff_read_models(original_models, restored_models)

    def teardown(self, ctx: object) -> None:
        """No teardown: the drill only writes into caller-owned scratch dirs.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` (unused).
        """
        del ctx

    def _verify_chain(self, ledger_path: Path) -> None:
        """Verify a restored ledger's hash chain, failing the drill on tamper.

        Args:
            ledger_path: The restored ledger database to verify.

        Raises:
            DrillFailedError: If verification finds a broken chain, carrying the
                offending ``sequence_number``.
        """
        store = SqliteLedgerStore(ledger_path)
        try:
            store.verify_chain()
        except ChainIntegrityError as error:
            raise DrillFailedError(
                {
                    "restored_chain_valid": False,
                    "sequence_number": error.sequence_number,
                }
            ) from error
        finally:
            store.close()

    def _diff_read_models(
        self, original_models: Path, restored_models: Path
    ) -> dict[str, object]:
        """Assert every rebuilt read-model file is byte-identical across dirs.

        Args:
            original_models: The directory the original ledger rebuilt into.
            restored_models: The directory the restored copy rebuilt into.

        Returns:
            Evidence naming the compared files and confirming identity.

        Raises:
            DrillFailedError: If any read-model file differs between the two dirs.
        """
        filenames = sorted(path.name for path in original_models.glob("*.json"))
        mismatches = [
            name
            for name in filenames
            if (original_models / name).read_bytes()
            != (restored_models / name).read_bytes()
        ]
        if mismatches:
            raise DrillFailedError(
                {"read_models_identical": False, "diverging_files": mismatches}
            )
        return {
            "read_models_identical": True,
            "compared_files": len(filenames),
        }
