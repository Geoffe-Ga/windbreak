"""Offline record/replay harness for the forecast engine's LLM calls (S8.9).

The forecast pipeline must run fully offline and deterministically in CI, so
every LLM completion flows through an :class:`LlmTransport` seam -- a
dependency-injection point modeled on
:class:`windbreak.connector.snapshot.EventLedgerWriter`. Three transports back
the three modes the tests exercise:

* :class:`RecordingCassette` wraps a real (or fake) transport, persisting each
  request/response pair to disk keyed by a stable request hash.
* :class:`ReplayCassette` serves recorded responses purely from disk and
  *fails closed* (:class:`CassetteMissError`) on any unrecorded request --
  never a live fallback.
* :class:`ForbiddenLiveTransport` always raises
  :class:`LiveCallForbiddenError`, a structural proof that a given run never
  reaches a live network.

Request hashing uses the ledger's canonical JSON form (sorted keys, no-space
separators) over sha256, re-implemented here with only the standard library
so this module stays dependency-free and float-free.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class CassetteMissError(Exception):
    """Raised when a replayed request has no recorded response (fail-closed)."""


class LiveCallForbiddenError(Exception):
    """Raised when a run attempts a forbidden live LLM call."""


def _canonical_json(obj: dict[str, str]) -> str:
    """Serialize a mapping to deterministic, whitespace-free JSON.

    Mirrors :func:`windbreak.ledger.events.canonical_json`: keys are sorted and
    separators carry no spaces, so the output is a byte-stable function of the
    mapping's contents alone.

    Args:
        obj: The mapping to serialize.

    Returns:
        The canonical JSON encoding of ``obj``.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """A single, hashable LLM completion request.

    Attributes:
        provider: The LLM provider identifier.
        model_version: The pinned model version string.
        prompt: The full prompt text.
    """

    provider: str
    model_version: str
    prompt: str

    def request_hash(self) -> str:
        """Return a stable sha256 hex digest of this request's fields.

        The digest is taken over the canonical JSON of ``{provider,
        model_version, prompt}``, so it is deterministic across processes and
        changes if and only if a field changes.

        Returns:
            A lowercase, 64-character sha256 hex digest.
        """
        canonical = _canonical_json(
            {
                "provider": self.provider,
                "model_version": self.model_version,
                "prompt": self.prompt,
            }
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LlmTransport(Protocol):
    """The seam through which a single LLM completion is obtained."""

    def complete(self, request: LlmRequest) -> str:
        """Return the completion text for ``request``.

        Args:
            request: The completion request.

        Returns:
            The completion response text.
        """
        ...


class ForbiddenLiveTransport:
    """An :class:`LlmTransport` that structurally forbids any live call."""

    def complete(self, request: LlmRequest) -> NoReturn:
        """Refuse the call, proving no stage reached a live network.

        Args:
            request: The (rejected) completion request.

        Raises:
            LiveCallForbiddenError: Always.
        """
        raise LiveCallForbiddenError(
            f"live LLM call forbidden for {request.provider}:{request.model_version}"
        )


class RecordingCassette:
    """An :class:`LlmTransport` that records each call to disk as it delegates.

    Delegates every completion to an underlying transport, accumulates the
    request/response pairs keyed by :meth:`LlmRequest.request_hash`, and
    rewrites the full mapping to ``path`` after each call so a replay cassette
    can be reloaded from it deterministically.
    """

    def __init__(self, *, transport: LlmTransport, path: Path) -> None:
        """Initialize the recorder.

        Args:
            transport: The underlying transport to delegate to.
            path: The file path the recorded mapping is written to.
        """
        self._transport = transport
        self._path = path
        self._entries: dict[str, dict[str, object]] = {}

    def complete(self, request: LlmRequest) -> str:
        """Delegate to the transport, record the pair, and persist to disk.

        Args:
            request: The completion request.

        Returns:
            The response returned by the underlying transport.
        """
        response = self._transport.complete(request)
        entry: dict[str, object] = {
            "request": {
                "provider": request.provider,
                "model_version": request.model_version,
                "prompt": request.prompt,
            },
            "response": response,
        }
        self._entries[request.request_hash()] = entry
        self._path.write_text(
            json.dumps(self._entries, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return response


def _reject_float(raw: str) -> NoReturn:
    """Reject any float leaf encountered while loading a cassette.

    Installed as ``json.loads(..., parse_float=...)`` so a cassette containing
    a float (e.g. ``temperature: 0.7``) fails loudly rather than smuggling a
    float onto the probability path.

    Args:
        raw: The raw float token text from the JSON parser.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"float leaf is banned in cassettes, got {raw!r}")


class ReplayCassette:
    """An :class:`LlmTransport` that serves recorded responses, fail-closed."""

    def __init__(self, entries: dict[str, str]) -> None:
        """Initialize the replayer.

        Args:
            entries: A mapping of request hash to recorded response text.
        """
        self._entries = entries

    @classmethod
    def from_path(cls, path: Path) -> ReplayCassette:
        """Load a recorded cassette file into a replayer.

        The file is parsed with a float-rejecting hook, so any float leaf
        raises :class:`ValueError`. Each top-level key is used verbatim as the
        replay lookup key, paired with its entry's ``response`` text.

        Args:
            path: The cassette file to load.

        Returns:
            A replayer serving the file's recorded responses.

        Raises:
            ValueError: If the cassette contains a float leaf.
        """
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=_reject_float)
        entries = {key: entry["response"] for key, entry in raw.items()}
        return cls(entries)

    def complete(self, request: LlmRequest) -> str:
        """Return the recorded response for ``request`` or fail closed.

        Args:
            request: The completion request.

        Returns:
            The recorded response text.

        Raises:
            CassetteMissError: If ``request`` has no recorded response.
        """
        key = request.request_hash()
        if key not in self._entries:
            raise CassetteMissError(f"no recorded response for request {key}")
        return self._entries[key]
