"""F-002 AI Note Generation API blueprint.

Single endpoint:

    POST /api/notes/generate

Behaviour summary
-----------------

* Authenticated, RBAC-gated to ``UserRole.CONTRIBUTOR`` and
  ``UserRole.ADMIN`` (Viewer / Sales Rep callers cannot generate AI
  notes for new submissions; they can only mutate outreach status on
  existing records, per the AAP s 6.2 RBAC permission matrix).

* Validates ``relationship_context`` via
  :class:`app.schemas.note_generation.NoteGenerationRequest`
  (1-4000 characters; ``extra='forbid'`` to reject any user-supplied
  ``model``/``temperature``/etc. overrides; ``str_strip_whitespace=True``
  so a payload of ``"   "`` is rejected by ``min_length=1`` after the
  whitespace strip).

* Delegates to
  :func:`app.services.ai_orchestration.generate_outreach_notes`.
  Per AAP Section 0.4.4, this module is the SOLE caller of that
  service module across the entire backend. The Anthropic SDK,
  ``langchain``, and ``langchain_anthropic`` packages are NEVER
  imported from any feature handler directly, preserving the
  provider-replaceability invariant.

* Returns
  :class:`app.schemas.note_generation.NoteGenerationResponse` JSON
  (carrying ``ai_notes``, ``model``, ``generated_at``) on the happy
  path.

* On AI provider failure (timeout, error, or misconfiguration), the
  service raises
  :class:`app.services.ai_orchestration.AIServiceUnavailableError`
  which inherits from
  :class:`app.middleware.error_handlers.AppError`; the global
  ``AppError`` handler registered in
  :func:`app.middleware.error_handlers.register_error_handlers`
  converts it to the appropriate envelope:

  * 504 with ``error.code = "ai_timeout"`` (provider did not respond
    within the 5 s P95 budget per AAP s 0.7.3),
  * 502 with ``error.code = "ai_unavailable"`` (provider returned a
    non-timeout error),
  * 503 with ``error.code = "ai_not_configured"`` (the
    ``ANTHROPIC_API_KEY`` Flask config value is empty).

  The frontend treats every AI failure as a non-blocking warning per
  AAP Section 0.4.4: the user can still submit the form (with their
  own typed notes or a blank ``ai_notes`` field). AI FAILURE NEVER
  BLOCKS FORM SUBMISSION.

This handler is intentionally THIN per AAP Section 0.5.3: parse JSON,
validate via pydantic, RBAC-gate, call service, format response. It
does not touch the database, does not open a transaction, does not
emit any audit event (AI generation is read-only from the data-tier
perspective and is NOT one of the eight audit event types in
:class:`app.models.enums.AuditEventType`), and does not emit any
prometheus metric directly (the service layer owns AI-call
telemetry via ``ai_request_duration_seconds``).

Coordination contract
---------------------

This blueprint is registered (without overriding the URL prefix) by
``app.api.__init__.register_blueprints`` at the prefix ``/api/notes``;
the route declared below at the relative path ``"/generate"`` is
therefore reachable at ``POST /api/notes/generate``.

Importing this module triggers ZERO database calls, ZERO HTTP calls
to Anthropic, ZERO network activity. All side effects are scoped to
the per-request handler invocation.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` provides the module-level logger that emits the
# ``ai_note_generation_requested`` structured event. The stdlib
# logger is routed through structlog's processor chain (configured in
# ``app.observability.logging``) so each emitted record carries the
# request-scoped ``correlation_id``, ``user_id``, ``org_id`` bound on
# contextvars by the correlation/auth middleware. This is the same
# convention used by ``app.api.health`` and ``app.middleware.rbac``.
#
# ``typing.TYPE_CHECKING`` and ``typing.Any`` support the type
# annotations on the route handler. ``TYPE_CHECKING`` keeps the
# Flask ``Response`` import out of the runtime import graph (the
# annotation is a PEP 563 string under ``from __future__ import
# annotations``), and ``Any`` widens the service-call return type so
# the defensive isinstance branch below is fully reachable per
# mypy's ``warn_unreachable`` configuration.
import logging
from typing import TYPE_CHECKING, Any

# Third-party imports.
#
# ``Blueprint`` constructs the modular ``notes_bp`` registered at
# the ``/api/notes`` URL prefix by ``app.api.__init__.register_blueprints``.
# ``g`` provides the per-request global where ``g.session.user_id``
# and ``g.session.org_id`` are read for structured-log enrichment
# after the auth middleware populates them.
# ``jsonify`` serializes the ``NoteGenerationResponse.model_dump(
# mode='json')`` output into a Content-Type: application/json HTTP
# response.
# ``request.get_json(silent=True)`` parses the inbound JSON body
# without raising ``werkzeug.BadRequest`` so a malformed payload
# yields a clean 422 with a field-scoped ``invalid_json`` error code
# rather than the default werkzeug HTML error page.
from flask import Blueprint, g, jsonify, request

# ``ValidationError`` is caught around
# ``NoteGenerationRequest.model_validate(raw_body)`` so schema/type
# violations (missing ``relationship_context``, length out-of-range
# 1-4000 chars, or ``extra='forbid'`` rejection of user-supplied
# overrides) are converted into ``ValidationFailedError`` carrying
# pydantic-shaped field-level details (``loc``/``msg``/``type``).
# This explicit catch is the canonical pattern for centralizing the
# 422 envelope shape under ``ValidationFailedError`` and preserving
# the original pydantic exception via ``raise ... from exc`` so log
# forensics can recover the underlying error type.
from pydantic import ValidationError

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration; relative imports are banned
# (see ``backend/pyproject.toml`` ``[tool.ruff.lint.flake8-tidy-imports]``).
#
# ``ValidationFailedError`` is the AppError subclass mapped to HTTP
# 422 by the registered Flask error handler with field-level error
# details preserving the AAP s 0.4.3 uniform error envelope shape.
# ``requires_role`` is the role-gating decorator that checks
# ``g.session.role`` against an allowlist with no DB round-trip,
# satisfying the sub-50ms RBAC budget per AAP s 0.7.3.
# ``UserRole`` provides the type-safe ``CONTRIBUTOR`` and ``ADMIN``
# members supplied to ``@requires_role(...)``; typo-resistant role
# values are validated at decoration (app-startup) time, NOT at
# request time.
# ``NoteGenerationRequest`` and ``NoteGenerationResponse`` are the
# pydantic 2.x request/response schemas re-exported from
# ``app.schemas.note_generation`` via ``app.schemas.__init__``.
# ``generate_outreach_notes`` is the public service-layer entry
# point that sanitizes, prompt-templates, invokes Langchain
# ``ChatAnthropic`` with a 5-second watchdog, and returns a
# ``NoteGenerationResponse`` (or raises ``AIServiceUnavailableError``
# on timeout/provider-error/misconfiguration).
# ``AIServiceUnavailableError`` is imported here to make the
# failure-mode contract explicit at the handler boundary; it is
# NEVER caught locally because the global ``AppError`` handler in
# ``app.middleware.error_handlers`` reads ``status_code`` and
# ``error_code`` off the exception instance and produces the right
# envelope shape (504 / 502 / 503) without per-handler coordination.
# This preserves the thin-handler convention from AAP s 0.5.3.
from app.middleware.error_handlers import ValidationFailedError
from app.middleware.rbac import requires_role
from app.models.enums import UserRole
from app.schemas import NoteGenerationRequest, NoteGenerationResponse
from app.services.ai_orchestration import (
    AIServiceUnavailableError,
    generate_outreach_notes,
)

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing ``Response`` in a ``TYPE_CHECKING`` guard satisfies ruff's
# strict ``flake8-type-checking`` configuration without breaking the
# return-type annotation on the route handler. ``Response`` is the
# concrete Flask response object produced by ``jsonify``; using it as
# the return-type annotation is consistent with ``app.api.health``.
if TYPE_CHECKING:
    from flask.wrappers import Response


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# The stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain; ``merge_contextvars``
# automatically attaches the request-scoped ``correlation_id``,
# ``user_id``, ``org_id``, and ``trace_id`` so the log line emitted
# below carries full request context without per-call duplication.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# The blueprint is named ``"notes"`` so handlers are referenced as
# ``url_for("notes.generate")``. No ``url_prefix`` is supplied here:
# ``app.api.__init__.register_blueprints`` registers this blueprint
# under ``/api/notes`` so the route declared below at the relative
# path ``"/generate"`` is reachable at ``POST /api/notes/generate``.
# Centralizing the URL prefix at registration time keeps the route
# table editable in one place and avoids prefix duplication if the
# blueprint is ever re-mounted under a different namespace.
#
# A reference to the imported ``AIServiceUnavailableError`` symbol is
# captured below as a module-level documentation alias. The class is
# never raised by this handler (AI failures propagate uncaught from
# the service layer to the global ``AppError`` handler in
# ``app.middleware.error_handlers``) but is imported so that the
# handler's failure-mode contract is explicit at the import layer
# and so that any future try/except hooks have access without
# requiring an additional refactor of the import block.
notes_bp = Blueprint("notes", __name__)


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Only the blueprint object is part of the module's public surface.
# The route handler ``generate`` is intentionally NOT re-exported:
# callers should reach it through HTTP requests against the
# registered route rather than invoking the view function directly.
# ``_AI_FAILURE_CLASS`` documents the AppError subclass that the
# global error handler converts into the 504 / 502 / 503 envelopes
# this endpoint can emit; it is referenced at module level so the
# import of ``AIServiceUnavailableError`` is recognized as needed at
# the runtime layer (not just for documentation), preserving the
# provider-replaceability invariant from AAP s 0.4.4 (no feature
# handler may import the Anthropic SDK directly; the only AI symbol
# allowed in the handler module is the typed exception class
# imported from ``app.services.ai_orchestration``).
__all__ = ["notes_bp"]

# Module-level alias documenting the AppError subclass that this
# endpoint may surface to callers (via the global error handler).
# The alias is referenced at runtime so the import is preserved
# under ruff's flake8-type-checking strict mode AND so the failure-
# mode contract is verifiable at import time -- mismatched aliases
# (e.g., a future rename of ``AIServiceUnavailableError`` that does
# not update this line) would surface as an ImportError at app
# startup rather than as a silent contract drift.
_AI_FAILURE_CLASS: type[AIServiceUnavailableError] = AIServiceUnavailableError


# ---------------------------------------------------------------------------
# Route: POST /api/notes/generate
# ---------------------------------------------------------------------------


@notes_bp.route("/generate", methods=["POST"])
@requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
def generate() -> tuple[Response, int]:
    """Generate AI-suggested outreach talking points (F-002).

    The handler implements the second leg of the SPA's two-call flow
    documented in AAP Section 0.4.4:

        1. (optional) ``POST /api/notes/generate`` -> this endpoint.
        2. ``POST /api/connections`` carries the user-edited
           ``ai_notes`` payload to the connections-create endpoint.

    AI failure does NOT roll back the form submit; the SPA renders
    a non-blocking "AI unavailable; you can still submit" affordance
    and the contributor either retries, types their own notes, or
    submits with an empty ``ai_notes`` field.

    Request body (JSON)::

        {"relationship_context": "We went to college together..."}

    Validation:
        * ``relationship_context`` is required, 1-4000 characters
          (post-strip).
        * No additional fields permitted: ``extra='forbid'`` on the
          pydantic schema rejects e.g., user-supplied ``model``
          overrides that would otherwise let a malicious client
          steer the Claude call.

    Success response (HTTP 200)::

        {"ai_notes": "...", "model": "claude-sonnet-4-5", "generated_at": "2026-..."}

    Failure response on malformed JSON (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "Request body must be a JSON object.",
                "correlation_id": "...",
                "fields": [
                    {
                        "loc": ["body"],
                        "msg": "Expected a JSON object containing 'relationship_context'.",
                        "type": "invalid_json",
                    }
                ],
            }
        }

    Failure response on schema violation (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "The request payload failed validation.",
                "correlation_id": "...",
                "fields": [{"loc": ["relationship_context"], "msg": "...", "type": "..."}],
            }
        }

    Failure response on AI timeout (HTTP 504, surfaced by global
    handler from ``AIServiceUnavailableError(code="ai_timeout")``)::

        {
            "error": {
                "code": "ai_timeout",
                "message": "AI provider did not respond within the configured timeout.",
                "correlation_id": "...",
                "fields": [],
            }
        }

    Failure response on AI provider error (HTTP 502, surfaced by
    global handler from ``AIServiceUnavailableError(code="ai_unavailable")``)::

        {
            "error": {
                "code": "ai_unavailable",
                "message": "AI provider returned an error.",
                "correlation_id": "...",
                "fields": [],
            }
        }

    Failure response on AI misconfiguration (HTTP 503, surfaced by
    global handler from
    ``AIServiceUnavailableError(code="ai_not_configured")``)::

        {
            "error": {
                "code": "ai_not_configured",
                "message": "Anthropic API key is not configured.",
                "correlation_id": "...",
                "fields": [],
            }
        }

    Returns:
        A 2-tuple of ``(jsonified NoteGenerationResponse, 200)`` on
        the happy path. Flask consumes the tuple as the response.
        Failure paths raise typed exceptions that are converted to
        the canonical envelope shape by the registered Flask error
        handlers; this function never returns a non-200 response
        directly.

    Raises:
        ValidationFailedError: The request body is not a JSON object,
            or the body fails ``NoteGenerationRequest`` schema
            validation. Mapped to HTTP 422 by
            :func:`app.middleware.error_handlers._handle_validation_failed_error`.
        AIServiceUnavailableError: The AI provider call timed out
            (504), errored (502), or the API key is unconfigured
            (503). Surfaced by the service layer; converted to the
            appropriate envelope by the global ``AppError`` handler.
            NEVER caught locally so the thin-handler convention from
            AAP Section 0.5.3 is preserved.
    """
    # Step 1: parse the JSON body. ``request.get_json(silent=True)``
    # returns ``None`` if the body is missing, malformed, or carries
    # the wrong content type (i.e., NOT
    # ``application/json``). This avoids letting werkzeug raise its
    # own ``BadRequest`` with an HTML error page; we surface a
    # clean 422 envelope with a field-scoped ``invalid_json`` code
    # so the SPA can render a proper error toast.
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": ("Expected a JSON object containing 'relationship_context'."),
                    "type": "invalid_json",
                }
            ],
        )

    # Step 2: validate against the pydantic schema. The schema
    # enforces:
    #   * ``relationship_context`` required
    #   * 1 <= len(relationship_context) <= 4000 (post-whitespace-strip)
    #   * extra='forbid' rejects any user-supplied overrides
    #     (model, temperature, max_tokens, etc.) that would let a
    #     malicious client steer the Claude call.
    #
    # We catch ValidationError explicitly and convert to
    # ValidationFailedError (which also produces 422) for two
    # reasons:
    #   1. Centralize the envelope shape under a single AppError
    #      subclass; the SPA's typed dispatch on
    #      ``error.code = "validation_failed"`` works uniformly
    #      whether the rejection originated from pydantic or from
    #      a service-layer business rule.
    #   2. Preserve the original pydantic exception via
    #      ``raise ... from exc`` so log forensics can recover the
    #      underlying error type and ``error.errors()`` payload.
    try:
        payload = NoteGenerationRequest.model_validate(raw_body)
    except ValidationError as exc:
        # Convert pydantic's structured errors() output to the
        # ValidationFailedError fields shape. We DROP pydantic's
        # ``input``, ``ctx``, and ``url`` keys for the same reason
        # ``app.middleware.error_handlers._serialize_pydantic_errors``
        # does: ``input`` may echo user data (PII risk); ``ctx`` may
        # leak internal regex patterns or constraint values; ``url``
        # is documentation noise the SPA does not consume.
        fields = [
            {
                "loc": [str(seg) for seg in err.get("loc", ())],
                "msg": str(err.get("msg", "Invalid value.")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        raise ValidationFailedError(
            message="The request payload failed validation.",
            fields=fields,
        ) from exc

    # Step 3: emit a structured "request received" log line. The
    # ``ai_note_generation_requested`` event name is stable so log
    # search/alerting tooling can pin to it. We log ONLY the integer
    # length of the relationship context, NEVER the raw text -- the
    # context may carry PII about people in the contributor's
    # network (per AAP s 0.7.4 security invariant: "User-supplied
    # relationship context sanitized server-side before AI prompt"
    # AND the implicit corollary that the unredacted text MUST NOT
    # land in operator-readable logs).
    #
    # ``user_id`` and ``org_id`` are sourced from ``g.session``
    # (populated by the auth middleware which runs BEFORE this
    # handler). They are stringified because UUIDs are not directly
    # JSON-serializable in every structlog renderer configuration
    # and the structured-log consumers (CloudWatch, OTLP collector)
    # expect string identifier fields.
    _logger.info(
        "ai_note_generation_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "context_chars": len(payload.relationship_context),
        },
    )

    # Step 4: delegate to the AI orchestration service. The service
    # is the SOLE importer of langchain/anthropic across the entire
    # backend (AAP s 0.4.4 provider-replaceability invariant).
    #
    # The service signature is
    # ``generate_outreach_notes(request: NoteGenerationRequest) ->
    # NoteGenerationResponse``: it accepts the validated pydantic
    # request directly (NOT decomposed args). On the happy path the
    # service returns a typed ``NoteGenerationResponse``; on
    # timeout/error/misconfiguration it raises
    # ``AIServiceUnavailableError`` carrying per-instance
    # ``status_code`` (504/502/503) and ``error_code``
    # (``ai_timeout``/``ai_unavailable``/``ai_not_configured``)
    # that the global ``AppError`` handler reads to produce the
    # canonical envelope shape. We NEVER catch
    # ``AIServiceUnavailableError`` locally; letting it propagate
    # preserves the thin-handler convention from AAP s 0.5.3 and
    # centralizes envelope wiring in
    # ``app.middleware.error_handlers``.
    #
    # The intermediate ``Any`` widens the static type so the
    # defensive isinstance branch below is fully reachable per
    # mypy's ``warn_unreachable`` configuration. Without the cast,
    # mypy would observe the declared return type of
    # ``generate_outreach_notes`` and flag the else branch as
    # unreachable code; the defensive branch only fires during a
    # future service-layer refactor (e.g., the service starts
    # returning a plain dict during a partial provider swap), at
    # which point the pydantic ``model_validate`` call recovers the
    # response shape without requiring a deploy lock-step.
    service_result: Any = generate_outreach_notes(payload)
    if isinstance(service_result, NoteGenerationResponse):
        response: NoteGenerationResponse = service_result
    else:
        response = NoteGenerationResponse.model_validate(service_result)

    # Step 5: serialize the response. ``model_dump(mode='json')``
    # converts pydantic-native types (e.g., ``AwareDatetime`` ->
    # ISO-8601 string) to JSON-serializable primitives so
    # ``jsonify`` can produce a valid JSON response without a
    # custom encoder. The 200 status is explicit (rather than
    # relying on Flask's default) so the contract-tests can pin to
    # the literal value.
    return jsonify(response.model_dump(mode="json")), 200
