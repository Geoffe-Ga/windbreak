"""The approval-token signing key handle -- the sole-importer boundary target.

SPEC S5.3 reserves the approval-token signing key to the Risk Kernel alone: no
package outside :mod:`windbreak.riskkernel` may ever import this module. That
boundary is enforced today by the pure-``ast`` scanner in
``tests/riskkernel/test_process_isolation.py`` and documented as a
forbidden-modules contract in ``plans/architecture/.importlinter``.

The handle wraps injectable key material and signs with HMAC-SHA256 (SPEC
S10.6's "HMAC/signature" family). The #29 skeleton's "Ed25519" guess is
superseded: this module does *symmetric* HMAC, not an asymmetric signature, so
the same shared key both signs (here) and verifies (in the Gateway-consumable
:mod:`windbreak.tokens` package). The key bytes are held only in a private
attribute -- no public, non-callable attribute exposes them, :meth:`__repr__`
is redacted, and pickling is blocked (:meth:`__getstate__` raises) so the key
can never be serialized to rest outside this boundary.
:meth:`SigningKeyHandle.from_env` is the injected loading seam a future
EPIC_01 keyring replaces; it reads only an environment mapping, never a config
file.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Minimum admissible key length, in bytes. 32 bytes (256 bits) matches the
#: HMAC-SHA256 output/security level; anything shorter is rejected fail-closed.
_MIN_KEY_BYTES = 32


class SigningKeyHandle:
    """An isolated handle to the approval-token signing key (HMAC-SHA256).

    Construction validates the key length and stores the material only in the
    private ``_key`` attribute. The handle exposes no public attribute holding
    the raw bytes, redacts its ``repr``, and refuses to be pickled.
    ``__slots__`` keeps the instance ``__dict__``-free so the key can never be
    surfaced via ``vars(handle)`` either -- defense in depth on top of the
    redacted ``repr`` and blocked pickling.
    """

    __slots__ = ("_key",)

    def __init__(self, key_material: bytes) -> None:
        """Wrap injectable key material, validating its minimum length.

        Args:
            key_material: The raw signing key, at least :data:`_MIN_KEY_BYTES`
                bytes long.

        Raises:
            ValueError: If ``key_material`` is shorter than 32 bytes.
        """
        if len(key_material) < _MIN_KEY_BYTES:
            raise ValueError(
                f"signing key must be at least {_MIN_KEY_BYTES} bytes, "
                f"got {len(key_material)}"
            )
        self._key = key_material

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        var: str = "WINDBREAK_APPROVAL_TOKEN_KEY",
    ) -> SigningKeyHandle:
        """Load a handle from a hex-encoded environment variable, fail-closed.

        Reads ``var`` from ``environ`` (defaulting to :data:`os.environ`),
        hex-decodes it, and constructs a handle. A missing variable, an
        undecodable value, or a decodable-but-too-short key each raise
        :class:`ValueError` -- the loader never falls back to an insecure or
        absent key. This is the injected seam a future keyring replaces; it
        never reads a config file.

        Args:
            environ: The environment mapping to read from. Defaults to
                :data:`os.environ`.
            var: The variable name holding the hex-encoded key material.

        Returns:
            A validated :class:`SigningKeyHandle`.

        Raises:
            ValueError: If ``var`` is absent, its value is not valid hex, or the
                decoded key is shorter than 32 bytes.
        """
        source = os.environ if environ is None else environ
        raw = source.get(var)
        if raw is None:
            raise ValueError(f"missing environment variable {var}")
        try:
            key_material = bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError(f"{var} is not valid hex") from exc
        return cls(key_material)

    def sign(self, payload: bytes) -> bytes:
        """Return the HMAC-SHA256 digest of ``payload`` under the handle's key.

        Args:
            payload: The bytes to sign.

        Returns:
            The 32-byte HMAC-SHA256 tag over ``payload``.
        """
        return hmac.new(self._key, payload, hashlib.sha256).digest()

    def __repr__(self) -> str:
        """Return a redacted representation that never exposes key bytes."""
        return f"{type(self).__name__}(<redacted>)"

    def __getstate__(self) -> object:
        """Block pickling so the key material cannot be serialized to rest.

        Raises:
            TypeError: Always -- a serialized handle would place the key
                material outside its SPEC S5.3 access boundary.
        """
        raise TypeError(f"{type(self).__name__} is not picklable")
