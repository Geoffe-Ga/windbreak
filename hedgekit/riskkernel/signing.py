"""The approval-token signing key handle -- the sole-importer boundary target.

SPEC S5.3 reserves the approval-token signing key to the Risk Kernel alone: no
package outside :mod:`hedgekit.riskkernel` may ever import this module. That
boundary is enforced today by the pure-``ast`` scanner in
``tests/riskkernel/test_process_isolation.py`` and documented as a
forbidden-modules contract in ``plans/architecture/.importlinter``.

This issue ships only the *shape* of the handle: a key-material-free stub that
holds no bytes and whose :meth:`SigningKeyHandle.sign` is not yet implemented.
The real key loading and Ed25519 signing land in a later issue.
"""

from __future__ import annotations


class SigningKeyHandle:
    """An isolated handle to the approval-token signing key.

    The handle is a pure stub in this issue: it stores no key material and
    performs no cryptography. It exists so the SPEC S5.3 import boundary has a
    concrete target to reserve to the Risk Kernel package.
    """

    def sign(self, payload: bytes) -> bytes:
        """Sign a payload with the approval-token key.

        Args:
            payload: The bytes to sign.

        Returns:
            The detached signature over ``payload`` (once implemented).

        Raises:
            NotImplementedError: Always, in this issue -- signing logic and key
                loading land in a later issue; only the isolated, key-material-
                free handle shape ships now.
        """
        del payload  # No signing logic ships in this issue.
        raise NotImplementedError(
            "approval-token signing lands in a later issue; SigningKeyHandle "
            "is a key-material-free stub for now"
        )
