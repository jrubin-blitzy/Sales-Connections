"""AI orchestration for outreach-note generation (F-002).

Wraps Anthropic Claude via Langchain's ``ChatAnthropic`` provider so the
upstream provider can be replaced without changing feature handlers. Per
AAP Section 0.7.7 (provider-replaceability invariant), this module is the
SOLE importer of the ``anthropic`` and ``langchain_anthropic`` packages
across the backend; all feature handlers reach Claude through
``generate_outreach_notes`` exclusively.

Latency budget: 5 seconds P95 end-to-end (AAP Section 0.7.3). The
timeout watchdog is enforced at TWO layers as defense-in-depth:

1. The Anthropic SDK supports a per-request ``timeout`` parameter that
   short-circuits an HTTP request that exceeds the configured budget.
2. A ``concurrent.futures`` watchdog runs the SDK call in a bounded
   thread pool and hard-cancels the future when the SDK-level timeout
   fails to fire (e.g., the underlying TCP socket is stuck waiting on
   a half-open connection that never returns).

Outcome telemetry is emitted via the ``ai_request_duration_seconds``
prometheus histogram with one of four ``outcome`` labels - ``success``,
``timeout``, ``error``, ``validation`` - which power the 5s P95 alarm
and let operators alert on each failure mode independently.

Security:

- ``ANTHROPIC_API_KEY`` is read from Flask config (sourced from AWS
  Secrets Manager in production; from ``.env`` in development). It
  never crosses the SPA boundary and never appears in log output.
- User-supplied ``relationship_context`` is sanitized via
  ``app.utils.sanitization.sanitize_for_ai_prompt`` BEFORE being
  templated into the prompt. The sanitizer also enforces the
  ``AI_PROMPT_CONTEXT_MAX_CHARS`` length cap.
- Empty or whitespace-only context yields a ``ValidationFailedError``
  before any external call is made, which the
  ``app.middleware.error_handlers`` registry maps to HTTP 422.
- Provider failures (timeout, error, misconfiguration) raise
  ``AIServiceUnavailableError`` carrying a per-instance
  ``status_code`` (504/503/502) and stable ``error_code``
  (``ai_timeout``/``ai_not_configured``/``ai_error``); the API layer
  surfaces this as a non-blocking warning per AAP Section 0.4.4 (AI
  failure must NOT block form submission).

Architectural notes:

- The Anthropic and Langchain imports are deliberately performed
  LOCALLY inside ``_call_chat_anthropic`` (not at module scope) so
  that importing this module does not pull the heavy SDK into the
  module-import graph of consumers. This keeps test startup fast and
  makes the provider-replaceability invariant visible at the
  import-graph level.
- All Flask configuration values are resolved in the calling thread
  (in ``generate_outreach_notes``) and passed down as plain
  arguments to ``_invoke_with_timeout`` and ``_call_chat_anthropic``.
  The thread-pool worker thread does NOT have a Flask app context and
  must not call ``current_app``.
"""

from __future__ import annotations

# Standard library imports.
#
# ``concurrent.futures.CancelledError`` is caught defensively when an
# external graceful-shutdown handler cancels the in-flight Future.
# ``ThreadPoolExecutor`` is the bounded pool that hosts the
# watchdog-protected SDK call. ``TimeoutError`` (aliased to
# ``FuturesTimeoutError`` so it does not collide with the built-in)
# is raised by ``Future.result(timeout=...)`` when the watchdog fires.
# ``Future`` itself is imported under ``TYPE_CHECKING`` (see below)
# because it is used only as a type annotation.
#
# ``datetime.UTC`` is the Python 3.11+ alias for ``timezone.utc`` used
# to construct timezone-aware UTC timestamps for the response.
#
# ``threading.current_thread`` surfaces the Gunicorn worker thread
# name on log lines so operators can diagnose thread-pool exhaustion
# or per-thread anomalies in production.
#
# ``time.perf_counter`` is the monotonic high-resolution clock used
# to measure elapsed seconds for the request-duration histogram and
# the structured-log ``elapsed_seconds`` field.
from concurrent.futures import (
    CancelledError,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from datetime import UTC, datetime
import threading
import time
from typing import TYPE_CHECKING, Any

# Third-party runtime imports. ``current_app`` resolves Flask
# configuration in the calling thread (the thread-pool worker thread
# has no Flask app context and MUST NOT call ``current_app``).
# ``structlog`` provides the JSON structured logger that the
# observability stack routes through the correlation/auth middleware
# contextvars so log lines emitted here carry the correlation_id,
# user_id, and org_id automatically.
from flask import current_app
import structlog

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration (relative imports are banned).
#
# ``AppError`` is the base class of the ``AIServiceUnavailableError``
# subclass defined below. ``ValidationFailedError`` is raised when
# the relationship_context reduces to empty after sanitization.
# ``ai_request_duration_seconds`` is the prometheus histogram observed
# on every AI request with one of four ``outcome`` labels.
# ``NoteGenerationResponse`` is the response shape returned on the
# happy path. ``sanitize_for_ai_prompt`` is the byte-level cleanliness
# guarantee applied before any user-supplied text reaches the
# Anthropic SDK.
from app.middleware.error_handlers import (
    AppError,
    ValidationFailedError,
)
from app.observability.metrics import ai_request_duration_seconds
from app.schemas.note_generation import NoteGenerationResponse
from app.utils.sanitization import sanitize_for_ai_prompt

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are strings (PEP 563) and these symbols are never
# evaluated at runtime, satisfying the project's strict
# ``flake8-type-checking`` configuration. ``Future`` annotates the
# return type of ``_AI_THREAD_POOL.submit``; ``NoteGenerationRequest``
# annotates the ``request`` parameter of ``generate_outreach_notes``.
if TYPE_CHECKING:
    from concurrent.futures import Future

    from app.schemas.note_generation import NoteGenerationRequest

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# structlog's ``merge_contextvars`` processor (configured in
# ``app.observability.logging``) automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id`` bound
# by the correlation/auth middleware so log lines emitted here are
# automatically correlated by request without any per-call work.
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants - configuration defaults
# ---------------------------------------------------------------------------
# These constants serve as fallbacks when the corresponding Flask
# configuration key is absent. Production deployments source the
# values from AWS Secrets Manager via ``app.config.ProductionConfig``;
# development reads from ``.env``; testing uses ephemeral overrides.
# Drift between these defaults and ``app.config.BaseConfig`` is a defect
# that the ``test_config.py`` suite is responsible for catching.

# Default end-to-end timeout for the AI call. Matches the F-002 P95
# budget documented in AAP Section 0.7.3.
_DEFAULT_AI_TIMEOUT_SECONDS: int = 5

# Default Anthropic ``max_tokens`` value. 512 tokens translates to
# roughly 2000 English characters in practice, which is enough room
# for the 3-5 bullet talking-point response shape mandated by the
# system prompt below.
_DEFAULT_AI_MAX_TOKENS: int = 512

# Default Claude model identifier. Aligned with the ``.env.example``
# default and ``app.config.BaseConfig.ANTHROPIC_MODEL`` per AAP
# Section 0.5.2 Layer 3 ("the chosen Claude model").
_DEFAULT_AI_MODEL: str = "claude-sonnet-4-5"

# Default character cap for the relationship_context value passed
# through to the sanitizer. Matches
# ``app.config.BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS`` and
# ``app.utils.sanitization._DEFAULT_AI_MAX_CHARS``.
_DEFAULT_AI_PROMPT_CONTEXT_MAX_CHARS: int = 4000

# Watchdog grace period above the SDK timeout. The SDK's internal
# timeout fires first; if it fails to fire (network stall, hung
# socket), the ``concurrent.futures`` watchdog cancels the future
# this many seconds later as a hard ceiling on AI request latency.
_WATCHDOG_GRACE_SECONDS: float = 0.5

# Bounded thread pool for AI calls. The thread pool is the
# infrastructure that lets the calling thread enforce a hard
# wall-clock timeout via ``Future.result(timeout=...)``. Real
# concurrency at the application layer comes from Gunicorn worker
# count; this pool is a watchdog mechanism, not a load-distribution
# mechanism. Bounding ``max_workers`` to 8 prevents a flood of AI
# requests from exhausting threads and starving other endpoints.
_AI_THREAD_POOL: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="ai-orch",
)


# ---------------------------------------------------------------------------
# Module-level constants - prompt templates
# ---------------------------------------------------------------------------
# The system prompt frames Claude as a sales-outreach copywriter per
# AAP Section 0.5.2 Layer 3. The text is plain ASCII (no smart quotes,
# no emoji) so the encoded byte representation is stable across
# editors and CI environments. Per AAP Section 0.7.5 (Explainability
# rule), changes to this prompt MUST be accompanied by a decision-log
# entry in ``docs/decision-log.md`` because the prompt is a behavioral
# contract observable to end users.
_SYSTEM_PROMPT: str = (
    "You are a senior B2B sales-development copywriter helping a colleague "
    "draft warm, concise outreach talking points. The colleague will provide "
    "a brief description of their personal relationship to a prospective "
    "lead. Your task is to convert that relationship context into a small "
    "set of edit-ready talking points the colleague can paste into an email "
    "or DM.\n\n"
    "Guidelines:\n"
    "- Produce three to five short bullet points.\n"
    "- Each bullet point should be one or two sentences.\n"
    "- Be warm, specific, and grounded ONLY in the provided context.\n"
    "- Do NOT invent facts about the prospect or claim achievements that "
    "are not stated in the context.\n"
    "- Do NOT include greetings, sign-offs, or boilerplate.\n"
    "- Output plain text bullets prefixed by '- '. No Markdown headings, "
    "no numbered lists, no emoji.\n"
)


# ---------------------------------------------------------------------------
# Module-level constants - prometheus outcome labels
# ---------------------------------------------------------------------------
# The four outcome labels recorded on ``ai_request_duration_seconds``
# enable independent alerting on each failure mode. ``success`` is the
# happy path; ``timeout`` indicates provider unavailability;
# ``error`` indicates an exception from the SDK or HTTP layer;
# ``validation`` indicates the sanitizer reduced the input to an empty
# string and the call was skipped before reaching the provider.
_OUTCOME_SUCCESS: str = "success"
_OUTCOME_TIMEOUT: str = "timeout"
_OUTCOME_ERROR: str = "error"
_OUTCOME_VALIDATION: str = "validation"


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------
# ``__all__`` is sorted alphabetically (RUF022 "isort-style"). The
# public surface is intentionally minimal: a single class for the
# error case and a single function for the happy path. All other
# symbols in this module are private (underscore-prefixed) and not
# part of the contract with feature handlers.
__all__ = [
    "AIServiceUnavailableError",
    "generate_outreach_notes",
]


# ---------------------------------------------------------------------------
# Domain exception
# ---------------------------------------------------------------------------


class AIServiceUnavailableError(AppError):
    """Raised when the AI provider call cannot complete successfully.

    This exception unifies three distinct failure modes under a single
    application-level error class while preserving the per-mode HTTP
    status code and stable ``error_code`` that the SPA's typed
    ``ApiError`` dispatch consumes:

    - ``code="ai_timeout"`` / ``status_code=504`` -- the provider did
      not respond within the configured timeout.
    - ``code="ai_error"`` / ``status_code=502`` -- the provider
      responded with an error or the SDK raised a non-timeout
      exception.
    - ``code="ai_not_configured"`` / ``status_code=503`` -- the
      ``ANTHROPIC_API_KEY`` Flask config value is empty (the app was
      misconfigured at startup or the secret rotation failed).

    The frontend treats ``AIServiceUnavailableError`` as a
    non-blocking warning per AAP Section 0.4.4: the user can submit
    the form without AI notes. The SPA shows a "Generate AI Notes is
    currently unavailable" affordance and a manual retry button; it
    does NOT roll back the form submission.

    The base ``AppError.__init__`` accepts only ``message`` and
    ``fields``; this subclass takes ``code`` and ``status_code`` as
    keyword arguments and assigns them as INSTANCE attributes (which
    shadow the class attributes inherited from ``AppError``). The
    registered ``_handle_app_error`` handler in
    ``app.middleware.error_handlers`` reads ``status_code`` and
    ``error_code`` off the instance, so the per-instance overrides
    flow through to the JSON envelope automatically.

    Attributes:
        status_code: HTTP status code (504 / 503 / 502) -- shadows
            the class attribute set by the parent ``AppError``.
        error_code: Stable error code (``ai_timeout`` /
            ``ai_not_configured`` / ``ai_error`` /
            ``ai_unavailable``) -- shadows the class attribute set by
            the parent.
        message: User-facing message inherited from
            ``AppError.__init__``.
        fields: Empty list inherited from ``AppError.__init__`` -- AI
            failures do not have field-level error detail.
        default_message: Fallback message used by the parent class
            when no explicit ``message`` is supplied; overridden here
            to a domain-specific default so a misconfigured caller
            does not surface the generic ``AppError`` 500 message.
    """

    def __init__(
        self,
        *,
        message: str = "AI provider is currently unavailable.",
        code: str = "ai_unavailable",
        status_code: int = 504,
    ) -> None:
        """Initialize an AI provider failure exception.

        Args:
            message: User-facing message describing the failure mode.
                Defaults to a generic "currently unavailable" string;
                callers SHOULD pass a more specific message
                (e.g., "AI provider did not respond within the
                configured timeout.").
            code: Stable error code consumed by the SPA's typed
                ``ApiError`` dispatch. Expected values:
                ``ai_timeout`` (504), ``ai_error`` (502),
                ``ai_not_configured`` (503), ``ai_unavailable``
                (catch-all default).
            status_code: HTTP status code for the response envelope.
                504 indicates the provider timed out; 503 indicates
                misconfiguration; 502 indicates an upstream error.
        """
        # Assign the per-instance status_code and error_code BEFORE
        # delegating to the parent constructor. AppError.__init__
        # inspects only ``message`` and ``fields``, so the assignment
        # order is semantic-only (it documents the override pattern
        # for future readers); functionally either order works.
        # Instance attributes shadow the class attributes inherited
        # from AppError, which is the mechanism by which a generic
        # AppError handler (``_handle_app_error``) can dispatch the
        # correct envelope without per-subclass coordination.
        self.status_code: int = status_code
        self.error_code: str = code
        # AppError.__init__ stores ``message`` on ``self.message`` and
        # normalizes ``fields`` to a fresh ``list[dict]`` on
        # ``self.fields``. We pass ``fields=None`` because AI
        # failures do not carry field-level detail (those go through
        # ValidationFailedError instead).
        super().__init__(message=message, fields=None)

    @property
    def default_message(self) -> str:
        """Return the default user-facing message for this exception.

        Used by ``AppError.__init__`` when no explicit ``message`` is
        provided. Overrides the generic 500 message inherited from
        the base class so misconfigured callers surface a
        domain-meaningful default instead of leaking the generic
        ``AppError`` fallback.
        """
        return "AI provider is currently unavailable."


# ---------------------------------------------------------------------------
# Configuration accessors
# ---------------------------------------------------------------------------
# Each accessor reads exactly one configuration key and returns the
# typed value with a documented fallback. Centralising config reads
# in these tiny helpers (rather than scattering ``current_app.config``
# accesses across the module) makes it easy to:
#
# 1. Unit-test the accessors with a Flask app context override.
# 2. Audit the complete set of configuration keys this module
#    consumes without grepping the entire file.
# 3. Add cross-cutting validation (e.g., type coercion, range
#    checking) in one place if the shape ever needs hardening.
#
# All accessors must be called from within a Flask application
# context. The thread-pool worker thread does NOT have an app
# context, so callers MUST resolve all config values before calling
# ``_AI_THREAD_POOL.submit(...)``.


def _get_ai_timeout_seconds() -> float:
    """Return the configured AI request timeout in seconds.

    Reads ``AI_REQUEST_TIMEOUT_SECONDS`` from the Flask config, which
    is sourced from the ``AI_REQUEST_TIMEOUT_SECONDS`` environment
    variable in development and from AWS Secrets Manager (or the
    ECS task definition environment) in production. Defaults to 5
    seconds per AAP Section 0.7.3 ("AI note generation: <= 5 s P95").
    """
    return float(
        current_app.config.get(
            "AI_REQUEST_TIMEOUT_SECONDS",
            _DEFAULT_AI_TIMEOUT_SECONDS,
        )
    )


def _get_ai_max_tokens() -> int:
    """Return the configured Anthropic ``max_tokens`` budget.

    Reads ``ANTHROPIC_MAX_TOKENS`` from the Flask config. Capping
    ``max_tokens`` at the request layer bounds the input/output
    token cost and keeps the AI response payload size predictable.
    """
    return int(
        current_app.config.get(
            "ANTHROPIC_MAX_TOKENS",
            _DEFAULT_AI_MAX_TOKENS,
        )
    )


def _get_ai_model() -> str:
    """Return the configured Anthropic Claude model identifier.

    Reads ``ANTHROPIC_MODEL`` from the Flask config. The default
    matches the ``.env.example`` default and the
    ``app.config.BaseConfig.ANTHROPIC_MODEL`` class attribute.
    Updates to the default model SHOULD be accompanied by a
    decision-log entry per AAP Section 0.7.5 (Explainability rule).
    """
    return str(
        current_app.config.get(
            "ANTHROPIC_MODEL",
            _DEFAULT_AI_MODEL,
        )
    )


def _get_ai_prompt_context_max_chars() -> int:
    """Return the configured character cap for the AI prompt input.

    Reads ``AI_PROMPT_CONTEXT_MAX_CHARS`` from the Flask config. This
    cap is enforced by the sanitizer in
    ``app.utils.sanitization.sanitize_for_ai_prompt`` as the second
    of three layered defenses (the first is the pydantic schema's
    ``max_length`` constraint; the third is the SDK-level token
    budget set by ``ANTHROPIC_MAX_TOKENS``).
    """
    return int(
        current_app.config.get(
            "AI_PROMPT_CONTEXT_MAX_CHARS",
            _DEFAULT_AI_PROMPT_CONTEXT_MAX_CHARS,
        )
    )


def _get_anthropic_api_key() -> str:
    """Return the Anthropic API key from Flask config.

    Raises ``AIServiceUnavailableError(code="ai_not_configured",
    status_code=503)`` if the key is missing or empty. The SPA maps
    the 503 status to an "AI service is not configured" toast and
    surfaces the manual retry control. Production deployments load
    the key from AWS Secrets Manager at startup; development reads
    it from ``.env``. Either source must produce a non-empty value
    before this module can serve traffic.

    Returns:
        The non-empty Anthropic API key string.

    Raises:
        AIServiceUnavailableError: ``ANTHROPIC_API_KEY`` is empty.
    """
    key = current_app.config.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIServiceUnavailableError(
            message="Anthropic API key is not configured.",
            code="ai_not_configured",
            status_code=503,
        )
    return str(key)


# ---------------------------------------------------------------------------
# Public API: generate_outreach_notes
# ---------------------------------------------------------------------------


def generate_outreach_notes(
    request: NoteGenerationRequest,
    *,
    model: str | None = None,
) -> NoteGenerationResponse:
    """Generate outreach talking points from a relationship context (F-002).

    The two-call flow per AAP Section 0.4.4: the SPA calls
    ``POST /api/notes/generate`` first (which routes here), then sends
    the optional resulting ``ai_notes`` string in the
    ``POST /api/connections`` payload. AI failure does NOT roll back
    the form submission; the error is surfaced to the user as a
    non-blocking warning and the SPA's submit button remains enabled.

    Workflow:

        1. Resolve the model identifier (caller override or Flask config).
        2. Pass the user-supplied ``relationship_context`` through
           ``sanitize_for_ai_prompt`` to strip control characters,
           bidi/zero-width Unicode, and apply the
           ``AI_PROMPT_CONTEXT_MAX_CHARS`` length cap.
        3. If sanitization yields an empty string, observe a
           ``validation`` outcome on the prometheus histogram and
           raise ``ValidationFailedError`` (HTTP 422) without making
           any external call.
        4. Submit the SDK call to the bounded thread pool and wait
           for the result with a hard wall-clock timeout
           (``timeout_seconds + _WATCHDOG_GRACE_SECONDS``).
        5. On success, observe a ``success`` outcome and return the
           ``NoteGenerationResponse``.
        6. On timeout, observe a ``timeout`` outcome and raise
           ``AIServiceUnavailableError(code="ai_timeout",
           status_code=504)``.
        7. On any other exception, observe an ``error`` outcome and
           raise ``AIServiceUnavailableError(code="ai_error",
           status_code=502)``. The original exception is chained via
           ``__cause__`` so engineers can debug from CloudWatch via
           the correlation ID.

    Args:
        request: ``NoteGenerationRequest`` carrying the user's
            ``relationship_context`` (already pydantic-validated to
            1-4000 chars by the inbound schema). Field-level
            validation has already run; this function defends against
            byte-level abuse by re-running the sanitizer on the
            already-pydantic-validated value as defense-in-depth.
        model: Optional model override. When ``None`` (the typical
            case) the value is resolved from
            ``current_app.config["ANTHROPIC_MODEL"]``. The override
            exists so admin tooling can experiment with a specific
            model without restarting the application.

    Returns:
        ``NoteGenerationResponse`` carrying:

        - ``ai_notes``: the trimmed text produced by Claude, ready
          for the SPA to render into an editable textarea.
        - ``model``: the model identifier actually used for this
          call (informational; surfaced to the user if desired).
        - ``generated_at``: a timezone-aware UTC timestamp recorded
          when the response was received, satisfying the
          ``AwareDatetime`` constraint on the response schema.

    Raises:
        ValidationFailedError: ``relationship_context`` reduced to
            empty / whitespace-only after sanitization. Mapped to
            HTTP 422 by the registered error handler.
        AIServiceUnavailableError: provider call timed out (504),
            errored (502), or the API key is unconfigured (503).
            The frontend treats these as non-blocking warnings.
    """
    # Resolve the model identifier: caller override (rare) wins over
    # the Flask config default. Doing this first lets the bound logger
    # carry the model identifier on every subsequent log line for
    # this request.
    selected_model = model or _get_ai_model()

    # Sanitize the relationship_context BEFORE prompt construction,
    # per AAP Section 0.7.4 security invariant: "User-supplied
    # relationship context sanitized server-side before AI prompt."
    # The sanitizer is pure and never raises; an empty input simply
    # produces an empty output.
    sanitized = sanitize_for_ai_prompt(
        request.relationship_context,
        max_chars=_get_ai_prompt_context_max_chars(),
    )

    # Guard: an empty string post-sanitization indicates that the
    # original input contained ONLY control characters, bidi marks,
    # zero-width characters, or whitespace. Reject it immediately
    # without making a Claude round-trip; that round-trip would be
    # both pointless (no useful content to feed the model) and
    # billable. Observe the validation outcome on the histogram so
    # operators can spot abusive input patterns even though the
    # request never reached the provider.
    if not sanitized.strip():
        ai_request_duration_seconds.labels(outcome=_OUTCOME_VALIDATION).observe(0.0)
        raise ValidationFailedError(
            message="Relationship context is empty after sanitization.",
            fields=[{"field": "relationship_context", "code": "empty"}],
        )

    # Resolve remaining configuration in the calling thread (which
    # owns the Flask app context). The thread-pool worker thread has
    # no app context; passing the resolved values down as plain
    # arguments avoids a context-bleeding bug.
    timeout_s = _get_ai_timeout_seconds()
    max_tokens = _get_ai_max_tokens()

    # ``time.perf_counter()`` is a monotonic clock with the highest
    # available resolution; per the Python docs it is the right
    # choice for measuring elapsed time. The wall-clock
    # ``datetime.now()`` value is still recorded separately on the
    # response (``generated_at``) for audit/timeline use.
    started = time.perf_counter()

    # Bind per-request fields onto the structured logger. The bound
    # logger emits these on every subsequent log call without
    # re-passing them. NOTE: ``prompt_chars`` is the integer length;
    # the raw user-supplied text is NEVER logged at any level
    # (per AAP Section 0.7.4 security invariant on PII handling).
    # ``caller_thread`` aids in diagnosing thread-pool exhaustion
    # by surfacing which Gunicorn worker thread initiated the call.
    bound_logger = logger.bind(
        ai_model=selected_model,
        prompt_chars=len(sanitized),
        timeout_seconds=timeout_s,
        caller_thread=threading.current_thread().name,
    )
    bound_logger.info("ai_request_start")

    try:
        notes_text = _invoke_with_timeout(
            sanitized=sanitized,
            model=selected_model,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
    except FuturesTimeoutError as exc:
        # The watchdog fired (or the SDK raised its own timeout).
        # Map to HTTP 504 with stable code ``ai_timeout``.
        elapsed = time.perf_counter() - started
        ai_request_duration_seconds.labels(outcome=_OUTCOME_TIMEOUT).observe(elapsed)
        bound_logger.warning("ai_request_timeout", elapsed_seconds=elapsed)
        raise AIServiceUnavailableError(
            message="AI provider did not respond within the configured timeout.",
            code="ai_timeout",
            status_code=504,
        ) from exc
    except AIServiceUnavailableError:
        # ``_invoke_with_timeout`` may raise this directly (e.g., when
        # the API key is unconfigured) -- pass through unmodified so
        # the original code/status_code reach the response envelope.
        # Observation is attributed to the caller (validation already
        # observed in the ``not sanitized.strip()`` branch above; api
        # key absence is a configuration error, not a runtime error,
        # so we leave it unobserved here).
        raise
    except Exception as exc:
        # Any other exception class -- network error, SDK exception,
        # invalid response shape, etc. -- is mapped to HTTP 502 with
        # stable code ``ai_error``. The exception class name is
        # logged so engineers can identify the failure mode without
        # the full traceback (which the catch-all handler in
        # ``error_handlers`` will log separately at error level).
        elapsed = time.perf_counter() - started
        ai_request_duration_seconds.labels(outcome=_OUTCOME_ERROR).observe(elapsed)
        bound_logger.error(
            "ai_request_error",
            elapsed_seconds=elapsed,
            error_class=type(exc).__name__,
        )
        raise AIServiceUnavailableError(
            message="AI provider returned an error.",
            code="ai_error",
            status_code=502,
        ) from exc

    # Happy path: observe the success outcome and emit the success
    # log line carrying the elapsed time and the response character
    # count (NOT the raw text, which may contain user-identifying
    # detail and is the production output of the model).
    elapsed = time.perf_counter() - started
    ai_request_duration_seconds.labels(outcome=_OUTCOME_SUCCESS).observe(elapsed)
    bound_logger.info(
        "ai_request_success",
        elapsed_seconds=elapsed,
        response_chars=len(notes_text),
    )

    # Construct the response. ``generated_at`` is a timezone-aware
    # UTC datetime per the project-wide convention "Datetime fields
    # use ``datetime`` (timezone-aware UTC) -- never naive datetimes."
    # The ``AwareDatetime`` annotation on
    # ``NoteGenerationResponse.generated_at`` rejects naive values at
    # validation time, so any drift from this convention surfaces as
    # a pydantic validation error during response construction.
    return NoteGenerationResponse(
        ai_notes=notes_text,
        model=selected_model,
        generated_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Private helpers - timeout watchdog and provider call
# ---------------------------------------------------------------------------


def _invoke_with_timeout(
    *,
    sanitized: str,
    model: str,
    max_tokens: int,
    timeout_s: float,
) -> str:
    """Invoke the Anthropic-backed Langchain client with a hard timeout.

    Strategy:

    1. The Anthropic SDK supports a per-request ``timeout`` parameter
       passed through Langchain's ``ChatAnthropic`` wrapper. Under
       normal conditions this fires first and raises an SDK-internal
       timeout exception, which the calling function maps to
       ``AIServiceUnavailableError(code="ai_error")``.
    2. As defense-in-depth, the call runs in the bounded
       ``_AI_THREAD_POOL`` and the calling thread enforces
       ``timeout_s + _WATCHDOG_GRACE_SECONDS`` via
       ``Future.result(timeout=...)``. If the SDK ignores its own
       timeout (stuck socket, DNS hang, half-open TCP connection),
       the watchdog fires and the calling function maps it to
       ``AIServiceUnavailableError(code="ai_timeout")``.

    Args:
        sanitized: The relationship context already passed through
            ``sanitize_for_ai_prompt``. This helper does NOT
            re-sanitize; if the caller hands in raw user input the
            sanitization invariant is violated.
        model: The Claude model identifier to invoke.
        max_tokens: Anthropic ``max_tokens`` budget for the response.
        timeout_s: SDK-level per-request timeout in seconds. The
            watchdog uses ``timeout_s + _WATCHDOG_GRACE_SECONDS`` as
            the wall-clock budget.

    Returns:
        The plain-text bullets produced by Claude with leading and
        trailing whitespace stripped.

    Raises:
        AIServiceUnavailableError: ``ANTHROPIC_API_KEY`` is missing.
            (Other exceptions propagate to the caller for mapping.)
        FuturesTimeoutError: the watchdog fired before the future
            completed. The caller maps this to HTTP 504.
    """
    # Resolve the API key inside the calling thread; the key value
    # crosses the thread boundary as a plain string argument so the
    # worker thread does not need a Flask app context.
    api_key = _get_anthropic_api_key()

    # Floor the SDK-level timeout at 100 ms. A configured value of
    # 0 (or negative) would cause the SDK to fail-immediately on
    # every request; clamping defends against config typos that
    # would otherwise look like a global outage.
    sdk_timeout = max(0.1, timeout_s)

    # Submit the SDK call to the bounded thread pool. The Future
    # type annotation captures the ``str`` return type for tooling
    # transparency.
    future: Future[str] = _AI_THREAD_POOL.submit(
        _call_chat_anthropic,
        api_key=api_key,
        sanitized=sanitized,
        model=model,
        max_tokens=max_tokens,
        sdk_timeout=sdk_timeout,
    )
    try:
        # The watchdog's wall-clock budget is the SDK timeout plus
        # the grace window, so the SDK timeout fires first under
        # normal conditions and the watchdog only kicks in for
        # pathological cases (stuck sockets).
        text = future.result(timeout=timeout_s + _WATCHDOG_GRACE_SECONDS)
    except FuturesTimeoutError:
        # Best-effort cancellation of the submitted future. If the
        # underlying worker has already started the SDK call,
        # ``cancel()`` returns False and the call continues in the
        # background until the SDK's own timeout (or socket close)
        # terminates it; we discard the result. We re-raise so the
        # caller maps to ``ai_timeout``.
        future.cancel()
        raise
    except CancelledError as exc:
        # Defensive: if a graceful-shutdown handler or another
        # caller cancels the future from outside, ``result()``
        # raises ``CancelledError`` instead of returning a value.
        # Treat external cancellation identically to a timeout so
        # the failure mode is opaque to the consumer.
        raise FuturesTimeoutError("AI request future was cancelled") from exc
    return text.strip()


def _call_chat_anthropic(
    *,
    api_key: str,
    sanitized: str,
    model: str,
    max_tokens: int,
    sdk_timeout: float,
) -> str:
    """Issue the actual Anthropic call via Langchain ``ChatAnthropic``.

    This is the ONLY function in the entire backend codebase that
    imports ``langchain_anthropic`` and ``langchain_core.messages``.
    The imports are deliberately LOCAL (function-scope) so importing
    ``app.services.ai_orchestration`` does not pull Langchain into
    consumer module-import graphs unnecessarily, per AAP Section
    0.7.7 (provider-replaceability invariant).

    A future provider replacement (e.g., OpenAI, Google Gemini) will
    swap the body of this function while ``generate_outreach_notes``
    and ``AIServiceUnavailableError`` keep their public signatures
    stable. Feature handlers (``app.api.notes``,
    ``app.api.connections``) will require zero changes.

    Args:
        api_key: The Anthropic API key, already resolved from Flask
            config. Never logged.
        sanitized: Relationship context already passed through the
            sanitizer.
        model: Claude model identifier (e.g., ``claude-sonnet-4-5``).
        max_tokens: Anthropic ``max_tokens`` budget.
        sdk_timeout: Per-request timeout passed to the SDK.

    Returns:
        The text content extracted from the ``BaseMessage`` returned
        by ``ChatAnthropic.invoke``. Langchain may return content as
        a plain string OR as a list of structured content blocks
        (the latter is more common in newer SDK versions); both
        shapes are handled.

    Raises:
        RuntimeError: the SDK returned ``None`` content or an
            unrecognized content shape. Caught by the caller and
            mapped to ``AIServiceUnavailableError(code="ai_error")``.
        Exception: any SDK or network-level exception propagates to
            the caller for mapping.
    """
    # Local (function-scope) imports for the Anthropic / Langchain
    # SDK per the function docstring's provider-replaceability
    # rationale. Importing these inside the function body keeps the
    # heavy SDK out of the module-import graph for consumers that
    # only need the public surface (generate_outreach_notes /
    # AIServiceUnavailableError) -- those imports complete in
    # microseconds while the SDK itself takes hundreds of
    # milliseconds to import. The PLC0415 suppressions document the
    # deliberate departure from the "imports at top of file" rule.
    from langchain_anthropic import ChatAnthropic  # noqa: PLC0415
    from langchain_core.messages import (  # noqa: PLC0415
        HumanMessage,
        SystemMessage,
    )

    # Construct the client with the agent's mandated parameters. The
    # ``timeout`` keyword is the public alias for the
    # ``default_request_timeout`` field on the underlying pydantic
    # model; ``api_key`` is the alias for ``anthropic_api_key``
    # (typed as ``SecretStr`` internally but the model is configured
    # with ``populate_by_name=True`` so the plain ``str`` form is
    # accepted at runtime); ``max_tokens`` is the alias for
    # ``max_tokens_to_sample``. We use the public aliases to keep
    # the surface stable across langchain-anthropic minor version
    # upgrades.
    #
    # The ``# type: ignore`` suppression covers two pydantic-alias
    # interactions that mypy cannot fully resolve without the
    # pydantic-mypy plugin (which the project does not enable per
    # AAP s 0.7.7 mypy override): the runtime accepts BOTH the
    # field name and the alias, but mypy's view of the
    # pydantic-generated ``__init__`` signature only includes one
    # form per field. The runtime behavior is exactly as documented
    # by the SDK; the suppression only silences the false-positive
    # mypy errors.
    client = ChatAnthropic(  # type: ignore[call-arg]
        model=model,
        api_key=api_key,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        timeout=sdk_timeout,
        # ``max_retries=0`` because the AAP does not specify retry
        # semantics and silent retries would extend the latency
        # budget invisibly. The SPA can offer a manual "Retry AI"
        # button if the user wants explicit retry behavior.
        max_retries=0,
    )
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_build_user_prompt(sanitized)),
    ]
    response = client.invoke(messages)

    # Langchain's BaseMessage.content is documented as ``str | list``.
    # Older versions returned strings; newer versions sometimes return
    # a list of dict-shaped content blocks (e.g., ``[{"type": "text",
    # "text": "..."}]``) to support multi-modal responses. We handle
    # both shapes defensively. The ``Any`` annotation captures the
    # union without forcing the caller to import langchain types.
    content: Any = getattr(response, "content", None)
    if content is None:
        raise RuntimeError("Anthropic returned no content.")
    if isinstance(content, list):
        # Langchain may return a list of content blocks; concatenate
        # the text parts. Non-text blocks (e.g., tool calls) are
        # ignored because the F-002 contract is plain-text bullets.
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    if not isinstance(content, str):
        # An unrecognized content shape from the SDK. The caller
        # maps this to ``AIServiceUnavailableError(code="ai_error")``.
        raise RuntimeError(f"Anthropic returned unexpected content type: {type(content).__name__}")
    return content


def _build_user_prompt(sanitized_context: str) -> str:
    """Assemble the user-message body for the chat call.

    The system prompt (see ``_SYSTEM_PROMPT``) frames the role and
    output format; the user message contains the relationship
    context plus a short instruction reinforcing the format.

    Splitting the framing (system) from the content (user) follows
    Anthropic's documented best practice and lets future provider
    swaps reuse the same context string with a different framing
    layer.

    Args:
        sanitized_context: The user-supplied relationship context
            after passing through ``sanitize_for_ai_prompt``.

    Returns:
        The user-message body to attach to a ``HumanMessage``.
    """
    return (
        "Here is the relationship context. Convert it into 3-5 outreach "
        "talking points following the guidelines.\n\n"
        "Relationship context:\n"
        f"{sanitized_context}\n"
    )
