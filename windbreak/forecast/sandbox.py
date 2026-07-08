"""SPEC S8.3 bounded research sandbox: the forecast engine's egress firewall.

SPEC S8.3 requires the forecast engine's web-research stage to be able to reach
a small, explicit allowlist of hosts and *nothing else* -- no ledger, no order
gateway, no config, no arbitrary network. This module makes that boundary
**structural rather than prompt-based**: instead of instructing a model "please
only fetch from these hosts" (a request a prompt-injected tool call can ignore),
the only capabilities a research step is ever handed are the three methods on a
slots-closed :class:`ResearchTools` object, and :meth:`ResearchTools.fetch`
allowlists by exact, parsed hostname before it will call its transport. A caller
(or a hijacked tool-use turn) cannot smuggle in a fourth capability, cannot
widen the allowlist, and cannot escape the on-disk cache jail, because those
constraints live in code the model never gets to rewrite.

Three seams enforce the boundary:

* **Egress allowlist** -- :meth:`ResearchTools.fetch` parses the URL with
  :func:`urllib.parse.urlsplit`, takes the real ``hostname`` (which already
  excludes any ``user@`` userinfo prefix), lowercases it, and refuses any
  non-http(s) scheme, missing host, or off-allowlist host with
  :class:`EgressDeniedError`. This mirrors the scheme/redirect-host refusals in
  :mod:`windbreak.connector.kalshi.client`.
* **Path jail** -- :class:`ResearchCache` resolves every candidate write path
  and refuses (:class:`SandboxPathViolationError`) anything that lands outside its
  root, defeating absolute names, ``..`` traversal, and symlink escapes.
* **Final capability surface** -- :func:`tool_registry` exposes exactly
  ``{"search", "fetch", "verify_citation"}`` as a read-only mapping, and
  ``verify_citation`` is a reserved slot that fails closed until issue #26
  ships its verification logic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

#: The only URL schemes egress is ever permitted for (SPEC S8.3): plain
#: http(s), never ``file://``, ``ftp://``, or any other privileged scheme.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Ordinal of the first printable ASCII character (space, ``0x20``). Anything
#: below it is a C0 control character; a well-formed URL percent-encodes such
#: bytes, so their raw presence is a parser-differential red flag.
_MIN_PRINTABLE_ORD = 0x20

#: Ordinal of the ASCII DEL control character (``0x7f``), the one control
#: codepoint that sorts *above* the printable range and so is checked by hand.
_DEL_ORD = 0x7F

#: Suffix stamped on every cached fetch payload; the stem is a sha256 digest of
#: the source URL, so distinct URLs cache to distinct, collision-free files.
_CACHE_FILE_SUFFIX = ".txt"


class EgressDeniedError(Exception):
    """Raised when a fetch targets a scheme or host outside the allowlist."""


class SandboxPathViolationError(Exception):
    """Raised when a cache write would escape the sandbox's on-disk root."""


class SearchTransport(Protocol):
    """The seam through which candidate URLs are obtained for a query."""

    def search(self, query: str) -> tuple[str, ...]:
        """Return candidate URLs for ``query``.

        Args:
            query: The subquestion text to search for.

        Returns:
            The candidate URLs, most relevant first.
        """
        ...


class FetchTransport(Protocol):
    """The seam through which a single URL's content is retrieved."""

    def fetch(self, url: str) -> str:
        """Return the textual content at ``url``.

        Args:
            url: The URL to retrieve.

        Returns:
            The retrieved content.
        """
        ...


def _has_unsafe_url_chars(url: str) -> bool:
    """Return whether ``url`` holds any control or whitespace character.

    :func:`urllib.parse.urlsplit` silently *strips* tab/newline/carriage-return
    (and tolerates leading control/space bytes, CVE-2023-24329) while computing
    the ``hostname`` this sandbox's allowlist gate trusts -- but the raw ``url``
    string is later handed verbatim to the fetch transport. A byte the gate's
    parser drops and the transport's parser keeps is a classic
    parse-differential SSRF escape, so any such byte must fail closed *before*
    parsing rather than let the two parsers disagree on the real host.

    Args:
        url: The candidate URL to screen.

    Returns:
        ``True`` if any character is ASCII whitespace, a C0 control byte, or
        DEL -- none of which belong, unencoded, in a well-formed URL.
    """
    return any(
        char.isspace() or ord(char) < _MIN_PRINTABLE_ORD or ord(char) == _DEL_ORD
        for char in url
    )


def _cache_filename(url: str) -> str:
    """Derive a collision-free cache filename from a source URL.

    Args:
        url: The URL whose fetched content is being cached.

    Returns:
        A ``<sha256-hex>.txt`` filename, a deterministic function of ``url``.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{digest}{_CACHE_FILE_SUFFIX}"


class ResearchCache:
    """A write-jailed cache for fetched research payloads.

    Every write is confined to ``root``: names are resolved (following any
    symlinks) and rejected with :class:`SandboxPathViolationError` unless the
    resolved candidate is inside the resolved root, so absolute names, ``..``
    traversal, and symlink escapes all fail closed.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        """Initialize the cache over its jail root.

        Args:
            root: The directory every stored file must live under.
        """
        self._root = root

    def _is_within_root(self, candidate: Path) -> bool:
        """Return whether ``candidate`` resolves to a path *strictly* inside the root.

        Both the candidate and the root are fully resolved (following
        symlinks) before the containment test, and containment is checked with
        :meth:`~pathlib.PurePath.is_relative_to` on those resolved paths -- not
        a string prefix -- so sibling roots like ``/cache`` and ``/cache2`` can
        never be confused. The resolved candidate must also differ from the
        resolved root itself: an empty or ``.`` name resolves *onto* the root
        directory, and letting that through would clobber the jail dir (writing
        a file where the cache root should be), so it is refused as out of jail.

        Args:
            candidate: The prospective write path, relative to or under root.

        Returns:
            ``True`` if the resolved candidate lies strictly within the resolved
            root (never equal to it).
        """
        resolved_root = self._root.resolve()
        resolved_candidate = candidate.resolve()
        return (
            resolved_candidate != resolved_root
            and resolved_candidate.is_relative_to(resolved_root)
        )

    def store(self, name: str, content: str) -> Path:
        """Write ``content`` to ``name`` under the root, or fail closed.

        Args:
            name: The relative path (under the root) to write to.
            content: The text to persist.

        Returns:
            The path the content was written to.

        Raises:
            SandboxPathViolationError: If ``name`` is absolute, traverses out of the
                root, resolves (via a symlink) outside it, or resolves onto the
                root directory itself. The message names the offending path.
        """
        # ``Path.joinpath`` (not the ``/`` operator) keeps this module clear of
        # the no-float lint (``scripts/lint_no_floats.py``), which reads a bare
        # ``/`` as banned true division on the probability/money path.
        candidate = self._root.joinpath(name)
        if Path(name).is_absolute() or not self._is_within_root(candidate):
            raise SandboxPathViolationError(
                f"cache path {name!r} escapes the sandbox root {self._root}"
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        return candidate


class ResearchTools:
    """The final, capability-closed set of tools a research step may use.

    The only public methods are :meth:`search`, :meth:`fetch`, and the reserved
    :meth:`verify_citation`; a flat ``__slots__`` blocks any fourth attribute
    from being attached. The allowlist and both transports are private, so a
    caller cannot widen the egress policy or swap a transport after
    construction -- the boundary is structural, not advisory.
    """

    __slots__ = ("_allowed_hosts", "_cache", "_fetch_transport", "_search_transport")

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        search_transport: SearchTransport,
        fetch_transport: FetchTransport,
        cache: ResearchCache,
    ) -> None:
        """Initialize the capability object from its private collaborators.

        Args:
            allowed_hosts: The lowercased egress allowlist.
            search_transport: The seam returning candidate URLs for a query.
            fetch_transport: The seam returning content for an allowed URL.
            cache: The write-jailed cache fetched content is persisted to.
        """
        self._allowed_hosts = allowed_hosts
        self._search_transport = search_transport
        self._fetch_transport = fetch_transport
        self._cache = cache

    def search(self, query: str) -> tuple[str, ...]:
        """Return candidate URLs for ``query`` via the search transport.

        Args:
            query: The subquestion text to search for.

        Returns:
            The candidate URLs the search transport returned.
        """
        return self._search_transport.search(query)

    def fetch(self, url: str) -> str:
        """Fetch ``url`` if allowlisted, persist the content, and return it.

        The URL is first screened for control/whitespace bytes (which would let
        this gate's parser and the transport's parser disagree on the real
        host), then parsed with :func:`urllib.parse.urlsplit`; its ``hostname``
        (already stripped of any ``user@`` userinfo) is lowercased and checked
        against the allowlist. Only then is the fetch transport called and the
        content cached. The match is host-only: a port is intentionally
        unconstrained (SPEC S8.3 allowlists by host), so any port on an
        allowlisted host is permitted.

        Args:
            url: The URL to fetch.

        Returns:
            The fetched content, verbatim.

        Raises:
            EgressDeniedError: If the URL contains a control or whitespace
                character, the scheme is not http(s), the host is missing, or
                the host is not on the allowlist. The message names the host.
        """
        if _has_unsafe_url_chars(url):
            raise EgressDeniedError(
                f"egress denied: control or whitespace character in URL {url!r}"
            )
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise EgressDeniedError(
                f"egress denied: scheme {parts.scheme!r} in {url!r}"
            )
        hostname = parts.hostname
        if not hostname:
            raise EgressDeniedError(f"egress denied: no host in {url!r}")
        host = hostname.lower()
        if host not in self._allowed_hosts:
            raise EgressDeniedError(f"egress denied: host {host!r} is not allowlisted")
        content = self._fetch_transport.fetch(url)
        self._cache.store(_cache_filename(url), content)
        return content

    def verify_citation(self) -> NoReturn:
        """Reserved verification capability -- deferred to issue #26.

        The capability surface is final now, so ``verify_citation`` is present
        in the registry, but no verification logic ships in issue #24.

        Raises:
            NotImplementedError: Always, referencing the deferred issue #26.
        """
        raise NotImplementedError(
            "verify_citation is reserved for issue #26 (citation verification)"
        )


def tool_registry(tools: ResearchTools) -> Mapping[str, Callable[..., object]]:
    """Return the read-only tool registry backed by ``tools``.

    The mapping surface is exactly ``{"search", "fetch", "verify_citation"}``
    and is wrapped in :class:`types.MappingProxyType`, so a caller can neither
    add a fourth capability nor rebind an existing one.

    Args:
        tools: The capability object whose bound methods back the registry.

    Returns:
        A read-only mapping of tool name to the bound method implementing it.
    """
    tools_by_name: dict[str, Callable[..., object]] = {
        "search": tools.search,
        "fetch": tools.fetch,
        "verify_citation": tools.verify_citation,
    }
    return MappingProxyType(tools_by_name)


def build_research_tools(
    *,
    allowed_hosts: Iterable[str],
    cache_dir: Path,
    search_transport: SearchTransport,
    fetch_transport: FetchTransport,
) -> ResearchTools:
    """Assemble a sandboxed :class:`ResearchTools` from its collaborators.

    Args:
        allowed_hosts: The hosts egress is permitted for; normalized to a
            lowercased frozenset so matching is case-insensitive.
        cache_dir: The root the fetch cache is jailed to.
        search_transport: The seam returning candidate URLs for a query.
        fetch_transport: The seam returning content for an allowed URL.

    Returns:
        A capability-closed :class:`ResearchTools` over the given seams.
    """
    normalized_hosts = frozenset(host.lower() for host in allowed_hosts)
    cache = ResearchCache(root=cache_dir)
    return ResearchTools(
        allowed_hosts=normalized_hosts,
        search_transport=search_transport,
        fetch_transport=fetch_transport,
        cache=cache,
    )
