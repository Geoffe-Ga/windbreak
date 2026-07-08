"""Tests for windbreak.logging_setup (issue #14): structured, redacted logs.

`configure_logging` installs a process-wide StreamHandler carrying a
`JsonFormatter` and a `RedactionFilter`. These tests pin two independent
contracts:

1. Every emitted line is a single JSON object with `ts`/`level`/`component`/
   `msg` plus any extras -- observable by any consumer that just reads
   stderr and calls `json.loads`.
2. Nothing that looks like a secret -- either by key name (the denylist) or
   by value shape (an `sk-...` token or a `Bearer ...` header) -- ever
   reaches that stream in the clear, on *any* logger under the root, not
   just `windbreak` itself.

None of `windbreak.logging_setup`'s public names exist yet, so importing
this module fails at collection with `ModuleNotFoundError` -- the expected
RED state for issue #14's Gate 1.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import TYPE_CHECKING

import pytest

from windbreak.logging_setup import (
    DENYLIST_KEY_TOKENS,
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    is_denylisted_key,
    redact_text,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_ISO_UTC_MICROS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.fixture
def restore_logging_state() -> Iterator[None]:
    """Snapshot and restore root-logger handlers/level around a test.

    `configure_logging` uses `logging.basicConfig(force=True, ...)`, which
    tears down whatever handlers were previously installed on the root
    logger. Without this fixture, one test's `configure_logging` call would
    leak its handler into every subsequent test in the suite.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)


def _first_payload(raw: str) -> dict[str, object]:
    """Parse the first non-empty line of captured output as JSON."""
    lines = [line for line in raw.splitlines() if line]
    assert lines, "expected at least one emitted log line"
    return json.loads(lines[0])


class TestIsDenylistedKey:
    """Unit tests for the standalone `is_denylisted_key` predicate."""

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "apikey",
            "access_token",
            "secret",
            "password",
            "passwd",
            "authorization",
            "credential",
            "private_key",
            "auth_token",
            "llm_api_key",
            "AUTH_TOKEN",
            "Some-Secret-Value",
        ],
    )
    def test_matches_known_denylist_substrings(self, key: str) -> None:
        """A key containing any denylisted token (any case) is flagged."""
        assert is_denylisted_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "access_token",
            "refresh_token",
            "api_token",
            "auth_token",
            "id_token",
            "bearer_token",
            "session_token",
            "authorization",
            "llm_api_key",
        ],
    )
    def test_specific_secret_key_forms_are_denylisted(self, key: str) -> None:
        """The tightened denylist still flags every specific secret key form."""
        assert is_denylisted_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "prompt_tokens",
            "token_count",
            "tokenizer",
            "author",
            "authority",
        ],
    )
    def test_observability_fields_are_not_over_redacted(self, key: str) -> None:
        """Legitimate fields resembling `token`/`auth` are not flagged as secrets."""
        assert is_denylisted_key(key) is False

    @pytest.mark.parametrize("key", ["order_id", "count", "user", "note", "level"])
    def test_rejects_non_denylisted_keys(self, key: str) -> None:
        """A key with no denylisted substring is not flagged."""
        assert is_denylisted_key(key) is False

    def test_denylist_key_tokens_is_a_frozenset_of_expected_tokens(self) -> None:
        """DENYLIST_KEY_TOKENS is immutable and covers the documented tokens."""
        expected = {
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
        assert isinstance(DENYLIST_KEY_TOKENS, frozenset)
        assert expected == DENYLIST_KEY_TOKENS


class TestRedactText:
    """Unit tests for the standalone `redact_text` pattern-substitution."""

    def test_replaces_sk_prefixed_secret_with_redacted_marker(self) -> None:
        """An `sk-...` token embedded in text is replaced with REDACTED."""
        result = redact_text("prefix sk-abcDEF12345678 suffix")

        assert result == f"prefix {REDACTED} suffix"

    def test_replaces_bearer_header_with_redacted_marker(self) -> None:
        """A `Bearer <token>` header is replaced with REDACTED."""
        result = redact_text("Authorization: Bearer abc.def-XYZ")

        assert result == f"Authorization: {REDACTED}"

    def test_bearer_matching_is_case_sensitive(self) -> None:
        """Lowercase `bearer` is not matched -- only exact-case `Bearer`."""
        text = "authorization: bearer abc.def-XYZ"

        assert redact_text(text) == text

    def test_leaves_text_without_secrets_unchanged(self) -> None:
        """Text containing no secret pattern passes through unmodified."""
        assert redact_text("hello world, order #42") == "hello world, order #42"

    def test_redacted_constant_value(self) -> None:
        """REDACTED is the literal marker "[REDACTED]"."""
        assert REDACTED == "[REDACTED]"


class TestRedactionFilter:
    """Unit tests for `RedactionFilter` against bare `logging.LogRecord`s."""

    def test_filter_always_returns_true(self) -> None:
        """`filter()` never suppresses a record -- it only mutates it."""
        record = logging.LogRecord(
            "name", logging.INFO, __file__, 1, "hello", None, None
        )

        assert RedactionFilter().filter(record) is True

    def test_filter_never_raises_on_non_string_message(self) -> None:
        """A non-string `record.msg` (e.g. an int) must not crash the filter."""
        record = logging.LogRecord("name", logging.INFO, __file__, 1, 12345, None, None)

        assert RedactionFilter().filter(record) is True


class TestJsonFormatter:
    """Unit tests for `JsonFormatter.format` against bare `LogRecord`s."""

    def test_format_returns_json_with_required_keys(self) -> None:
        """The formatted string is valid JSON with ts/level/component/msg."""
        record = logging.LogRecord(
            "comp", logging.WARNING, __file__, 10, "msg text", None, None
        )

        payload = json.loads(JsonFormatter().format(record))

        assert payload["level"] == "WARNING"
        assert payload["component"] == "comp"
        assert payload["msg"] == "msg text"
        assert _ISO_UTC_MICROS.match(payload["ts"])


class TestConfigureLogging:
    """End-to-end tests for `configure_logging` writing to an injected stream."""

    def test_emits_exactly_one_json_line_per_log_call(
        self, restore_logging_state: None
    ) -> None:
        """A single `logger.info` call produces exactly one JSON line."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("hello world")

        lines = [line for line in stream.getvalue().splitlines() if line]
        assert len(lines) == 1
        assert json.loads(lines[0])["msg"] == "hello world"

    def test_ts_matches_iso8601_utc_microsecond_pattern(
        self, restore_logging_state: None
    ) -> None:
        """`ts` is an ISO-8601 UTC timestamp with microseconds and a Z suffix."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("x")

        payload = _first_payload(stream.getvalue())
        assert _ISO_UTC_MICROS.match(payload["ts"])

    def test_component_defaults_to_logger_name(
        self, restore_logging_state: None
    ) -> None:
        """`component` defaults to the emitting logger's dotted name."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("hi")

        payload = _first_payload(stream.getvalue())
        assert payload["component"] == "windbreak.test"

    def test_component_extra_overrides_logger_name(
        self, restore_logging_state: None
    ) -> None:
        """`extra={"component": ...}` overrides the logger-name default."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("hi", extra={"component": "config"})

        payload = _first_payload(stream.getvalue())
        assert payload["component"] == "config"

    def test_respects_explicit_level_argument(
        self, restore_logging_state: None
    ) -> None:
        """Records below the configured level are dropped entirely."""
        stream = io.StringIO()
        configure_logging(level=logging.WARNING, stream=stream)
        logger = logging.getLogger("windbreak.test")

        logger.info("hidden")
        logger.warning("shown")

        lines = [line for line in stream.getvalue().splitlines() if line]
        assert len(lines) == 1
        assert json.loads(lines[0])["msg"] == "shown"

    def test_defaults_to_stderr_when_no_stream_given(
        self, restore_logging_state: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Omitting `stream` writes JSON lines to `sys.stderr`."""
        configure_logging()

        logging.getLogger("windbreak.test").info("hi")

        captured = capsys.readouterr()
        payload = _first_payload(captured.err)
        assert payload["msg"] == "hi"

    def test_redacts_denylisted_extra_keys_to_the_redacted_marker(
        self, restore_logging_state: None
    ) -> None:
        """Denylisted extra keys are replaced wholesale with REDACTED."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info(
            "auth attempt",
            extra={
                "llm_api_key": "sk-secret123456",
                "password": "hunter2",
                "authorization": "Bearer abc.def",
            },
        )

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert payload["llm_api_key"] == REDACTED
        assert payload["password"] == REDACTED
        assert payload["authorization"] == REDACTED
        assert "sk-secret123456" not in raw
        assert "hunter2" not in raw
        assert "Bearer abc.def" not in raw

    def test_redacts_denylisted_key_nested_inside_dict_extra(
        self, restore_logging_state: None
    ) -> None:
        """A denylisted key nested inside a dict extra is redacted, not leaked."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info(
            "resp",
            extra={
                "response": {
                    "authorization": "Bearer xyz987654",
                    "meta": {"api_key": "sk-DEEP12345678"},
                    "status": 200,
                    42: "numeric key survives",
                }
            },
        )

        raw = stream.getvalue()
        payload = _first_payload(raw)
        response = payload["response"]
        assert isinstance(response, dict)
        assert response["authorization"] == REDACTED
        assert response["status"] == 200
        # A non-string nested key is never treated as denylisted; its value is
        # still shape-redacted (here, left intact) and JSON stringifies the key.
        assert response["42"] == "numeric key survives"
        meta = response["meta"]
        assert isinstance(meta, dict)
        assert meta["api_key"] == REDACTED
        assert "xyz987654" not in raw
        assert "sk-DEEP12345678" not in raw

    def test_redacts_secret_tokens_inside_list_and_tuple_extras(
        self, restore_logging_state: None
    ) -> None:
        """Secret-shaped tokens inside list and tuple extras are redacted.

        A tuple is JSON-serialized as an array, so it reads back as a list;
        redaction still recurses through it before serialization.
        """
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info(
            "headers",
            extra={
                "headers": ["Bearer xyz123456", "sk-ABCDEFGH12345", "plain"],
                "trailers": ("sk-TRAIL12345678", "ok"),
            },
        )

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert payload["headers"] == [REDACTED, REDACTED, "plain"]
        assert payload["trailers"] == [REDACTED, "ok"]
        assert "Bearer xyz123456" not in raw
        assert "sk-ABCDEFGH12345" not in raw
        assert "sk-TRAIL12345678" not in raw

    def test_passes_through_observability_fields_that_resemble_secrets(
        self, restore_logging_state: None
    ) -> None:
        """Legitimate `token`/`auth`-like fields survive unredacted in the stream."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info(
            "usage", extra={"prompt_tokens": 1500, "author": "Ada"}
        )

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert payload["prompt_tokens"] == 1500
        assert payload["author"] == "Ada"
        assert "1500" in raw
        assert "Ada" in raw

    def test_redacts_secret_pattern_in_percent_style_message_args(
        self, restore_logging_state: None
    ) -> None:
        """A secret substituted via `%s` into the message is still redacted."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("key is %s", "sk-ABCDEF12345678")

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert "sk-ABCDEF12345678" not in raw
        assert REDACTED in payload["msg"]

    def test_redacts_bearer_token_in_literal_message(
        self, restore_logging_state: None
    ) -> None:
        """A `Bearer <token>` embedded directly in the message is redacted."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info("Authorization: Bearer eyJtoken")

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert "eyJtoken" not in raw
        assert REDACTED in payload["msg"]

    def test_redacts_secret_pattern_in_non_denylisted_extra_field(
        self, restore_logging_state: None
    ) -> None:
        """A non-denylisted extra field is pattern-redacted, not wholesale."""
        stream = io.StringIO()
        configure_logging(stream=stream)
        original = "uses sk-XYZ12345678"

        logging.getLogger("windbreak.test").info("note test", extra={"note": original})

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert payload["note"] == redact_text(original)
        assert "sk-XYZ12345678" not in raw

    def test_redacts_records_from_child_loggers_via_root_handler(
        self, restore_logging_state: None
    ) -> None:
        """Redaction applies process-wide, not only to the `windbreak` logger."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.some.child").info("token=sk-CHILD12345678")

        raw = stream.getvalue()
        assert "sk-CHILD12345678" not in raw
        assert REDACTED in raw

    def test_passes_through_non_secret_extras_unchanged(
        self, restore_logging_state: None
    ) -> None:
        """Extras that are neither denylisted keys nor secret-shaped pass through."""
        stream = io.StringIO()
        configure_logging(stream=stream)

        logging.getLogger("windbreak.test").info(
            "order placed", extra={"order_id": "abc123", "count": 3}
        )

        payload = _first_payload(stream.getvalue())
        assert payload["order_id"] == "abc123"
        assert payload["count"] == 3

    def test_exception_logging_includes_exc_info_and_redacts_traceback(
        self, restore_logging_state: None
    ) -> None:
        """`logger.exception` yields valid JSON with a redacted `exc_info`."""
        stream = io.StringIO()
        configure_logging(stream=stream)
        logger = logging.getLogger("windbreak.test")

        try:
            raise ValueError("token leak sk-TRACE1234567")
        except ValueError:
            logger.exception("failed")

        raw = stream.getvalue()
        payload = _first_payload(raw)
        assert "exc_info" in payload
        assert "sk-TRACE1234567" not in raw
        assert REDACTED in payload["exc_info"]
