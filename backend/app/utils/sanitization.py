"""Server-side input sanitization helpers for AI prompts and logs.

This module defines two pure helpers used at the edges of the
Sales-Connections backend to prevent prompt-injection-style abuse and
accidental secret leakage:

- ``sanitize_for_ai_prompt(text, max_chars=4000)``  Cleans free-form
  user-supplied text BEFORE it is templated into an Anthropic Claude
  prompt. Strips ASCII control characters (except ``\\t\\n\\r``),
  Unicode bidi / zero-width / private-use characters, and applies a
  length cap.

- ``redact_secret_for_logging(value, keep=4)``  Renders a partial
  representation of a credential string for log lines that require
  SOME visibility (e.g., "the first 4 characters of the API key in
  use") without disclosing the full secret.

Per AAP Section 0.4.4 and Section 0.7.4, sanitization is the last
line of defense between the API surface and Anthropic's API. Although
pydantic enforces field-level type and length constraints, this module
is responsible for the byte-level cleanliness guarantees that pydantic
cannot express:

- No raw ASCII control bytes (e.g., ``\\x00``, ``\\x07``, ``\\x1b``)
   reach the Claude prompt.
- No Unicode-level bidi tricks (e.g., U+202E ``RIGHT-TO-LEFT OVERRIDE``)
   reach the Claude prompt.
- No zero-width whitespace (e.g., U+200B, U+FEFF) reaches the Claude
   prompt to confuse downstream string comparisons or render
   differently in different terminals.

Module-level imports are limited to the standard library (``re``,
``unicodedata``) per the assigned folder conventions.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Default character cap for AI prompt context. Matches
# BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS in app.config so the
# configuration default and the parameter default agree.
_DEFAULT_AI_MAX_CHARS: int = 4000

# Suffix appended to truncated text so downstream AI consumers and
# debuggers can see that truncation occurred. The suffix itself is
# included in the final ``max_chars`` budget.
_TRUNCATION_SUFFIX: str = "... [truncated]"

# Match ASCII control characters EXCEPT tab (0x09), line feed (0x0A),
# and carriage return (0x0D). Also include 0x7F (DEL).
_ASCII_CONTROL_RE: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Unicode characters that are invisible or alter rendering direction.
# Removing them defeats prompt-injection patterns that smuggle hidden
# instructions inside otherwise-innocuous user text.
_INVISIBLE_UNICODE_CHARS: frozenset[str] = frozenset(
    {
        # Zero-width characters
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\u2060",  # WORD JOINER
        "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
        # Bidirectional formatting characters
        "\u200e",  # LEFT-TO-RIGHT MARK
        "\u200f",  # RIGHT-TO-LEFT MARK
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE
        "\u2066",  # LEFT-TO-RIGHT ISOLATE
        "\u2067",  # RIGHT-TO-LEFT ISOLATE
        "\u2068",  # FIRST STRONG ISOLATE
        "\u2069",  # POP DIRECTIONAL ISOLATE
        # Soft hyphen and other invisible formatting
        "\u00ad",  # SOFT HYPHEN
        "\u034f",  # COMBINING GRAPHEME JOINER
        # Object replacement / interlinear annotation characters
        "\ufffc",  # OBJECT REPLACEMENT CHARACTER
        "\ufff9",  # INTERLINEAR ANNOTATION ANCHOR
        "\ufffa",  # INTERLINEAR ANNOTATION SEPARATOR
        "\ufffb",  # INTERLINEAR ANNOTATION TERMINATOR
    }
)

# Match runs of two or more whitespace characters. Used to collapse
# whitespace into a single space after control-character removal.
_MULTIPLE_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s{2,}")

# Default number of characters of a secret to leave un-redacted at the
# start of the value when calling ``redact_secret_for_logging``. Four
# characters is enough to disambiguate which key/credential a log line
# is referring to without disclosing the secret itself.
_DEFAULT_REDACT_KEEP: int = 4

# The placeholder text appended after the kept prefix when redacting.
_REDACT_PLACEHOLDER: str = "***REDACTED***"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "redact_secret_for_logging",
    "sanitize_for_ai_prompt",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _strip_invisible_unicode(text: str) -> str:
    """Remove zero-width, bidi, and other invisible Unicode characters.

    Walks the input character by character and excludes:

    1. Any character in ``_INVISIBLE_UNICODE_CHARS`` (the explicit
       allow-list of bidi / zero-width characters).
    2. Any character whose Unicode general category is ``Cf``
       (Format) -- this catches additions to the format class in
       future Unicode versions without requiring a code update.
    3. Any character whose category is ``Co`` (Private Use) -- these
       render inconsistently across fonts and have no place in
       Sales-Connections free text.

    Returns:
        A new string with the offending characters removed.
    """
    # Fast path: skip the per-character walk if the input contains no
    # candidates for removal.
    if not text:
        return text
    result: list[str] = []
    for ch in text:
        if ch in _INVISIBLE_UNICODE_CHARS:
            continue
        category = unicodedata.category(ch)
        if category in ("Cf", "Co"):
            continue
        result.append(ch)
    return "".join(result)


def _truncate_with_suffix(text: str, max_chars: int) -> str:
    """Truncate *text* to *max_chars* characters, appending a marker.

    The returned string is never longer than ``max_chars``. If the
    input already fits, it is returned unchanged. If ``max_chars`` is
    too small to fit the truncation suffix, a hard truncation without
    the suffix is performed.

    Args:
        text: The string to (potentially) truncate.
        max_chars: Maximum allowed length, in characters. Values
            less than or equal to zero produce the empty string.

    Returns:
        The truncated string, with a length guaranteed to be at most
        ``max_chars``.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix_len = len(_TRUNCATION_SUFFIX)
    if max_chars <= suffix_len:
        # Not enough room for the suffix; hard-truncate.
        return text[:max_chars]
    cutoff = max_chars - suffix_len
    return text[:cutoff] + _TRUNCATION_SUFFIX


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def sanitize_for_ai_prompt(
    text: str,
    max_chars: int = _DEFAULT_AI_MAX_CHARS,
) -> str:
    """Clean free-form text before templating into an AI prompt.

    The cleaning pipeline (in order):

    1. Coerce ``None`` and other non-string inputs to the empty string.
    2. Strip ASCII control characters (codes 0x00-0x08, 0x0B, 0x0C,
       0x0E-0x1F, 0x7F). The whitespace controls 0x09 (tab),
       0x0A (newline), and 0x0D (carriage return) are preserved.
    3. Apply Unicode NFC normalization so visually identical characters
       have identical byte representations (defends against canonical-
       equivalence smuggling).
    4. Strip zero-width, bidi, format, and private-use Unicode
       characters via ``_strip_invisible_unicode``.
    5. Collapse runs of two or more whitespace characters (after
       removals) into a single space, then strip leading/trailing
       whitespace.
    6. Truncate the result to ``max_chars`` characters, appending a
       visible "[truncated]" suffix when truncation occurs.

    Args:
        text: The raw user-supplied free-form text. ``None`` and
            non-string values are tolerated and coerced to ``""``.
        max_chars: Maximum number of characters allowed in the
            returned string. Defaults to 4000, matching
            ``app.config.BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS``.

    Returns:
        The sanitized string, ready to be templated into a Claude
        prompt. Always a ``str``, never ``None``. Length is at most
        ``max_chars``.

    This function is pure: it does not mutate inputs and has no side
    effects. It NEVER raises for any input type.

    Examples:
        >>> sanitize_for_ai_prompt("Hello\\x00world")
        'Helloworld'
        >>> sanitize_for_ai_prompt("Tab\\there\\nand newline")
        'Tab\\there\\nand newline'
        >>> sanitize_for_ai_prompt(None)
        ''
        >>> # When ``max_chars`` is too small to fit the truncation
        >>> # suffix ("... [truncated]" is 15 chars), the helper hard-
        >>> # truncates without the suffix to honor the cap exactly.
        >>> sanitize_for_ai_prompt("a" * 5000, max_chars=10)
        'aaaaaaaaaa'
        >>> # When ``max_chars`` accommodates the suffix, the suffix is
        >>> # appended so callers can see that truncation occurred.
        >>> sanitize_for_ai_prompt("a" * 5000, max_chars=20)
        'aaaaa... [truncated]'
    """
    # Step 1: coerce non-string input. The ``text: str`` annotation
    # advertises the intended type to typed callers, but the function
    # is also called from boundary code where the value originates
    # outside our type system (e.g., raw JSON payloads). The defensive
    # check below is intentional; mypy considers the body unreachable
    # under strict typing, hence the targeted ignore on the return.
    if not isinstance(text, str):
        return ""  # type: ignore[unreachable]

    # Step 2: strip ASCII control characters (preserve \t \n \r).
    cleaned = _ASCII_CONTROL_RE.sub("", text)

    # Step 3: Unicode NFC normalization.
    cleaned = unicodedata.normalize("NFC", cleaned)

    # Step 4: strip invisible / bidi / format / private-use characters.
    cleaned = _strip_invisible_unicode(cleaned)

    # Step 5: collapse multi-whitespace runs and trim.
    cleaned = _MULTIPLE_WHITESPACE_RE.sub(" ", cleaned).strip()

    # Step 6: enforce length cap.
    return _truncate_with_suffix(cleaned, max_chars)


def redact_secret_for_logging(
    value: str,
    keep: int = _DEFAULT_REDACT_KEEP,
) -> str:
    """Render a partial representation of a secret for log output.

    This helper is intended for the rare case where a log line MUST
    reveal SOME identifying information about a credential (e.g., to
    disambiguate which API key is currently active across multiple
    tenants) without disclosing the secret itself.

    The PRIMARY defense against secret leakage in logs is the
    whole-key redaction processor in ``app.observability.logging``,
    which replaces values for keys named ``*_key``, ``*_secret``,
    ``password``, ``token``, ``authorization`` with
    ``***REDACTED***``. Use this helper ONLY when the structured-log
    processor cannot apply (e.g., a secret value embedded in a
    free-form message string).

    Args:
        value: The secret string to render. ``None``, empty, and
            non-string inputs are tolerated and produce
            ``"***REDACTED***"`` (no leakage).
        keep: Number of leading characters to leave un-redacted.
            Defaults to 4. Negative values and values greater than
            the input length are clamped: ``keep`` is capped at
            ``min(keep, len(value) // 2)`` so that very short
            secrets are never partially exposed.

    Returns:
        A string of the form ``"abcd***REDACTED***"`` where
        ``abcd`` is the leading slice of length up to ``keep``.

    Examples:
        >>> redact_secret_for_logging("sk-ant-abcdefg")
        'sk-a***REDACTED***'
        >>> # ``keep=8`` is clamped to ``len(value) // 2 == 7`` so the
        >>> # half-secret invariant is preserved even on long inputs.
        >>> redact_secret_for_logging("sk-ant-abcdefg", keep=8)
        'sk-ant-***REDACTED***'
        >>> # Short input: keep=4 clamped to 5 // 2 == 2.
        >>> redact_secret_for_logging("short")
        'sh***REDACTED***'
        >>> redact_secret_for_logging("")
        '***REDACTED***'
        >>> redact_secret_for_logging(None)
        '***REDACTED***'
    """
    if not isinstance(value, str) or not value:
        return _REDACT_PLACEHOLDER

    effective_keep = max(0, keep)
    # Never expose more than half of a short secret.
    effective_keep = min(effective_keep, len(value) // 2)
    if effective_keep <= 0:
        return _REDACT_PLACEHOLDER
    return value[:effective_keep] + _REDACT_PLACEHOLDER
