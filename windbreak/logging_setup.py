"""Structured, secret-redacting logging for windbreak.

``configure_logging`` installs a process-wide :class:`logging.StreamHandler`
carrying a :class:`JsonFormatter` (one JSON object per line) and a
:class:`RedactionFilter` (which scrubs anything that looks like a secret, by
key name or value shape) so no credential ever reaches the log stream in the
clear -- on any logger under the root, not just ``windbreak`` itself.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from typing import TextIO

#: The marker substituted in place of any redacted secret.
REDACTED: Final = "[REDACTED]"

#: Field-name substrings that mark a log value as a secret to drop wholesale.
#:
#: Tokens are deliberately *specific* (e.g. ``access_token`` rather than a bare
#: ``token``, ``authorization`` rather than a bare ``auth``) so that legitimate
#: observability fields -- ``prompt_tokens``, ``token_count``, ``tokenizer``,
#: ``author``, ``authority`` -- are not over-redacted. ``llm_api_key`` is still
#: covered by the ``api_key`` token.
DENYLIST_KEY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "authorization",
        "credential",
        "private_key",
        "access_token",
        "refresh_token",
        "api_token",
        "auth_token",
        "id_token",
        "bearer_token",
        "session_token",
    }
)

#: Value-shape patterns that mark text as containing a secret to redact.
_SECRET_PATTERNS: Final = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+\S+"),
)


def _standard_logrecord_attrs() -> frozenset[str]:
    """Return the attribute names carried by every :class:`logging.LogRecord`.

    The set is the attributes present on a freshly built record plus the two
    names the stdlib formatter injects during ``format`` (``message`` and
    ``asctime``); any attribute outside it is caller-supplied ``extra``.

    Returns:
        The frozenset of standard LogRecord attribute names.
    """
    bare = logging.LogRecord("", logging.NOTSET, "", 0, "", None, None)
    return frozenset(vars(bare)) | {"message", "asctime"}


#: The attribute names that are intrinsic to a LogRecord (not caller extras).
_STANDARD_LOGRECORD_ATTRS: Final[frozenset[str]] = _standard_logrecord_attrs()


def is_denylisted_key(key: str) -> bool:
    """Return whether a log field name looks like a secret by its name.

    Args:
        key: The field name to test, in any case.

    Returns:
        True if the lowercased key contains any denylisted token.
    """
    lowered = key.lower()
    return any(token in lowered for token in DENYLIST_KEY_TOKENS)


def redact_text(text: str) -> str:
    """Replace every secret-shaped substring in ``text`` with ``REDACTED``.

    Args:
        text: Arbitrary text that may embed an ``sk-...`` token or a
            ``Bearer ...`` header.

    Returns:
        The text with each secret pattern replaced by ``REDACTED``.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _redact_value(value: object) -> object:
    """Redact a log value by its shape, recursing into nested containers.

    A secret hidden inside a nested ``dict``/``list``/``tuple`` must not slip
    through in the clear, so redaction recurses instead of only touching
    top-level strings.

    Args:
        value: The value to redact. Strings are pattern-redacted; ``dict``
            values are redacted per key (so a denylisted nested key drops its
            value wholesale); ``list``/``tuple`` elements are redacted in place;
            non-string scalars (int/float/bool/None) pass through untouched.

    Returns:
        The redacted value, preserving ``dict``/``list``/``tuple`` shape.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_field(key, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        redacted = [_redact_value(item) for item in value]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


def _redact_field(key: object, value: object) -> object:
    """Redact one log field by key name and value shape.

    Args:
        key: The field name; a denylisted (string) name redacts the value
            wholesale. A non-string key is never treated as denylisted, but its
            value is still shape-redacted.
        value: The field value; redacted by :func:`_redact_value` (which
            recurses into nested containers) unless the key is denylisted.

    Returns:
        ``REDACTED`` for a denylisted key, otherwise the shape-redacted value.
    """
    if isinstance(key, str) and is_denylisted_key(key):
        return REDACTED
    return _redact_value(value)


class RedactionFilter(logging.Filter):
    """Mutate log records in place to scrub secrets before they are emitted.

    Installed as a handler-level filter so redaction applies to every logger
    routed through the configured handler, not just ``windbreak``'s own.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record's message and extra fields; never suppress it.

        Args:
            record: The record to mutate. Its rendered message is
                pattern-redacted (args are collapsed into it) and each
                caller-supplied ``extra`` field is either dropped to
                ``REDACTED`` (a denylisted key) or shape-redacted -- strings
                are pattern-redacted and nested ``dict``/``list``/``tuple``
                values are recursed into so no nested secret leaks. Non-string
                scalars pass through untouched.

        Returns:
            Always True -- the filter scrubs but never drops records.
        """
        record.msg = redact_text(record.getMessage())
        record.args = None
        for key, value in list(vars(record).items()):
            if key in _STANDARD_LOGRECORD_ATTRS:
                continue
            setattr(record, key, _redact_field(key, value))
        return True


def _format_timestamp(created: float) -> str:
    """Format a record timestamp as ISO-8601 UTC with a trailing ``Z``.

    Args:
        created: The record's ``created`` epoch timestamp.

    Returns:
        A string like ``2026-07-04T12:00:00.000000Z``.
    """
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Collect caller-supplied ``extra`` fields from a record.

    Args:
        record: The record whose non-standard attributes to gather.

    Returns:
        A mapping of every attribute outside the standard LogRecord set,
        excluding ``component`` (surfaced as a top-level field instead).
    """
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD_LOGRECORD_ATTRS and key != "component"
    }


class JsonFormatter(logging.Formatter):
    """Render each log record as a single-line JSON object.

    Every line carries ``ts`` (ISO-8601 UTC, microsecond precision),
    ``level``, ``component`` (the ``component`` extra when present, else the
    logger name), ``msg``, and any additional caller-supplied ``extra``
    fields. Exception records gain a redacted ``exc_info`` string.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a record to a JSON line.

        Args:
            record: The record to serialize.

        Returns:
            A JSON object string (the handler appends the newline).
        """
        payload: dict[str, object] = {
            "ts": _format_timestamp(record.created),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "msg": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_info"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(
    *, level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    """Install a process-wide JSON, secret-redacting logging handler.

    Args:
        level: The root logging level; records below it are dropped.
        stream: The text stream to write JSON lines to. Defaults to
            ``sys.stderr``.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logging.basicConfig(force=True, handlers=[handler], level=level)
