"""SPEC S8.5 prompt-injection defenses for untrusted web content (threat T1).

Bounded web research (``pipeline.bounded_web_research``) fetches attacker-
controlled pages. This module is the response-side firewall between those raw
bytes and any LLM prompt: it strips a page down to its *visible* text
(discarding ``<script>``/``<style>`` and CSS/ARIA-hidden payloads), neutralizes
any forged untrusted-data delimiter, caps the surviving excerpt at
:data:`MAX_QUOTE_WORDS` words, wraps that excerpt in an explicitly-labelled data
block (:func:`wrap_data_block`), and validates a model's *own* vote response for
delimiter forgery or a tool-call lure (:func:`validate_vote_response`).

The module is pure and stdlib-only (``html.parser.HTMLParser``, no new
dependency) and never touches a float -- it sits on the probability/money path
guarded by ``scripts/lint_no_floats.py``, so every operation here is a
string/int one.

The sanitize/raw-hash contract is deliberate. ``sanitize_content`` returns
*visible* text with hidden subtrees removed, while a citation's content hash
(``pipeline.bounded_web_research``) stays over the *raw* bytes and
``citations.verify_citation`` re-checks the sanitized quote as a raw substring.
So a page whose attack sits *after* the first :data:`MAX_QUOTE_WORDS` clean
words yields a quote that is still a contiguous raw substring (it verifies and
the run proceeds), whereas a page with a hidden span, a ``<script>`` block, or a
forged delimiter *inside* that first window has its removal break the raw-
substring property -- the citation then fails to verify and the run fails
closed, exactly the intended defense.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

#: Opening token of an untrusted-data block wrapping a web quote in a prompt.
DATA_BLOCK_BEGIN: Final = "<<<UNTRUSTED-DATA"

#: Closing token of an untrusted-data block wrapping a web quote in a prompt.
DATA_BLOCK_END: Final = "<<<END-UNTRUSTED-DATA>>>"

#: Maximum words retained in a sanitized quote excerpt (SPEC S8.5 length cap).
MAX_QUOTE_WORDS: Final = 25

#: Response-failure code: the model returned an empty/whitespace-only vote.
RESPONSE_FAILURE_EMPTY: Final = "empty_response"

#: Response-failure code: the model's response forges an untrusted-data
#: delimiter (an attempt to break out of, or fake, a data block).
RESPONSE_FAILURE_DELIMITER_FORGERY: Final = "delimiter_forgery"

#: Response-failure code: the model's response embeds a tool-call lure (a JSON
#: key that would coax a caller into dispatching an unrequested tool call).
RESPONSE_FAILURE_TOOL_CALL_LURE: Final = "tool_call_lure"

#: The JSON key tokens a tool-call lure uses; a vote response carrying any of
#: them is discarded rather than trusted (SPEC S8.5).
TOOL_CALL_MARKERS: frozenset[str] = frozenset(
    {'"tool"', '"tool_call"', '"function_call"'}
)

#: HTML elements whose entire text subtree is discarded during sanitization:
#: executable, styling, or off-DOM containers that never carry visible prose.
_SUPPRESSED_TAGS: frozenset[str] = frozenset(
    {"script", "style", "template", "noscript", "iframe"}
)

#: Void HTML elements: they have no text subtree, so they are never pushed onto
#: the open-element stack (they carry no content to suppress or keep).
_VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Space-stripped inline-``style`` fragments that mark an element as hidden; an
#: element whose ``style`` contains any of them has its subtree discarded.
_HIDDEN_STYLE_TOKENS: frozenset[str] = frozenset(
    {"display:none", "visibility:hidden", "font-size:0"}
)


@dataclass(frozen=True, slots=True)
class ResearchQuote:
    """One sanitized web quote paired with the URL it was fetched from.

    Attributes:
        url: The URL the sandbox actually fetched, never a URL parsed out of
            the page content -- so a citation-URL spoof buried in the text can
            never redirect where a quote claims to come from.
        text: The sanitized excerpt, at most :data:`MAX_QUOTE_WORDS` words.
    """

    url: str
    text: str


@dataclass(slots=True)
class _ElementFrame:
    """One open element on the sanitizer's tag stack.

    Attributes:
        tag: The (lowercased) element tag name.
        suppressed: Whether this element is a text-suppressing tag (script,
            style, and the like).
        hidden: Whether this element is hidden (via ``hidden``,
            ``aria-hidden``, or an inline ``style``).
    """

    tag: str
    suppressed: bool
    hidden: bool


def _style_hides(value: str | None) -> bool:
    """Return whether an inline ``style`` value marks its element hidden.

    Args:
        value: The raw ``style`` attribute value, or ``None`` for a valueless
            attribute.

    Returns:
        ``True`` if the space-stripped, lowercased style contains any hidden
        token (``display:none`` / ``visibility:hidden`` / ``font-size:0``).
    """
    if value is None:
        return False
    condensed = value.lower().replace(" ", "")
    return any(token in condensed for token in _HIDDEN_STYLE_TOKENS)


def _attrs_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """Return whether an element's attributes mark it visually hidden.

    Args:
        attrs: The element's ``(name, value)`` attributes as HTMLParser yields
            them (names already lowercased, values verbatim or ``None``).

    Returns:
        ``True`` if a bare ``hidden`` attribute, ``aria-hidden="true"``, or a
        hiding inline ``style`` is present.
    """
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "aria-hidden" and value is not None and value.lower() == "true":
            return True
        if name == "style" and _style_hides(value):
            return True
    return False


class _VisibleTextParser(HTMLParser):
    """An HTMLParser that collects only the *visible* character data of a page.

    Text inside a suppressed subtree (``<script>``, ``<style>``, ...) or a
    hidden element (``hidden`` / ``aria-hidden`` / a hiding inline ``style``)
    is dropped; everything else is retained. Entity/character references are
    auto-unescaped (``convert_charrefs=True``), so an entity-encoded delimiter
    forgery is decoded here and neutralized later by :func:`sanitize_content`.
    """

    def __init__(self) -> None:
        """Initialize an empty open-element stack and visible-text buffer."""
        super().__init__(convert_charrefs=True)
        self._stack: list[_ElementFrame] = []
        self._parts: list[str] = []

    @property
    def _hidden(self) -> bool:
        """Return whether any currently-open element suppresses its text.

        Returns:
            ``True`` if some open frame is a suppressed tag or a hidden
            element, so character data must not be collected.
        """
        return any(frame.suppressed or frame.hidden for frame in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Push a non-void element onto the stack, recording its visibility.

        Args:
            tag: The element's (lowercased) tag name.
            attrs: The element's attributes.
        """
        if tag in _VOID_TAGS:
            return
        self._stack.append(
            _ElementFrame(
                tag=tag,
                suppressed=tag in _SUPPRESSED_TAGS,
                hidden=_attrs_hidden(attrs),
            )
        )

    def handle_endtag(self, tag: str) -> None:
        """Pop back to the most recent matching start tag, if any.

        Popping the matched frame *and* any still-open frames nested inside it
        keeps the stack consistent against unclosed inner tags; a stray end tag
        with no matching start is ignored (tag-soup tolerant).

        Args:
            tag: The (lowercased) closing tag name.
        """
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Collect character data unless an open element suppresses it.

        Args:
            data: The character data between tags.
        """
        if not self._hidden:
            self._parts.append(data)

    def visible_text(self) -> str:
        """Return the concatenated visible character data collected so far.

        Returns:
            Every retained data run, joined in document order.
        """
        return "".join(self._parts)


def _collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace in ``text`` to single spaces.

    Args:
        text: The text to normalize.

    Returns:
        ``text`` with leading/trailing whitespace stripped and internal runs
        collapsed to one space each.
    """
    return " ".join(text.split())


def _neutralize_delimiters(text: str) -> str:
    """Replace every residual untrusted-data delimiter token with a space.

    A space is inserted (not an empty string) so two fragments that were only
    separated by a token can never abut and reassemble a fresh token.

    Args:
        text: The post-parse visible text.

    Returns:
        ``text`` with each :data:`DATA_BLOCK_BEGIN` / :data:`DATA_BLOCK_END`
        occurrence replaced by a single space.
    """
    return text.replace(DATA_BLOCK_BEGIN, " ").replace(DATA_BLOCK_END, " ")


def sanitize_content(content: str) -> str:
    """Reduce raw fetched page content to neutralized, visible text (S8.5).

    Fast identity path: content with no ``<`` and no ``&`` can carry neither
    HTML markup nor an entity nor a delimiter token (both delimiter tokens
    contain ``<``), so it is only whitespace-collapsed. Otherwise the content
    is parsed for *visible* text -- suppressed subtrees (``<script>`` and
    kin) and hidden elements are dropped, entities are unescaped -- and any
    delimiter token that survives (a literal or now-unescaped forgery) is
    neutralized before a final whitespace collapse.

    Args:
        content: The raw fetched page content.

    Returns:
        The neutralized, whitespace-collapsed visible text.
    """
    if "<" not in content and "&" not in content:
        return _collapse_whitespace(content)
    parser = _VisibleTextParser()
    parser.feed(content)
    parser.close()
    neutralized = _neutralize_delimiters(parser.visible_text())
    return _collapse_whitespace(neutralized)


def extract_quote(sanitized: str, *, max_words: int = MAX_QUOTE_WORDS) -> str:
    """Take at most ``max_words`` words from already-sanitized text (S8.5).

    Args:
        sanitized: The sanitized text (as returned by :func:`sanitize_content`).
        max_words: The maximum number of words to retain (defaults to
            :data:`MAX_QUOTE_WORDS`).

    Returns:
        The first ``max_words`` whitespace-separated words joined by single
        spaces; an empty string for empty input.
    """
    return " ".join(sanitized.split()[:max_words])


def _contains_delimiter(text: str) -> bool:
    """Return whether ``text`` contains either untrusted-data delimiter token.

    Args:
        text: The text to inspect.

    Returns:
        ``True`` if :data:`DATA_BLOCK_BEGIN` or :data:`DATA_BLOCK_END` appears.
    """
    return DATA_BLOCK_BEGIN in text or DATA_BLOCK_END in text


def wrap_data_block(*, url: str, quote: str) -> str:
    """Wrap a sanitized quote in a labelled untrusted-data block (S8.5).

    Args:
        url: The URL the quote was fetched from.
        quote: The sanitized quote excerpt.

    Returns:
        The quote framed between the opening and closing delimiter tokens, with
        the source URL on the opening line.

    Raises:
        ValueError: If ``url`` contains a delimiter token, a newline, or a
            double-quote character (any of which could break the opening
            line's structure), or if ``quote`` contains a delimiter token.
    """
    if _contains_delimiter(url) or "\n" in url or '"' in url:
        raise ValueError(
            f"url must not contain a delimiter token, a newline, or a "
            f"double-quote character: {url!r}"
        )
    if _contains_delimiter(quote):
        raise ValueError(f"quote must not contain a delimiter token: {quote!r}")
    return f'{DATA_BLOCK_BEGIN} url="{url}">>>\n{quote}\n{DATA_BLOCK_END}'


def validate_vote_response(response: str) -> str | None:
    """Screen a model's own vote response for injection artifacts (S8.5).

    Checks run in a fixed first-failure order: empty/whitespace-only, then a
    forged delimiter token, then a tool-call lure. The first failure wins.

    Args:
        response: The raw vote-completion text.

    Returns:
        A ``RESPONSE_FAILURE_*`` code for the first failing check, or ``None``
        when the response is clean.
    """
    if not response.strip():
        return RESPONSE_FAILURE_EMPTY
    if _contains_delimiter(response):
        return RESPONSE_FAILURE_DELIMITER_FORGERY
    if any(marker in response for marker in TOOL_CALL_MARKERS):
        return RESPONSE_FAILURE_TOOL_CALL_LURE
    return None
