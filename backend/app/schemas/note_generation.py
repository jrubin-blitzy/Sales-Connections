"""Pydantic 2.x AI note generation schemas (F-002).

This module defines the request/response shapes for ``POST /api/notes/generate``,
the F-002 AI Note Generation endpoint. The schemas:

- ``NoteGenerationRequest``   Inbound payload carrying the contributor's
                              free-form relationship context. Length-
                              capped at ``AI_PROMPT_CONTEXT_MAX_CHARS``
                              (4000 chars) to align with the timeout
                              budget and the prompt-window economics.
- ``NoteGenerationResponse``  Outbound payload carrying the AI-generated
                              outreach notes, the Claude model used,
                              and the generation timestamp.

Per AAP Section 0.1.1, the F-002 endpoint is bound by a 5-second P95
latency budget. The schema's length cap on ``relationship_context``
is one of three layered defenses (the others are
``app.utils.sanitization.sanitize_for_ai_prompt`` and the watchdog
timer in ``app.services.ai_orchestration``) that keep this budget
achievable.

Per AAP Section 0.4.4, AI failure does NOT roll back form submission;
the SPA's two-call flow is:

    1. (optional) POST /api/notes/generate -> NoteGenerationResponse
    2. POST /api/connections (with the user-edited ai_notes embedded)

This means ``NoteGenerationRequest`` is decoupled from
``ConnectionCreate``: the contributor can call the AI endpoint zero,
one, or many times before submitting the form, and is free to edit
the resulting text or replace it entirely.

Module-level imports are limited to the standard library
(``typing``) and pydantic; the schema is intentionally side-effect
free and contains no business logic. Sanitization is performed by
``app.utils.sanitization.sanitize_for_ai_prompt`` AFTER pydantic
validation passes; it is NOT invoked here.

Note on ``AwareDatetime`` import: pydantic v2 evaluates field type
hints at class-definition time (via ``get_type_hints``) to wire up
its validators, so ``AwareDatetime`` MUST be present at runtime even
though it appears only in type annotations. The ``# noqa: TC002``
suppression on the pydantic import line documents this constraint
and prevents ruff's flake8-type-checking strict mode from moving
the import into a ``TYPE_CHECKING`` block (which would break the
``NoteGenerationResponse`` schema at first use).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field  # noqa: TC002

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Maximum characters for the relationship_context field. MUST equal
# ``BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS`` in ``app.config`` AND
# ``_DEFAULT_AI_MAX_CHARS`` in ``app.utils.sanitization``. Drift
# between these three constants is a defect.
_RELATIONSHIP_CONTEXT_MAX_CHARS: int = 4000

# Maximum characters for the AI-generated notes returned in the
# response. Claude's 512-token output budget translates to roughly
# 2000 English characters in practice; we cap at 8000 to give
# headroom for verbose Claude responses while still bounding the
# response payload size.
_AI_NOTES_MAX_CHARS: int = 8000

# Maximum characters for the model identifier string in the response
# (e.g., "claude-sonnet-4-5"). Capping at 128 chars protects against
# malformed responses from a future Anthropic SDK release.
_MODEL_NAME_MAX_CHARS: int = 128

# Per the assigned folder Conventions: every inbound schema rejects
# unexpected keys and strips whitespace.
_STRICT_CONFIG: ConfigDict = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True,
)

# Outbound config strips whitespace but does NOT reject extra keys.
# The handler controls what is serialized; the consumer (SPA) tolerates
# additional fields gracefully so that future additive changes do not
# break existing clients.
_OUTBOUND_CONFIG: ConfigDict = ConfigDict(
    str_strip_whitespace=True,
)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "NoteGenerationRequest",
    "NoteGenerationResponse",
]


# ---------------------------------------------------------------------------
# Inbound: NoteGenerationRequest
# ---------------------------------------------------------------------------


class NoteGenerationRequest(BaseModel):
    """Inbound payload for ``POST /api/notes/generate`` (F-002).

    The contributor supplies free-form ``relationship_context`` (e.g.,
    "We went to college together, he's now VP of Ops at a Series B
    logistics startup" per the user's example in AAP Section 0.1.2).
    The handler validates this schema, then delegates to
    ``app.services.ai_orchestration.generate_notes``, which:

    1. Calls ``sanitize_for_ai_prompt`` to strip control characters
       and apply a server-side length cap (defense in depth on top
       of this schema's max_length).
    2. Templates the sanitized text into a Claude system prompt.
    3. Invokes Langchain's ``ChatAnthropic`` with a 5-second watchdog.
    4. Returns the generated text as ``NoteGenerationResponse``.

    Field-length defenses:
        relationship_context  Min 1 char (empty input is meaningless
                              for AI generation; reject early to save
                              a Claude round-trip).
                              Max 4000 chars (matches
                              ``AI_PROMPT_CONTEXT_MAX_CHARS``).

    Whitespace handling:
        ``str_strip_whitespace=True`` runs BEFORE length validation,
        so a payload of ``"   "`` becomes ``""`` post-strip and is
        rejected by ``min_length=1`` with a clear field-scoped error
        rather than passing through to the AI service.

    Per AAP Section 0.1.2, AI is provider-replaceable:

        > "Anthropic Claude API (or equivalent)."

    The Langchain wrapper in ``app.services.ai_orchestration`` makes
    the provider swap a config change, NOT a schema change.
    """

    model_config = _STRICT_CONFIG

    relationship_context: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_RELATIONSHIP_CONTEXT_MAX_CHARS,
            description=(
                "Free-form description of the contributor's relationship "
                "to the connection target. Capped at "
                f"{_RELATIONSHIP_CONTEXT_MAX_CHARS} characters to align "
                "with the AI request timeout budget."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Outbound: NoteGenerationResponse
# ---------------------------------------------------------------------------


class NoteGenerationResponse(BaseModel):
    """Response shape for ``POST /api/notes/generate``.

    Carries the AI-generated outreach notes back to the SPA along with
    provenance metadata so the contributor knows which model produced
    the suggestion and when. The SPA renders the ``ai_notes`` value
    into an editable textarea on the AddEditConnectionForm; the
    contributor may edit the text before submitting via
    ``POST /api/connections``.

    Fields:
        ai_notes      The AI-generated text. Has been server-side
                      sanitized (``app.utils.sanitization``) before
                      being returned, so it is safe to render directly
                      into a ``textarea`` element. Capped at 8000
                      chars to bound response payload size.
        model         The Claude model identifier used to generate
                      this output (e.g., "claude-sonnet-4-5"). Comes
                      from ``app.config.BaseConfig.ANTHROPIC_MODEL``.
                      Surfaced to the SPA so the UI can show "Generated
                      by Claude Sonnet 4.5" if desired.
        generated_at  Timezone-aware UTC timestamp recorded when the
                      AI response was received. Typed as
                      ``pydantic.AwareDatetime`` so naive datetimes
                      (``tzinfo is None``) are rejected at validation
                      time, enforcing the assigned-folder convention
                      "Datetime fields use ``datetime`` (timezone-aware
                      UTC) - never naive datetimes."

    No ``request_id`` or ``correlation_id`` is included in the response
    body; correlation flows through the ``X-Correlation-Id`` HTTP
    header per AAP Section 0.4.3.
    """

    model_config = _OUTBOUND_CONFIG

    ai_notes: Annotated[
        str,
        Field(
            max_length=_AI_NOTES_MAX_CHARS,
            description=(
                "AI-generated outreach notes ready for the contributor to "
                "review and optionally edit before submission."
            ),
        ),
    ]
    model: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_MODEL_NAME_MAX_CHARS,
            description=(
                "Identifier of the AI model that produced this output "
                "(e.g., 'claude-sonnet-4-5'). Informational only; the SPA "
                "does not branch logic on this value."
            ),
        ),
    ]
    generated_at: AwareDatetime
