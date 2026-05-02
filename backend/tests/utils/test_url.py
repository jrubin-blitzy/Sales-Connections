"""Unit tests for ``app.utils.url``.

Exercises the two pure helpers:

- ``is_valid_linkedin_url(url)``: returns True iff *url* is a syntactically
  valid LinkedIn profile URL. Validates scheme (http/https), host
  (linkedin.com or allowed subdomain), and path prefix (/in/ or /pub/
  with a non-empty URL-safe slug).

- ``normalize_linkedin_url(url)``: returns the canonical form of *url*
  used as the unique-index key for F-010 duplicate detection. Lowercases
  the host, strips ``www.`` (preserving regional subdomains), forces
  https, drops trailing slashes, drops query strings and fragments,
  and lowercases the path slug.

All tests are PURE: no DB, no I/O, no network, no fixtures from
``conftest.py``. Tests are deterministic and runnable in any order.
Marker: ``@pytest.mark.unit``.

Per AAP F-010 the unique partial index on
``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL`` requires
these two functions to agree perfectly: every URL the validator accepts
MUST normalize to a stable, deterministic, comparable string.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.utils.url import is_valid_linkedin_url, normalize_linkedin_url

# ---------------------------------------------------------------------------
# Module-level marker
# ---------------------------------------------------------------------------
# Apply ``unit`` to EVERY test in this file without per-function decoration.
# This is registered in ``backend/pyproject.toml`` under
# ``[tool.pytest.ini_options].markers`` so ``--strict-markers`` accepts it.
pytestmark = pytest.mark.unit


# ===========================================================================
# Tests for ``is_valid_linkedin_url``
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 3.1 - Happy Paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://linkedin.com/in/jane-doe", id="bare-host-https"),
        pytest.param("https://www.linkedin.com/in/jane-doe", id="www-https"),
        pytest.param("https://www.linkedin.com/in/jane-doe/", id="www-trailing-slash"),
        pytest.param("https://uk.linkedin.com/in/jane-doe", id="regional-uk"),
        pytest.param("https://m.linkedin.com/in/jane-doe", id="mobile-subdomain"),
        pytest.param(
            "https://linkedin.com/pub/jane-doe/12/345/678",
            id="pub-prefix-with-deep-path",
        ),
        pytest.param(
            "HTTPS://WWW.LINKEDIN.COM/in/jane",
            id="case-insensitive-scheme-and-host",
        ),
        pytest.param("http://linkedin.com/in/jane", id="http-allowed"),
        pytest.param("https://de.linkedin.com/in/jane-doe", id="regional-de"),
        pytest.param("https://br.linkedin.com/in/jane-doe", id="regional-br"),
        pytest.param("https://linkedin.com/in/jane.doe", id="dotted-slug"),
        pytest.param("https://linkedin.com/in/jane_doe", id="underscored-slug"),
        pytest.param("https://linkedin.com/in/jane-doe123", id="alphanumeric-slug"),
    ],
)
def test_is_valid_linkedin_url_accepts_valid_urls(url: str) -> None:
    """Every well-formed LinkedIn profile URL MUST validate as True."""
    assert is_valid_linkedin_url(url) is True


# ---------------------------------------------------------------------------
# Phase 3.2 - Type Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "non_string_input",
    [
        pytest.param(None, id="none"),
        pytest.param(42, id="int"),
        pytest.param(3.14, id="float"),
        pytest.param([], id="empty-list"),
        pytest.param({}, id="empty-dict"),
        pytest.param((), id="empty-tuple"),
        pytest.param(b"https://linkedin.com/in/jane", id="bytes"),
        pytest.param(bytearray(b"https://linkedin.com/in/jane"), id="bytearray"),
        pytest.param(object(), id="custom-object"),
        pytest.param(True, id="bool-true"),
        pytest.param(False, id="bool-false"),
    ],
)
def test_is_valid_linkedin_url_rejects_non_string(
    non_string_input: Any,
) -> None:
    """Non-string inputs MUST return False, never raise."""
    assert is_valid_linkedin_url(non_string_input) is False


# ---------------------------------------------------------------------------
# Phase 3.3 - Empty / Whitespace Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "empty_like",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="spaces-only"),
        pytest.param("\t\n\r", id="whitespace-controls-only"),
    ],
)
def test_is_valid_linkedin_url_rejects_empty(empty_like: str) -> None:
    """Empty and whitespace-only strings MUST return False."""
    assert is_valid_linkedin_url(empty_like) is False


# ---------------------------------------------------------------------------
# Phase 3.4 - Malformed URL Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("not a url", id="plain-text"),
        pytest.param("linkedin.com/in/jane", id="missing-scheme"),
        pytest.param("/in/jane", id="schema-relative-no-host"),
        pytest.param("://linkedin.com/in/jane", id="empty-scheme"),
        # Scheme-only URLs: scheme parses as ``https`` but host is empty,
        # exercising the empty-host short-circuit in ``_host_is_linkedin``.
        pytest.param("https://", id="scheme-only-no-host"),
        pytest.param("http:///in/jane", id="empty-host-with-path"),
    ],
)
def test_is_valid_linkedin_url_rejects_malformed(malformed: str) -> None:
    """Syntactically broken URLs MUST return False."""
    assert is_valid_linkedin_url(malformed) is False


# ---------------------------------------------------------------------------
# Phase 3.5 - Wrong Scheme Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "wrong_scheme_url",
    [
        pytest.param("ftp://linkedin.com/in/jane", id="ftp"),
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param(
            "javascript:alert('xss')",
            id="javascript",
        ),
        pytest.param("data:text/plain,hi", id="data"),
        pytest.param("mailto:jane@linkedin.com", id="mailto"),
        pytest.param("ssh://linkedin.com/in/jane", id="ssh"),
    ],
)
def test_is_valid_linkedin_url_rejects_wrong_scheme(
    wrong_scheme_url: str,
) -> None:
    """Schemes other than http/https MUST return False."""
    assert is_valid_linkedin_url(wrong_scheme_url) is False


# ---------------------------------------------------------------------------
# Phase 3.6 - Wrong Host Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "wrong_host_url",
    [
        pytest.param("https://example.com/in/jane", id="example-com"),
        pytest.param("https://google.com/in/jane", id="google-com"),
        pytest.param("https://twitter.com/in/jane", id="twitter-com"),
        pytest.param("https://linkdin.com/in/jane", id="typo-linkdin"),
        pytest.param("https://linkedinx.com/in/jane", id="lookalike-linkedinx"),
    ],
)
def test_is_valid_linkedin_url_rejects_wrong_host(
    wrong_host_url: str,
) -> None:
    """Non-LinkedIn hosts MUST return False."""
    assert is_valid_linkedin_url(wrong_host_url) is False


# ---------------------------------------------------------------------------
# Phase 3.7 - LinkedIn-Suffix Attack Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spoofed_host_url",
    [
        # Not a LinkedIn site: ``linkedin.com`` is part of a longer suffix.
        pytest.param(
            "https://linkedin.example.com/in/jane",
            id="linkedin-as-subdomain-of-evil",
        ),
        # Multi-level subdomain on linkedin.com - LinkedIn doesn't serve
        # profiles from these.
        pytest.param(
            "https://foo.bar.linkedin.com/in/jane",
            id="multi-level-subdomain",
        ),
        pytest.param(
            "https://attacker.linkedin.com/in/jane",
            id="unrecognized-subdomain",
        ),
        # The bare host appears in the path but the actual host is
        # something else.
        pytest.param(
            "https://example.com/linkedin.com/in/jane",
            id="linkedin-com-in-path",
        ),
    ],
)
def test_is_valid_linkedin_url_rejects_suffix_attacks(
    spoofed_host_url: str,
) -> None:
    """Hosts where ``linkedin.com`` is not the proper suffix MUST return False."""
    assert is_valid_linkedin_url(spoofed_host_url) is False


# ---------------------------------------------------------------------------
# Phase 3.8 - Missing or Wrong Path Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_path_url",
    [
        # No path at all.
        pytest.param("https://linkedin.com", id="no-path"),
        pytest.param("https://linkedin.com/", id="root-only"),
        # Wrong path prefix.
        pytest.param("https://linkedin.com/jane", id="missing-in-prefix"),
        pytest.param("https://linkedin.com/profile/jane", id="wrong-prefix"),
        pytest.param("https://linkedin.com/company/foo", id="company-prefix"),
        # Empty slug after /in/ or /pub/.
        pytest.param("https://linkedin.com/in/", id="empty-slug-in"),
        pytest.param("https://linkedin.com/pub/", id="empty-slug-pub"),
        pytest.param("https://linkedin.com/in", id="just-in-no-slash"),
        # Invalid slug character (space).
        pytest.param("https://linkedin.com/in/jane doe", id="space-in-slug"),
    ],
)
def test_is_valid_linkedin_url_rejects_bad_paths(bad_path_url: str) -> None:
    """Paths with no/wrong prefix or invalid slugs MUST return False."""
    assert is_valid_linkedin_url(bad_path_url) is False


# ---------------------------------------------------------------------------
# Phase 3.9 - Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://linkedin.com/in/jane-doe", True, id="valid"),
        pytest.param("https://example.com/in/jane", False, id="invalid"),
        pytest.param("", False, id="empty"),
        pytest.param("not a url", False, id="garbage"),
    ],
)
def test_is_valid_linkedin_url_is_deterministic(url: str, expected: bool) -> None:
    """The same input MUST always produce the same output."""
    first = is_valid_linkedin_url(url)
    second = is_valid_linkedin_url(url)
    third = is_valid_linkedin_url(url)
    assert first is expected
    assert first is second is third


# ===========================================================================
# Tests for ``normalize_linkedin_url``
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 4.1 - Core Canonicalization (Trailing Slash)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "https://www.linkedin.com/in/jane-doe/",
            "https://linkedin.com/in/jane-doe",
            id="strips-trailing-slash-and-www",
        ),
        pytest.param(
            "https://linkedin.com/in/jane-doe/",
            "https://linkedin.com/in/jane-doe",
            id="strips-trailing-slash",
        ),
        pytest.param(
            "https://linkedin.com/in/jane-doe",
            "https://linkedin.com/in/jane-doe",
            id="no-trailing-slash-unchanged",
        ),
    ],
)
def test_normalize_linkedin_url_strips_trailing_slash(input_url: str, expected: str) -> None:
    """Trailing slashes MUST be stripped from the canonical form."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.2 - Case Lowering of Host and Slug
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "HTTP://WWW.LINKEDIN.COM/in/JaneDoe",
            "https://linkedin.com/in/janedoe",
            id="upper-host-and-slug",
        ),
        pytest.param(
            "https://www.LinkedIn.com/in/JaneDoe/",
            "https://linkedin.com/in/janedoe",
            id="mixed-case-host-and-slug",
        ),
        pytest.param(
            "https://linkedin.com/in/JANE",
            "https://linkedin.com/in/jane",
            id="upper-slug",
        ),
    ],
)
def test_normalize_linkedin_url_lowercases_host_and_slug(input_url: str, expected: str) -> None:
    """Case-only differences MUST collapse in the canonical form."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.3 - Query and Fragment Dropping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "https://linkedin.com/in/jane?utm=foo",
            "https://linkedin.com/in/jane",
            id="drops-query-utm",
        ),
        pytest.param(
            "https://linkedin.com/in/jane#bio",
            "https://linkedin.com/in/jane",
            id="drops-fragment",
        ),
        pytest.param(
            "https://linkedin.com/in/jane?utm=foo#bio",
            "https://linkedin.com/in/jane",
            id="drops-both-query-and-fragment",
        ),
        pytest.param(
            "https://linkedin.com/in/jane?a=1&b=2&c=3",
            "https://linkedin.com/in/jane",
            id="drops-multi-param-query",
        ),
    ],
)
def test_normalize_linkedin_url_drops_query_and_fragment(input_url: str, expected: str) -> None:
    """Query strings and fragments MUST be dropped from the canonical form."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.4 - Regional Subdomain Preservation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "https://uk.linkedin.com/in/jane-doe",
            "https://uk.linkedin.com/in/jane-doe",
            id="uk-preserved",
        ),
        pytest.param(
            "https://de.linkedin.com/in/jane-doe",
            "https://de.linkedin.com/in/jane-doe",
            id="de-preserved",
        ),
        pytest.param(
            "https://br.linkedin.com/in/jane-doe",
            "https://br.linkedin.com/in/jane-doe",
            id="br-preserved",
        ),
        pytest.param(
            "https://m.linkedin.com/in/jane-doe",
            "https://m.linkedin.com/in/jane-doe",
            id="m-preserved",
        ),
    ],
)
def test_normalize_linkedin_url_preserves_regional_subdomain(input_url: str, expected: str) -> None:
    """Regional subdomains correspond to different LinkedIn locales for
    the same profile and MUST be preserved."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.5 - Force HTTPS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "http://linkedin.com/in/jane",
            "https://linkedin.com/in/jane",
            id="http-to-https",
        ),
        pytest.param(
            "http://www.linkedin.com/in/jane",
            "https://linkedin.com/in/jane",
            id="http-www-to-https-bare",
        ),
        pytest.param(
            "HTTP://LINKEDIN.COM/in/jane",
            "https://linkedin.com/in/jane",
            id="upper-http-to-https",
        ),
    ],
)
def test_normalize_linkedin_url_forces_https(input_url: str, expected: str) -> None:
    """All schemes are forced to https so http and https variants of the
    same profile collide on the unique index."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.6 - ``www.`` Stripping (Not Regional)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "https://www.linkedin.com/in/jane",
            "https://linkedin.com/in/jane",
            id="www-stripped",
        ),
        pytest.param(
            "https://uk.linkedin.com/in/jane",
            "https://uk.linkedin.com/in/jane",
            id="uk-not-stripped",
        ),
        # The validator rejects this anyway, but normalize is forgiving;
        # whatever it produces MUST still preserve the (invalid) host.
        pytest.param(
            "https://m.linkedin.com/in/jane",
            "https://m.linkedin.com/in/jane",
            id="m-not-stripped",
        ),
    ],
)
def test_normalize_linkedin_url_strips_only_www(input_url: str, expected: str) -> None:
    """Only the ``www.`` prefix is stripped; regional/mobile subdomains stay."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.7 - Edge Cases: Empty / None / Non-String / Malformed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "empty_like",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_normalize_linkedin_url_returns_empty_for_empty_input(
    empty_like: str,
) -> None:
    """Empty and whitespace-only strings MUST return the empty string."""
    assert normalize_linkedin_url(empty_like) == ""


@pytest.mark.parametrize(
    "non_string_input",
    [
        pytest.param(None, id="none"),
        pytest.param(42, id="int"),
        pytest.param(3.14, id="float"),
        pytest.param([], id="empty-list"),
        pytest.param({}, id="empty-dict"),
        pytest.param(b"https://linkedin.com/in/jane", id="bytes"),
        pytest.param(object(), id="custom-object"),
        pytest.param(True, id="bool"),
    ],
)
def test_normalize_linkedin_url_returns_empty_for_non_string(
    non_string_input: Any,
) -> None:
    """Non-string inputs MUST return empty string, never raise."""
    assert normalize_linkedin_url(non_string_input) == ""


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("not a url", id="plain-text"),
        pytest.param("///", id="just-slashes"),
        pytest.param("://", id="empty-scheme-host"),
        pytest.param("javascript:alert(1)", id="js-scheme-no-host"),
    ],
)
def test_normalize_linkedin_url_returns_empty_for_malformed(
    malformed: str,
) -> None:
    """Malformed inputs MUST return empty string, never raise.

    The empty string cannot collide with any real LinkedIn URL on the
    unique partial index, making the fallback safe.
    """
    assert normalize_linkedin_url(malformed) == ""


# ---------------------------------------------------------------------------
# Phase 4.8 - Multiple Slash Collapse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "https://linkedin.com//in//jane//",
            "https://linkedin.com/in/jane",
            id="double-slash-pair",
        ),
        pytest.param(
            "https://linkedin.com/in///jane",
            "https://linkedin.com/in/jane",
            id="triple-slash-mid",
        ),
        pytest.param(
            "https://linkedin.com////in////jane",
            "https://linkedin.com/in/jane",
            id="quad-slash",
        ),
    ],
)
def test_normalize_linkedin_url_collapses_multiple_slashes(input_url: str, expected: str) -> None:
    """Runs of ``//`` in the path MUST collapse to a single ``/``."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.9 - Whitespace Trimming
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        pytest.param(
            "  https://linkedin.com/in/jane  ",
            "https://linkedin.com/in/jane",
            id="space-padded",
        ),
        pytest.param(
            "\thttps://linkedin.com/in/jane\n",
            "https://linkedin.com/in/jane",
            id="tab-newline-padded",
        ),
    ],
)
def test_normalize_linkedin_url_strips_whitespace(input_url: str, expected: str) -> None:
    """Leading and trailing whitespace MUST be stripped before parsing."""
    assert normalize_linkedin_url(input_url) == expected


# ---------------------------------------------------------------------------
# Phase 4.10 - Idempotency
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "input_url",
    [
        pytest.param(
            "https://www.linkedin.com/in/jane-doe/",
            id="strips-www-and-slash",
        ),
        pytest.param(
            "HTTP://WWW.LINKEDIN.COM/in/JaneDoe?utm=x#bio",
            id="all-canonicalizations",
        ),
        pytest.param("https://uk.linkedin.com/in/jane", id="regional"),
        pytest.param("https://linkedin.com/in/jane", id="already-canonical"),
        pytest.param("", id="empty"),
        pytest.param("not a url", id="malformed"),
        pytest.param("https://linkedin.com//in//jane", id="double-slashes"),
    ],
)
def test_normalize_linkedin_url_is_idempotent(input_url: str) -> None:
    """``normalize(normalize(x)) == normalize(x)`` for every input."""
    once = normalize_linkedin_url(input_url)
    twice = normalize_linkedin_url(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Phase 4.11 - Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "input_url",
    [
        pytest.param("https://linkedin.com/in/jane", id="canonical"),
        pytest.param("HTTP://WWW.LINKEDIN.COM/in/JaneDoe/?x=1#y", id="messy"),
        pytest.param("not a url", id="malformed"),
        pytest.param("", id="empty"),
    ],
)
def test_normalize_linkedin_url_is_deterministic(input_url: str) -> None:
    """The same input MUST produce the same output across calls."""
    first = normalize_linkedin_url(input_url)
    second = normalize_linkedin_url(input_url)
    third = normalize_linkedin_url(input_url)
    assert first == second == third


# ---------------------------------------------------------------------------
# Phase 4.12 - No Exceptions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pathological",
    [
        pytest.param(None, id="none"),
        pytest.param(42, id="int"),
        pytest.param("\x00\x01\x02", id="control-chars-only"),
        pytest.param("a" * 10000, id="long-garbage"),
        pytest.param("https://" + "a" * 1000, id="long-host"),
        pytest.param("https://linkedin.com/in/" + "a" * 1000, id="long-slug"),
        pytest.param("ftp://linkedin.com/in/jane", id="wrong-scheme"),
        pytest.param("https://[invalid:ipv6/in/jane", id="malformed-ipv6"),
    ],
)
def test_normalize_linkedin_url_never_raises(pathological: Any) -> None:
    """Pathological inputs MUST NOT raise."""
    result = normalize_linkedin_url(pathological)
    assert isinstance(result, str)


# ===========================================================================
# Phase 5 - Cross-Function Consistency Tests
# ===========================================================================


@pytest.mark.parametrize(
    "valid_url",
    [
        pytest.param("https://linkedin.com/in/jane-doe", id="bare"),
        pytest.param("https://www.linkedin.com/in/jane-doe", id="www"),
        pytest.param("https://uk.linkedin.com/in/jane-doe", id="regional"),
        pytest.param("https://linkedin.com/pub/jane-doe/12/345/678", id="pub"),
    ],
)
def test_valid_urls_normalize_to_non_empty(valid_url: str) -> None:
    """Every URL the validator accepts MUST normalize to a non-empty string.

    This is the F-010 invariant: the unique-index key for a valid LinkedIn
    profile URL is always a deterministic, non-empty string.
    """
    assert is_valid_linkedin_url(valid_url) is True
    assert normalize_linkedin_url(valid_url) != ""


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        # Cosmetic differences that MUST collapse to the same key.
        pytest.param(
            "https://www.linkedin.com/in/jane-doe",
            "https://linkedin.com/in/jane-doe",
            id="www-vs-bare",
        ),
        pytest.param(
            "https://www.linkedin.com/in/jane-doe/",
            "https://linkedin.com/in/jane-doe",
            id="trailing-slash-vs-not",
        ),
        pytest.param(
            "http://linkedin.com/in/jane-doe",
            "https://linkedin.com/in/jane-doe",
            id="http-vs-https",
        ),
        pytest.param(
            "https://linkedin.com/in/JaneDoe",
            "https://linkedin.com/in/janedoe",
            id="case-only-difference",
        ),
        pytest.param(
            "https://linkedin.com/in/jane?utm=newsletter",
            "https://linkedin.com/in/jane",
            id="query-vs-no-query",
        ),
        pytest.param(
            "https://linkedin.com/in/jane#bio",
            "https://linkedin.com/in/jane",
            id="fragment-vs-no-fragment",
        ),
    ],
)
def test_cosmetic_url_variants_normalize_identically(url_a: str, url_b: str) -> None:
    """Per F-010: cosmetic variants MUST collapse to the same unique-index key."""
    assert normalize_linkedin_url(url_a) == normalize_linkedin_url(url_b)


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        # Different profiles MUST NOT collapse to the same key.
        pytest.param(
            "https://linkedin.com/in/jane",
            "https://linkedin.com/in/john",
            id="different-slugs",
        ),
        # Regional locales of the same profile MUST NOT collapse
        # because they correspond to different LinkedIn pages.
        pytest.param(
            "https://linkedin.com/in/jane",
            "https://uk.linkedin.com/in/jane",
            id="bare-vs-regional",
        ),
        pytest.param(
            "https://uk.linkedin.com/in/jane",
            "https://de.linkedin.com/in/jane",
            id="different-regional-locales",
        ),
    ],
)
def test_different_profiles_normalize_differently(url_a: str, url_b: str) -> None:
    """Genuinely different LinkedIn URLs MUST produce different unique-index keys."""
    assert normalize_linkedin_url(url_a) != normalize_linkedin_url(url_b)
