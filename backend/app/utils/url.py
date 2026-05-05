"""LinkedIn URL validation and normalization helpers.

This module is the single source of truth for what constitutes a valid
LinkedIn profile URL across the Sales-Connections backend, and for how
raw user-supplied URLs are canonicalized into the form stored in the
``records.normalized_linkedin_url`` column.

Public API
----------
``is_valid_linkedin_url(url)``     Returns True iff *url* is a syntactically
                                   valid LinkedIn profile URL.
``normalize_linkedin_url(url)``    Returns the canonical form of *url*
                                   suitable for the unique partial index
                                   ``(org_id, normalized_linkedin_url)
                                   WHERE deleted_at IS NULL``.

Validation Rules (per AAP F-001, F-010)
---------------------------------------
A URL is considered a valid LinkedIn profile URL when ALL of the
following hold:

1. Scheme is ``http`` or ``https`` (case-insensitive).
2. Host is ``linkedin.com`` or ``<subdomain>.linkedin.com`` where
   ``<subdomain>`` is one of: ``www``, ``m``, or a two-or-three letter
   region code (``uk``, ``ca``, ``au``, ``in``, ``de``, ``fr``, ``br``,
   ``mx``, ``es``, ``it``, ``nl``, ``be``, ``ch``, ``at``, ``se``,
   ``no``, ``dk``, ``fi``, ``pl``, ``cz``, ``sk``, ``hu``, ``ru``,
   ``ua``, ``tr``, ``il``, ``ae``, ``sa``, ``za``, ``eg``, ``ng``,
   ``ke``, ``jp``, ``kr``, ``cn``, ``hk``, ``tw``, ``sg``, ``my``,
   ``th``, ``id``, ``ph``, ``vn``, ``nz``, ``cl``, ``ar``, ``co``,
   ``pe``, ``pt``, ``gr``, ``ie``).
3. Path begins with ``/in/`` or ``/pub/`` and contains at least one
   non-empty segment after that prefix.

Normalization Rules (per AAP F-010)
-----------------------------------
The canonical form returned by ``normalize_linkedin_url``:

- Scheme is forced to ``https``.
- Host is lowercased and any leading ``www.`` is stripped (so that
  ``www.linkedin.com`` and ``linkedin.com`` collapse to the same key).
- Path is preserved, but any trailing slash is removed.
- Query string is dropped entirely.
- Fragment is dropped entirely.
- Username segment of the path is lowercased so that
  ``/in/JaneDoe`` and ``/in/janedoe`` collapse to the same key
  (LinkedIn vanity URLs are case-insensitive on the server side).

Examples::

    >>> normalize_linkedin_url("https://www.LinkedIn.com/in/JaneDoe/")
    'https://linkedin.com/in/janedoe'
    >>> normalize_linkedin_url("http://uk.linkedin.com/in/jane-doe?utm=foo#bar")
    'https://uk.linkedin.com/in/jane-doe'
    >>> is_valid_linkedin_url("https://linkedin.com/in/jane-doe")
    True
    >>> is_valid_linkedin_url("https://example.com/in/jane")
    False

Module-level imports are limited to the standard library (``re``,
``urllib.parse``) per the assigned folder conventions.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Allowed LinkedIn host subdomains (per AAP F-001 + the assigned folder
# requirements). The bare host ``linkedin.com`` (no subdomain) is also
# accepted; this set holds the leading subdomain labels only.
#
# Two-letter ISO 3166-1 alpha-2 region codes that LinkedIn actively
# serves (non-exhaustive but covers the long tail of network exports
# observed in practice). ``www`` and ``m`` are the global aliases.
_LINKEDIN_SUBDOMAINS: frozenset[str] = frozenset(
    {
        "www",
        "m",
        "uk", "ca", "au", "in", "de", "fr", "br", "mx", "es", "it",
        "nl", "be", "ch", "at", "se", "no", "dk", "fi", "pl", "cz",
        "sk", "hu", "ru", "ua", "tr", "il", "ae", "sa", "za", "eg",
        "ng", "ke", "jp", "kr", "cn", "hk", "tw", "sg", "my", "th",
        "id", "ph", "vn", "nz", "cl", "ar", "co", "pe", "pt", "gr",
        "ie",
    }
)  # fmt: skip

# The canonical (post-normalization) host. ``normalize_linkedin_url``
# strips a leading ``www.`` and emits whichever subdomain remains
# (so the bare host or one of ``_LINKEDIN_SUBDOMAINS`` minus ``www``).
_LINKEDIN_BASE_HOST: str = "linkedin.com"

# The two LinkedIn URL families we accept. ``/in/`` is the modern public
# profile URL; ``/pub/`` is the legacy public profile URL still seen in
# exported address books and CRM records.
_LINKEDIN_PATH_PREFIXES: tuple[str, ...] = ("/in/", "/pub/")

# Allowed schemes for inbound URLs. ``normalize_linkedin_url`` always
# emits ``https`` regardless of the input scheme so that ``http://``
# and ``https://`` versions of the same profile collide on the unique
# partial index ``(org_id, normalized_linkedin_url)``.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Pre-compiled pattern for collapsing two-or-more consecutive ``/``
# characters in the path component. Pre-compilation matters at the
# 10K-record feed-render scale (per AAP section 0.7.3).
_MULTIPLE_SLASH_RE: re.Pattern[str] = re.compile(r"/{2,}")

# Pre-compiled pattern for the slug segment after ``/in/`` or ``/pub/``.
# A valid slug contains only URL-safe characters: ASCII alphanumerics,
# dot, underscore, hyphen, and the percent-encoding sigil. Unicode
# slugs ARE possible in principle but LinkedIn vanity URLs in practice
# stick to ASCII; users with non-ASCII in their profile URL receive a
# percent-encoded equivalent here.
_SLUG_SEGMENT_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._\-%]+$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "is_valid_linkedin_url",
    "normalize_linkedin_url",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _split_for_validation(
    url: str,
) -> tuple[str, str, str, str, str] | None:
    """Parse *url* and return ``(scheme, host_no_port, path, query, fragment)``.

    Returns ``None`` when the URL cannot be parsed at all (e.g.,
    ``None`` input, empty string, non-string type, or a value that
    :func:`urllib.parse.urlsplit` cannot decompose into the expected
    components).

    The returned host is **lowercased** and **stripped of any trailing
    port** (``linkedin.com:443`` -> ``linkedin.com``) so callers can
    compare against :data:`_LINKEDIN_SUBDOMAINS` directly.

    This helper does NOT perform business-rule validation; it only does
    the syntactic decomposition. Use :func:`is_valid_linkedin_url` for
    the full check.

    Args:
        url: The raw user input. May be of any type; non-string inputs
            are short-circuited to ``None``.

    Returns:
        A 5-tuple ``(scheme, host, path, query, fragment)`` on success
        or ``None`` on parse failure. All components are lowercase
        where appropriate (scheme and host) and never ``None``.
    """
    # The ``url: str`` annotation advertises the intended type to typed
    # callers, but this helper is also reached from boundary code where
    # the value originates outside our type system (raw JSON payloads,
    # tests passing ``None`` / ``42`` / ``bytes``). The defensive check
    # below is intentional; mypy considers the body unreachable under
    # strict typing, hence the targeted ignore on the return.
    if not isinstance(url, str):
        return None  # type: ignore[unreachable]
    stripped = url.strip()
    if not stripped:
        return None
    try:
        parts = urlsplit(stripped)
    except ValueError:
        # ``urlsplit`` raises ValueError on malformed IPv6 literals
        # (e.g., ``http://[::1`` or ``https://[invalid]``) and on
        # similarly broken inputs. Treat all such cases as unparseable.
        return None
    scheme = (parts.scheme or "").lower()
    # The ``hostname`` accessor performs port-stripping and lowercasing
    # internally, but is ``None`` for schemeless or otherwise invalid
    # URLs. Fall back to ``netloc`` and lowercase manually so callers
    # always receive a string. ``hostname`` may itself raise ValueError
    # on broken port specifications, so we wrap defensively.
    try:
        host_attr = parts.hostname
    except ValueError:
        host_attr = None
    host = (host_attr or parts.netloc or "").lower()
    # Strip any IPv6 brackets that may have leaked into ``netloc`` when
    # ``hostname`` was unavailable. ``hostname`` itself does not include
    # brackets, but the ``netloc`` fallback can.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    path = parts.path or ""
    return scheme, host, path, parts.query or "", parts.fragment or ""


def _host_is_linkedin(host: str) -> bool:
    """Return ``True`` iff *host* is ``linkedin.com`` or an allowed subdomain.

    Allowed shapes:

    - ``linkedin.com`` (bare)
    - ``<subdomain>.linkedin.com`` where ``<subdomain>`` is in
      :data:`_LINKEDIN_SUBDOMAINS`.

    Multi-level subdomains (e.g., ``foo.bar.linkedin.com``) are
    rejected to keep the validation tight; LinkedIn does not serve
    profile URLs from multi-level subdomains in practice. This also
    closes the spoof vector ``attacker.com.linkedin.com`` where the
    attacker controls a host whose name happens to end with the
    ``.linkedin.com`` suffix.

    Args:
        host: A lowercase host string with no port. Empty strings
            return ``False``.

    Returns:
        ``True`` iff the host matches a permitted shape.
    """
    if not host:
        return False
    if host == _LINKEDIN_BASE_HOST:
        return True
    if not host.endswith("." + _LINKEDIN_BASE_HOST):
        return False
    # Slice off the trailing ``.linkedin.com`` to inspect what comes
    # before it. Any remaining ``.`` indicates a multi-level subdomain.
    prefix = host[: -(len(_LINKEDIN_BASE_HOST) + 1)]
    if "." in prefix:
        return False
    return prefix in _LINKEDIN_SUBDOMAINS


def _path_is_linkedin_profile(path: str) -> bool:
    """Return ``True`` iff *path* is a well-formed LinkedIn profile path.

    A well-formed path:

    1. Starts with one of :data:`_LINKEDIN_PATH_PREFIXES`
       (``"/in/"`` or ``"/pub/"``).
    2. Has a non-empty slug segment after the prefix.
    3. The slug segment matches :data:`_SLUG_SEGMENT_RE`
       (URL-safe characters only).

    The trailing slash and any further path segments after the slug are
    tolerated (LinkedIn profile URLs sometimes have
    ``/detail/contact-info`` appended, for example) since the full path
    is preserved during normalization.

    Args:
        path: The path component returned by
            :func:`urllib.parse.urlsplit`. Never ``None``; an empty
            string returns ``False``.

    Returns:
        ``True`` iff the path matches one of the LinkedIn profile
        prefix shapes and has a syntactically valid slug.
    """
    # Collapse accidental ``//`` runs the user may have pasted, then
    # check the resulting normalized path. This means we accept both
    # ``/in/jane`` and ``//in//jane`` consistently.
    normalized_path = _MULTIPLE_SLASH_RE.sub("/", path)
    for prefix in _LINKEDIN_PATH_PREFIXES:
        if normalized_path.startswith(prefix):
            # Extract the slug segment between the prefix and the next
            # slash. LinkedIn ``/pub/`` URLs include trailing numeric
            # path components (e.g., ``/pub/jane-doe/12/345/678``) so
            # the slug is just the FIRST segment after the prefix.
            tail = normalized_path[len(prefix) :]
            segments = tail.split("/", 1)
            slug = segments[0]
            if not slug:
                return False
            return bool(_SLUG_SEGMENT_RE.match(slug))
    return False


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def is_valid_linkedin_url(url: str) -> bool:
    """Return ``True`` if *url* is a syntactically valid LinkedIn profile URL.

    Args:
        url: The candidate URL string. ``None``, empty, or non-string
            inputs return ``False``.

    Returns:
        ``True`` when all of the following are true:

        1. Scheme is ``http`` or ``https``.
        2. Host is ``linkedin.com`` or an allowed subdomain.
        3. Path is a LinkedIn profile path (starts with ``/in/`` or
           ``/pub/`` and has a non-empty URL-safe slug segment).

        ``False`` otherwise. This function NEVER raises for any input
        type; malformed inputs simply return ``False``. Pydantic and
        Zod handle the user-facing rejection separately so that
        consumers can choose their own error message.

    Examples:
        >>> is_valid_linkedin_url("https://www.linkedin.com/in/jane-doe")
        True
        >>> is_valid_linkedin_url("http://uk.linkedin.com/in/jane-doe/")
        True
        >>> is_valid_linkedin_url("https://example.com/in/jane")
        False
        >>> is_valid_linkedin_url("not a url")
        False
        >>> is_valid_linkedin_url("")
        False
    """
    parsed = _split_for_validation(url)
    if parsed is None:
        return False
    scheme, host, path, _query, _fragment = parsed
    if scheme not in _ALLOWED_SCHEMES:
        return False
    if not _host_is_linkedin(host):
        return False
    return _path_is_linkedin_profile(path)


def normalize_linkedin_url(url: str) -> str:
    """Return the canonical form of *url* for duplicate detection.

    The canonical form:

    - Scheme is forced to ``https`` (per the F-010 unique-index
      strategy: all rows MUST use the same scheme so that ``http://``
      and ``https://`` versions of the same profile collide on the
      unique index).
    - Host is lowercased; any leading ``www.`` is stripped so that
      ``www.linkedin.com`` and ``linkedin.com`` collapse to the same
      key. Regional subdomains (``uk.``, ``de.``, ...) are preserved
      because they correspond to different LinkedIn locales.
    - Path is preserved BUT any trailing slash is removed AND any run
      of consecutive slashes is collapsed to a single slash.
    - The slug segment after ``/in/`` or ``/pub/`` is lowercased so
      that ``/in/JaneDoe`` and ``/in/janedoe`` collapse to the same
      key (LinkedIn vanity URLs are case-insensitive on the server).
    - Query string is dropped entirely.
    - Fragment is dropped entirely.

    Args:
        url: The user-supplied LinkedIn URL. Should be validated by
            :func:`is_valid_linkedin_url` first; this function does not
            re-validate. If *url* is malformed, the function returns
            the empty string rather than raising -- callers that need
            authoritative rejection MUST use the validator.

    Returns:
        The normalized URL string, or ``""`` for malformed input. The
        empty-string fallback for malformed input is safe because it
        cannot collide with any real LinkedIn URL (every real URL
        begins with ``https://``).

    Examples:
        >>> normalize_linkedin_url("https://www.LinkedIn.com/in/JaneDoe/")
        'https://linkedin.com/in/janedoe'
        >>> normalize_linkedin_url("http://uk.linkedin.com/in/jane-doe/?utm_source=newsletter#bio")
        'https://uk.linkedin.com/in/jane-doe'
        >>> normalize_linkedin_url("https://linkedin.com/in/jane")
        'https://linkedin.com/in/jane'
        >>> normalize_linkedin_url("not a url")
        ''
    """
    parsed = _split_for_validation(url)
    if parsed is None:
        return ""
    _scheme, host, path, _query, _fragment = parsed
    if not host:
        return ""

    # 1. Strip a leading ``www.`` subdomain so the bare and
    #    www-prefixed hosts share a canonical key. Regional subdomains
    #    (uk., de., ...) are PRESERVED because they correspond to
    #    different LinkedIn locales for the same profile.
    if host.startswith("www."):
        host = host[len("www.") :]

    # 2. Collapse multiple slashes and drop any trailing slash from the
    #    path. Preserve a single leading "/" unless the path was empty
    #    (in which case we leave it empty so urlunsplit produces a
    #    clean result without a stray trailing slash).
    collapsed_path = _MULTIPLE_SLASH_RE.sub("/", path)
    if collapsed_path != "/" and collapsed_path.endswith("/"):
        collapsed_path = collapsed_path.rstrip("/")

    # 3. Lowercase the slug segment after /in/ or /pub/ to defeat
    #    case-only duplicates such as /in/JaneDoe vs /in/janedoe.
    #    Only the first path segment after the prefix is lowercased;
    #    any trailing path components are left untouched (they are not
    #    part of the slug identity).
    lowered_path = collapsed_path
    for prefix in _LINKEDIN_PATH_PREFIXES:
        if collapsed_path.startswith(prefix):
            tail = collapsed_path[len(prefix) :]
            if "/" in tail:
                slug, rest = tail.split("/", 1)
                lowered_path = prefix + slug.lower() + "/" + rest
            else:
                lowered_path = prefix + tail.lower()
            break

    # 4. Reassemble. Always force https. Always drop query and
    #    fragment. ``urlunsplit`` handles host-only and host+path
    #    forms cleanly without leaving stray separators.
    return urlunsplit(("https", host, lowered_path, "", ""))
