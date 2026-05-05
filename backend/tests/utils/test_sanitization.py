"""Unit tests for ``app.utils.sanitization``.

Exercises the two pure helpers:

- ``sanitize_for_ai_prompt(text, max_chars=4000)``: cleans free-form
  user-supplied text BEFORE templating into an Anthropic Claude prompt.
  Strips ASCII control characters (preserving ``\\t\\n\\r``), Unicode
  bidi/zero-width/format characters, applies NFC normalization,
  collapses whitespace, and truncates to a length cap with a visible
  ``... [truncated]`` suffix.

- ``redact_secret_for_logging(value, keep=4)``: renders a partial
  representation of a credential string for log output. Clamps the
  ``keep`` parameter to half the input length so very short secrets
  are never partially exposed.

All tests are PURE: no DB, no I/O, no network, no fixtures from
``conftest.py``. Tests are deterministic and runnable in any order.
Marker: ``@pytest.mark.unit``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.utils.sanitization import (
    redact_secret_for_logging,
    sanitize_for_ai_prompt,
)

# ---------------------------------------------------------------------------
# Module-level marker
# ---------------------------------------------------------------------------
# Apply ``unit`` to EVERY test in this file without per-function decoration.
# This is registered in ``backend/pyproject.toml`` under
# ``[tool.pytest.ini_options].markers`` so ``--strict-markers`` accepts it.
pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
# Mirror the production constants for assertion clarity. The redaction
# placeholder text MUST match ``_REDACT_PLACEHOLDER`` in the source
# module. The default cap MUST match ``BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS``.
_REDACT_PLACEHOLDER: str = "***REDACTED***"
_DEFAULT_MAX_CHARS: int = 4000
_TRUNCATION_SUFFIX: str = "... [truncated]"


# ===========================================================================
# Tests for ``sanitize_for_ai_prompt``
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 4.1 - Whitespace Control Preservation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("a\tb", "a\tb", id="preserves-tab"),
        pytest.param("a\nb", "a\nb", id="preserves-newline"),
        pytest.param("a\rb", "a\rb", id="preserves-carriage-return"),
        pytest.param(
            "Tab\there\nand newline",
            "Tab\there\nand newline",
            id="preserves-mixed-whitespace-controls",
        ),
    ],
)
def test_sanitize_for_ai_prompt_preserves_whitespace_controls(
    input_text: str, expected: str
) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.2 - ASCII Control Character Stripping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("Hello\x00World", "HelloWorld", id="strips-null"),
        pytest.param("Hello\x01World", "HelloWorld", id="strips-soh"),
        pytest.param("Hello\x07World", "HelloWorld", id="strips-bell"),
        pytest.param("Hello\x08World", "HelloWorld", id="strips-backspace"),
        pytest.param("Hello\x0bWorld", "HelloWorld", id="strips-vertical-tab"),
        pytest.param("Hello\x0cWorld", "HelloWorld", id="strips-form-feed"),
        pytest.param("Hello\x1bWorld", "HelloWorld", id="strips-escape"),
        pytest.param("Hello\x1fWorld", "HelloWorld", id="strips-unit-separator"),
        pytest.param("Hello\x7fWorld", "HelloWorld", id="strips-delete"),
    ],
)
def test_sanitize_for_ai_prompt_strips_ascii_control_chars(input_text: str, expected: str) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.3 - Zero-Width Character Stripping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("Hello\u200bWorld", "HelloWorld", id="strips-zero-width-space"),
        pytest.param("Hello\u200cWorld", "HelloWorld", id="strips-zero-width-non-joiner"),
        pytest.param("Hello\u200dWorld", "HelloWorld", id="strips-zero-width-joiner"),
        pytest.param("Hello\u2060World", "HelloWorld", id="strips-word-joiner"),
        pytest.param("\ufeffhello", "hello", id="strips-bom"),
    ],
)
def test_sanitize_for_ai_prompt_strips_zero_width_chars(input_text: str, expected: str) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.4 - Bidirectional Control Character Stripping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("Hello\u200eWorld", "HelloWorld", id="strips-lrm"),
        pytest.param("Hello\u200fWorld", "HelloWorld", id="strips-rlm"),
        pytest.param("Hello\u202aevil", "Helloevil", id="strips-lre"),
        pytest.param("Hello\u202bevil", "Helloevil", id="strips-rle"),
        pytest.param("Hello\u202cevil", "Helloevil", id="strips-pdf"),
        pytest.param("Hello\u202devil", "Helloevil", id="strips-lro"),
        pytest.param("Hello\u202eevil", "Helloevil", id="strips-rlo"),
        pytest.param("Hello\u2066evil", "Helloevil", id="strips-lri"),
        pytest.param("Hello\u2067evil", "Helloevil", id="strips-rli"),
        pytest.param("Hello\u2068evil", "Helloevil", id="strips-fsi"),
        pytest.param("Hello\u2069evil", "Helloevil", id="strips-pdi"),
    ],
)
def test_sanitize_for_ai_prompt_strips_bidi_chars(input_text: str, expected: str) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.5 - Soft Hyphen and Object Replacement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("co\u00adoperate", "cooperate", id="strips-soft-hyphen"),
        pytest.param("ab\ufffccd", "abcd", id="strips-object-replacement-char"),
    ],
)
def test_sanitize_for_ai_prompt_strips_other_invisibles(input_text: str, expected: str) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.5b - Category-Based Cf/Co Fallthrough Stripping
# ---------------------------------------------------------------------------
# The source module documents a defense-in-depth fallthrough: any
# character whose Unicode general category is ``Cf`` (Format) or ``Co``
# (Private Use) is stripped EVEN WHEN it is not in the explicit
# ``_INVISIBLE_UNICODE_CHARS`` allow-list. This catches additions to
# the format class in future Unicode versions without requiring a code
# update, and it removes private-use codepoints that render
# inconsistently across fonts and have no place in Sales-Connections
# free text. The chars below are deliberately chosen to be NOT in the
# explicit allow-list to exercise the category branch.
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        # Cf (Format) characters not in the explicit allow-list.
        pytest.param("a\u061cb", "ab", id="strips-arabic-letter-mark-cf"),
        pytest.param("a\u180eb", "ab", id="strips-mongolian-vowel-separator-cf"),
        # Co (Private Use Area) characters - first and last in BMP.
        pytest.param("a\ue000b", "ab", id="strips-private-use-start-co"),
        pytest.param("a\uf8ffb", "ab", id="strips-private-use-end-co"),
    ],
)
def test_sanitize_for_ai_prompt_strips_format_and_private_use_categories(
    input_text: str, expected: str
) -> None:
    """Cf (Format) and Co (Private Use) chars are stripped via category check.

    These chars are NOT in the explicit ``_INVISIBLE_UNICODE_CHARS``
    allow-list; the source module relies on ``unicodedata.category``
    to catch them.
    """
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.6 - NFC Unicode Normalization
# ---------------------------------------------------------------------------
def test_sanitize_for_ai_prompt_applies_nfc_normalization() -> None:
    """Decomposed e + combining acute MUST collapse to composed e-acute.

    Defends against canonical-equivalence smuggling: an attacker could
    otherwise pass a visually identical string that bypasses byte-level
    string-equality checks downstream.
    """
    composed = "\u00e9"  # e-acute (single code point)
    decomposed = "e\u0301"  # e + combining acute (two code points)
    assert sanitize_for_ai_prompt(composed) == composed
    assert sanitize_for_ai_prompt(decomposed) == composed
    # Both inputs MUST produce identical sanitized output.
    assert sanitize_for_ai_prompt(decomposed) == sanitize_for_ai_prompt(composed)


# ---------------------------------------------------------------------------
# Phase 4.7 - Whitespace Collapsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        pytest.param("a    b", "a b", id="collapses-multiple-spaces"),
        pytest.param("a  b  c", "a b c", id="collapses-runs-mid-string"),
        pytest.param("  hello  ", "hello", id="strips-leading-trailing"),
        pytest.param("\t\thello\t\t", "hello", id="strips-leading-trailing-tabs"),
        pytest.param("a\u00a0\u00a0b", "a b", id="collapses-nbsp-runs"),
    ],
)
def test_sanitize_for_ai_prompt_collapses_whitespace(input_text: str, expected: str) -> None:
    assert sanitize_for_ai_prompt(input_text) == expected


# ---------------------------------------------------------------------------
# Phase 4.8 - Length Cap with Suffix
# ---------------------------------------------------------------------------
def test_sanitize_for_ai_prompt_applies_length_cap_with_suffix() -> None:
    """Long input gets truncated and ends with the visible suffix.

    The total length MUST equal ``max_chars`` exactly so callers can
    rely on the configured limit.
    """
    result = sanitize_for_ai_prompt("a" * 5000, max_chars=100)
    assert len(result) == 100
    assert result.endswith(_TRUNCATION_SUFFIX)


def test_sanitize_for_ai_prompt_hard_truncates_when_max_too_small_for_suffix() -> None:
    """When ``max_chars`` is smaller than the suffix length, hard-truncate.

    The function MUST never raise and MUST never return a string longer
    than ``max_chars``.
    """
    result = sanitize_for_ai_prompt("a" * 5000, max_chars=10)
    assert len(result) == 10
    # No suffix because there isn't room for it.
    assert _TRUNCATION_SUFFIX not in result


def test_sanitize_for_ai_prompt_default_max_chars_is_4000() -> None:
    """Documented default of 4000 MUST match BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS."""
    result = sanitize_for_ai_prompt("a" * 5000)
    assert len(result) == _DEFAULT_MAX_CHARS


@pytest.mark.parametrize(
    ("input_len", "max_chars"),
    [
        pytest.param(0, 100, id="empty-stays-empty"),
        pytest.param(50, 100, id="short-stays-unchanged"),
        pytest.param(100, 100, id="exact-fit-stays-unchanged"),
    ],
)
def test_sanitize_for_ai_prompt_does_not_truncate_when_within_cap(
    input_len: int, max_chars: int
) -> None:
    input_text = "a" * input_len
    result = sanitize_for_ai_prompt(input_text, max_chars=max_chars)
    assert len(result) == input_len
    # No suffix should be appended.
    assert _TRUNCATION_SUFFIX not in result


# ---------------------------------------------------------------------------
# Phase 4.8b - max_chars <= 0 Returns Empty
# ---------------------------------------------------------------------------
# Per the source contract: "Values less than or equal to zero produce
# the empty string." This is a documented behavior of the underlying
# ``_truncate_with_suffix`` helper. A non-positive cap means "do not
# emit any prompt content"; the function MUST honor that without
# raising and without leaking partial input.
@pytest.mark.parametrize(
    "max_chars",
    [
        pytest.param(0, id="zero-cap"),
        pytest.param(-1, id="negative-1-cap"),
        pytest.param(-100, id="negative-100-cap"),
    ],
)
def test_sanitize_for_ai_prompt_non_positive_max_chars_returns_empty(
    max_chars: int,
) -> None:
    """Non-positive ``max_chars`` MUST produce the empty string."""
    result = sanitize_for_ai_prompt("hello world", max_chars=max_chars)
    assert result == ""


# ---------------------------------------------------------------------------
# Phase 4.9 - Non-String Input Tolerance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "non_string_input",
    [
        pytest.param(None, id="none-input"),
        pytest.param(42, id="int-input"),
        pytest.param(3.14, id="float-input"),
        pytest.param([], id="list-input"),
        pytest.param({}, id="dict-input"),
        pytest.param((), id="tuple-input"),
        pytest.param(b"bytes", id="bytes-input"),
        pytest.param(bytearray(b"ba"), id="bytearray-input"),
        pytest.param(object(), id="custom-object-input"),
        pytest.param(True, id="bool-input"),
    ],
)
def test_sanitize_for_ai_prompt_returns_empty_for_non_string(
    non_string_input: Any,
) -> None:
    """Non-string inputs MUST be coerced to empty string, never raise."""
    result = sanitize_for_ai_prompt(non_string_input)
    assert result == ""
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Phase 4.10 - Empty String
# ---------------------------------------------------------------------------
def test_sanitize_for_ai_prompt_empty_input_returns_empty() -> None:
    assert sanitize_for_ai_prompt("") == ""


# ---------------------------------------------------------------------------
# Phase 4.11 - Idempotency
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "input_text",
    [
        pytest.param("Hello, world!", id="ascii-text"),
        pytest.param("Hello\x00\x01World", id="control-chars"),
        pytest.param("a\u200bb\u202ec", id="invisible-chars"),
        pytest.param("  spaces  ", id="leading-trailing-whitespace"),
        pytest.param("a    b    c", id="multi-space-runs"),
        pytest.param("a" * 5000, id="long-input-truncated"),
        pytest.param("e\u0301", id="decomposed-form"),
        pytest.param("", id="empty"),
        pytest.param(
            "We went to college together, he's now VP of Ops at a Series B logistics startup",
            id="realistic-prompt",
        ),
    ],
)
def test_sanitize_for_ai_prompt_is_idempotent(input_text: str) -> None:
    """``sanitize(sanitize(x)) == sanitize(x)`` for every input."""
    once = sanitize_for_ai_prompt(input_text)
    twice = sanitize_for_ai_prompt(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Phase 4.12 - Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "input_text",
    [
        pytest.param("Hello", id="ascii"),
        pytest.param("a\x00b\u200ec\u202ed", id="mixed-controls"),
        pytest.param("a" * 4500, id="long"),
    ],
)
def test_sanitize_for_ai_prompt_is_deterministic(input_text: str) -> None:
    """The same input MUST produce the same output across calls."""
    first = sanitize_for_ai_prompt(input_text)
    second = sanitize_for_ai_prompt(input_text)
    third = sanitize_for_ai_prompt(input_text)
    assert first == second == third


# ---------------------------------------------------------------------------
# Phase 4.13 - Realistic Input
# ---------------------------------------------------------------------------
def test_sanitize_for_ai_prompt_preserves_realistic_relationship_context() -> None:
    """The user's example prompt is short, clean, and within the cap.

    Per AAP Section 0.1.2, this is the canonical user-supplied input
    shape: a one-sentence description of how the contributor knows the
    target. It MUST pass through the sanitizer unchanged.
    """
    example = "We went to college together, he's now VP of Ops at a Series B logistics startup"
    assert sanitize_for_ai_prompt(example) == example


# ---------------------------------------------------------------------------
# Phase 4.14 - No Exceptions for Pathological Inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pathological_input",
    [
        pytest.param("\x00" * 100, id="all-nulls"),
        pytest.param("\u200b" * 100, id="all-zero-width-spaces"),
        pytest.param("\x00\x01\x02\x03\x04\x05", id="six-controls"),
        pytest.param("a\x00" * 5000, id="long-mixed-controls"),
    ],
)
def test_sanitize_for_ai_prompt_never_raises(pathological_input: str) -> None:
    """Pathological inputs MUST NOT raise."""
    result = sanitize_for_ai_prompt(pathological_input)
    assert isinstance(result, str)


# ===========================================================================
# Tests for ``redact_secret_for_logging``
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 5.1 - Default keep=4 Happy Path
# ---------------------------------------------------------------------------
def test_redact_secret_for_logging_default_keep_4() -> None:
    """Default keep=4 leaves the first 4 characters un-redacted."""
    result = redact_secret_for_logging("sk-ant-abcdefg")
    assert result == "sk-a" + _REDACT_PLACEHOLDER


def test_redact_secret_for_logging_default_keep_long_secret() -> None:
    """For a sufficiently long secret, exactly 4 chars are kept."""
    long_secret = "x" * 50
    result = redact_secret_for_logging(long_secret)
    assert result == "xxxx" + _REDACT_PLACEHOLDER


# ---------------------------------------------------------------------------
# Phase 5.2 - Custom keep Parameter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep", "expected"),
    [
        pytest.param(
            "sk-ant-abcdefghij",
            8,
            "sk-ant-a" + _REDACT_PLACEHOLDER,
            id="keep-8-of-17",
        ),
        pytest.param(
            "abcdefghij",
            5,
            "abcde" + _REDACT_PLACEHOLDER,
            id="keep-5-of-10",
        ),
        pytest.param(
            "supersecretvalue123456",
            10,
            "supersecre" + _REDACT_PLACEHOLDER,
            id="keep-10-of-22",
        ),
    ],
)
def test_redact_secret_for_logging_custom_keep(value: str, keep: int, expected: str) -> None:
    assert redact_secret_for_logging(value, keep=keep) == expected


# ---------------------------------------------------------------------------
# Phase 5.3 - Half-Length Clamp on Short Secrets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep", "expected"),
    [
        # "short" is 5 chars; half = 5//2 = 2 chars kept.
        pytest.param("short", 4, "sh" + _REDACT_PLACEHOLDER, id="5-char-clamps-to-2"),
        # "abcdef" is 6 chars; half = 6//2 = 3 chars kept.
        pytest.param("abcdef", 4, "abc" + _REDACT_PLACEHOLDER, id="6-char-clamps-to-3"),
        # "ab" is 2 chars; half = 2//2 = 1 char kept.
        pytest.param("ab", 4, "a" + _REDACT_PLACEHOLDER, id="2-char-clamps-to-1"),
        # 8 char secret with keep=4: half=4 -> kept=4 (no clamp needed).
        pytest.param("abcdefgh", 4, "abcd" + _REDACT_PLACEHOLDER, id="8-char-no-clamp"),
    ],
)
def test_redact_secret_for_logging_clamps_keep_to_half_length(
    value: str, keep: int, expected: str
) -> None:
    """Per the source spec: keep is clamped to ``min(keep, len(value)//2)``."""
    assert redact_secret_for_logging(value, keep=keep) == expected


# ---------------------------------------------------------------------------
# Phase 5.4 - Tiny Secret Returns Bare Placeholder
# ---------------------------------------------------------------------------
def test_redact_secret_for_logging_single_char_returns_placeholder() -> None:
    """1-char secret: half = 1//2 = 0, so nothing is exposed."""
    assert redact_secret_for_logging("a") == _REDACT_PLACEHOLDER


# ---------------------------------------------------------------------------
# Phase 5.5 - Empty / None / Non-String Inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "edge_input",
    [
        pytest.param("", id="empty-string"),
        pytest.param(None, id="none-input"),
        pytest.param(42, id="int-input"),
        pytest.param(3.14, id="float-input"),
        pytest.param([], id="list-input"),
        pytest.param({}, id="dict-input"),
        pytest.param(b"bytes", id="bytes-input"),
        pytest.param(object(), id="custom-object-input"),
        pytest.param(True, id="bool-input"),
    ],
)
def test_redact_secret_for_logging_returns_placeholder_for_invalid(
    edge_input: Any,
) -> None:
    """Empty, None, and non-string inputs MUST return the bare placeholder.

    A 'leak nothing' default is safer than echoing the literal value.
    """
    assert redact_secret_for_logging(edge_input) == _REDACT_PLACEHOLDER


# ---------------------------------------------------------------------------
# Phase 5.6 - Negative keep Clamps to Zero
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep"),
    [
        pytest.param("abcdef", -3, id="negative-3"),
        pytest.param("abcdef", -1, id="negative-1"),
        pytest.param("abcdef", -100, id="negative-100"),
        pytest.param("abcdef", 0, id="zero"),
    ],
)
def test_redact_secret_for_logging_negative_keep_returns_placeholder(value: str, keep: int) -> None:
    """Per the source spec: negative keep is clamped to 0; zero keep returns
    the bare placeholder.
    """
    assert redact_secret_for_logging(value, keep=keep) == _REDACT_PLACEHOLDER


# ---------------------------------------------------------------------------
# Phase 5.7 - keep > len(value) Clamps to len // 2
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep", "expected"),
    [
        pytest.param("ab", 10, "a" + _REDACT_PLACEHOLDER, id="2-char-keep-10"),
        pytest.param("abc", 100, "a" + _REDACT_PLACEHOLDER, id="3-char-keep-100"),
        pytest.param("abcdef", 1000, "abc" + _REDACT_PLACEHOLDER, id="6-char-keep-1000"),
    ],
)
def test_redact_secret_for_logging_keep_exceeds_length_clamps(
    value: str, keep: int, expected: str
) -> None:
    """When ``keep`` exceeds the input length, clamp to ``len // 2``."""
    assert redact_secret_for_logging(value, keep=keep) == expected


# ---------------------------------------------------------------------------
# Phase 5.8 - Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep"),
    [
        pytest.param("sk-ant-deadbeef", 4, id="default-keep"),
        pytest.param("super-secret-key", 8, id="keep-8"),
        pytest.param("short", 4, id="clamped-keep"),
        pytest.param("", 4, id="empty"),
    ],
)
def test_redact_secret_for_logging_is_deterministic(value: str, keep: int) -> None:
    """The same input MUST always produce the same output."""
    first = redact_secret_for_logging(value, keep=keep)
    second = redact_secret_for_logging(value, keep=keep)
    third = redact_secret_for_logging(value, keep=keep)
    assert first == second == third


# ---------------------------------------------------------------------------
# Phase 5.9 - No Exceptions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "keep"),
    [
        pytest.param(None, 4, id="none-default"),
        pytest.param(None, 0, id="none-zero"),
        pytest.param(None, -5, id="none-negative"),
        pytest.param(42, 4, id="int-default"),
        pytest.param([], 4, id="list-default"),
        pytest.param("", 4, id="empty-default"),
        pytest.param("a", 0, id="single-char-zero"),
        pytest.param("very-long-secret", 1000, id="over-large-keep"),
    ],
)
def test_redact_secret_for_logging_never_raises(value: Any, keep: int) -> None:
    """All edge case inputs MUST NOT raise."""
    result = redact_secret_for_logging(value, keep=keep)
    assert isinstance(result, str)
