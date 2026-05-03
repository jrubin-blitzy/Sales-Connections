"""F-001 Connection Record API blueprint.

Single endpoint at this checkpoint:

    POST /api/connections

Per AAP Section 0.5.2 Layer 3, F-001 (Connection Idea Form) is
delivered by a thin handler that:

* Authenticates the request (auth middleware populates
  ``g.session`` BEFORE this handler runs).
* RBAC-gates to ``UserRole.CONTRIBUTOR`` and ``UserRole.ADMIN`` --
  ``Viewer`` (Sales Rep) callers cannot create new records, per the
  AAP Section 6.2 RBAC permission matrix and Section 1.2 user
  populations (Sales Reps "consume" records, Contributors and Admins
  "submit" records).
* Validates the inbound JSON body via
  :class:`app.schemas.connection.ConnectionCreate` -- the schema
  enforces the nine business fields, length caps, LinkedIn URL
  format, ``involvement`` enum membership, ``ai_notes`` empty-to-None
  normalization, AND rejection of any client-supplied
  ``owner_user_id`` / ``owner_display_name`` / ``outreach_status``
  override (``extra='forbid'``) per AAP Section 0.7.4 security
  invariants.
* Delegates to :func:`app.services.connections.create_record`. The
  service opens its own ``with session.begin():`` transaction and
  emits the corresponding ``CREATE`` audit event in the same
  transaction (atomic state-change + audit pair per AAP Section
  0.7.1 invariant 6).
* Returns the persisted record as a
  :class:`app.schemas.connection.ConnectionRead` payload with
  HTTP 201.

Failure modes (all surfaced via the registered Flask error handlers
in :mod:`app.middleware.error_handlers`):

* HTTP 401 ``unauthorized`` -- the auth middleware rejects callers
  with no session cookie.
* HTTP 403 ``forbidden`` -- ``@requires_role`` rejects callers with
  ``Viewer`` role.
* HTTP 422 ``validation_failed`` -- the request body is not a JSON
  object, or fails the ``ConnectionCreate`` schema, or references
  unknown / cross-org tag ids.
* HTTP 409 ``duplicate_record`` -- a non-soft-deleted record with
  the same normalized LinkedIn URL already exists in the actor's
  org. Per AAP Section 0.7.6, F-010 duplicate detection is a
  warning at PRE-SUBMIT (the
  ``GET /api/connections/duplicate-check`` endpoint always returns
  HTTP 200), but the CREATE flow honours the database's unique
  partial index strictly: two contributors cannot both win the
  race to log the same LinkedIn URL into the same org. The 409
  surface lets the SPA re-fetch the now-existing record and
  surface a clear "Jane Doe just submitted this contact" message.
* HTTP 500 ``internal_error`` -- catch-all for unexpected exceptions
  (e.g., transient database errors). Production deployments alarm
  on the 5xx rate via the registered Prometheus metrics.

Why this blueprint exists as a separate module from
``app.api.notes``:

The AAP Section 0.4.3 endpoint catalog declares ``/api/connections``
as a distinct REST surface from ``/api/notes``. Per AAP Section
0.5.3 thin-handler convention, every handler has a single
responsibility and its own module so contributors can locate the
code by URL.

Why the blueprint declares only POST at this checkpoint:

Per the project's checkpoint plan, F-001 (this handler's deliverable)
ships in Layer 3. The remaining endpoints in the AAP Section 0.4.3
catalog -- ``GET /api/connections`` (F-004), ``GET
/api/connections/:id`` (F-011), ``PATCH /api/connections/:id``
(F-007), ``PATCH /api/connections/:id/status`` (F-005),
``DELETE /api/connections/:id`` (F-007 soft delete),
``GET /api/connections/duplicate-check`` (F-010), and
``GET /api/connections/:id/history`` (F-011) -- ship in Layers 4-6.
This module is structured so additional routes can be appended via
``@connections_bp.route(...)`` without re-architecting the file
layout.

Coordination contract:

This blueprint is registered (without overriding the URL prefix) by
:func:`app.api.__init__.register_blueprints` at the prefix
``/api/connections``; the route declared below at the empty relative
path ``""`` is therefore reachable at ``POST /api/connections``.

Importing this module triggers ZERO database calls, ZERO HTTP calls,
and ZERO network activity. All side effects are scoped to the
per-request handler invocation.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` provides the module-level ``_logger``; the stdlib logger
# is routed through structlog's processor chain (configured in
# ``app.observability.logging``) so each emitted record carries the
# request-scoped ``correlation_id``, ``user_id``, ``org_id`` bound on
# contextvars by the correlation/auth middleware. This convention
# matches ``app.api.notes`` and ``app.api.tags``.
#
# ``typing.TYPE_CHECKING`` keeps the Flask ``Response`` import out of
# the runtime import graph (the annotation is a PEP 563 string under
# ``from __future__ import annotations``), satisfying ruff's strict
# ``flake8-type-checking`` configuration without breaking the
# return-type annotation on the route handler.
import logging
from typing import TYPE_CHECKING

# Third-party imports.
#
# ``Blueprint`` declares the modular ``connections_bp`` registered at
# the ``/api/connections`` URL prefix by
# ``app.api.__init__.register_blueprints``.
# ``g`` provides the per-request global where ``g.session.user_id``
# and ``g.session.org_id`` are read after the auth middleware
# populates them.
# ``jsonify`` serializes Python lists/dicts into a
# ``Content-Type: application/json`` HTTP response.
# ``request.get_json(silent=True)`` parses the inbound JSON body
# without raising ``werkzeug.BadRequest`` so a malformed payload
# yields a clean 422 envelope rather than the default werkzeug HTML
# error page.
from flask import Blueprint, g, jsonify, request

# ``ValidationError`` is caught around ``ConnectionCreate.model_validate(...)``
# so schema/type violations (missing fields, length caps, LinkedIn
# format, or ``extra='forbid'`` rejection of user-supplied overrides
# like ``owner_user_id``) are converted into a
# ``ValidationFailedError`` carrying pydantic-shaped field-level
# details (``loc``/``msg``/``type``). This explicit catch centralizes
# the 422 envelope shape under ``ValidationFailedError`` and preserves
# the original pydantic exception via ``raise ... from exc`` so log
# forensics can recover the underlying error type. The pattern
# mirrors ``app.api.notes.generate`` and ``app.api.tags.create_tag``.
from pydantic import ValidationError

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration in ``backend/pyproject.toml``;
# relative imports are banned project-wide.
#
# ``ValidationFailedError`` is the AppError subclass mapped to HTTP
# 422 by the registered Flask error handler. The handler preserves
# the AAP Section 0.4.3 uniform error envelope shape.
from app.middleware.error_handlers import ValidationFailedError

# ``requires_role`` is the role-gating decorator. It checks
# ``g.session.role`` against an allowlist with no DB round-trip,
# satisfying the sub-50ms RBAC budget per AAP Section 0.7.3. Validated
# at decoration (app-startup) time so a typo crashes the build, NOT
# the request.
from app.middleware.rbac import requires_role

# ``UserRole`` is the three-role authorization enum
# (ADMIN / CONTRIBUTOR / VIEWER) used as arguments to the
# ``@requires_role(...)`` decorator. Per AAP Section 6.2 RBAC matrix,
# only Contributor and Admin can create new records.
from app.models.enums import UserRole

# ``ConnectionCreate`` validates the ``POST /api/connections`` request
# body. ``ConnectionRead`` serializes the persisted ``Record`` for
# the response payload. Both schemas live in ``app.schemas.connection``
# and are re-exported from ``app.schemas.__init__``.
from app.schemas import ConnectionCreate, ConnectionRead

# ``create_record`` is the sole writer of new ``records`` rows
# (per AAP Section 0.5.3 service-layer convention). The handler
# does NOT touch SQLAlchemy directly. ``DuplicateRecordError`` is
# imported only to document the failure-mode contract at the
# import boundary -- the handler does NOT catch this exception
# (it propagates uncaught to the global ``AppError`` handler in
# ``app.middleware.error_handlers``).
from app.services.connections import (
    DuplicateRecordError,
    create_record,
)

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing ``Response`` in a ``TYPE_CHECKING`` guard satisfies ruff's
# strict ``flake8-type-checking`` configuration without breaking the
# return-type annotation on the route handler.
if TYPE_CHECKING:
    from flask.wrappers import Response


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# The stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain; ``merge_contextvars``
# automatically attaches the request-scoped ``correlation_id``,
# ``user_id``, ``org_id``, and ``trace_id`` so log lines below carry
# full request context without per-call duplication. The
# ``add_stdlib_record_extras`` processor (introduced for QA Issue 11)
# promotes ``extra={...}`` kwargs from stdlib LogRecord into the
# emitted JSON body so the structural fields below are searchable in
# CloudWatch Insights without a custom log parser.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# The blueprint is named ``"connections"`` so handlers are referenced
# as ``url_for("connections.create_connection")``. No ``url_prefix``
# is supplied here: ``app.api.__init__.register_blueprints``
# registers this blueprint under ``/api/connections`` so the route
# declared below at the empty relative path ``""`` is reachable at
# ``POST /api/connections``. Centralizing the URL prefix at
# registration time keeps the route table editable in one place and
# avoids prefix duplication if the blueprint is ever re-mounted
# under a different namespace.
connections_bp = Blueprint("connections", __name__)


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Only the blueprint object is part of the module's public surface.
# The route handler ``create_connection`` is intentionally NOT
# re-exported: callers should reach it through HTTP requests against
# the registered route rather than invoking the view function
# directly.
__all__ = ["connections_bp"]


# Module-level alias documenting the AppError subclass that this
# endpoint may surface to callers (via the global error handler).
# The alias is referenced at runtime so the import is preserved
# under ruff's flake8-type-checking strict mode AND so the failure-
# mode contract is verifiable at import time -- mismatched aliases
# (e.g., a future rename of ``DuplicateRecordError``) would surface
# as an ImportError at app startup rather than as a silent contract
# drift.
_DUPLICATE_FAILURE_CLASS: type[DuplicateRecordError] = DuplicateRecordError


# ---------------------------------------------------------------------------
# Route: POST /api/connections
# ---------------------------------------------------------------------------


@connections_bp.route("", methods=["POST"])
@requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
def create_connection() -> tuple[Response, int]:
    """Create a Connection-Idea record (F-001).

    Request body (JSON, all nine F-001 fields)::

        {
            "full_name": "Jane Doe",
            "linkedin_url": "https://www.linkedin.com/in/janedoe",
            "company": "Acme Corp",
            "job_title": "VP of Operations",
            "relationship_context": "We went to college together,
                                     she's now VP of Ops at a Series B
                                     logistics startup.",
            "ai_notes": "<optional, AI-generated text the user may have
                         edited>",
            "involvement": "Warm Intro",
            "tag_ids": ["<uuid>", "..."]
        }

    The schema's ``model_config = _STRICT_CONFIG`` carries
    ``extra='forbid'``, rejecting any client-supplied
    ``owner_user_id``, ``owner_display_name``, ``outreach_status``,
    ``id``, ``submission_date``, or ``deleted_at`` override per AAP
    Section 0.7.4 (Security Invariants). This is the FIRST line of
    defense; the service layer also derives ``owner_user_id`` and
    ``org_id`` from ``g.session`` rather than from the payload.

    Successful response (HTTP 201)::

        {
            "id": "<uuid>",
            "full_name": "Jane Doe",
            "linkedin_url": "...",
            "normalized_linkedin_url": "...",
            "company": "Acme Corp",
            "job_title": "VP of Operations",
            "relationship_context": "...",
            "ai_notes": "...",
            "involvement": "Warm Intro",
            "outreach_status": "Not Started",
            "submission_date": "2026-04-23T10:15:30Z",
            "owner_user_id": "<g.session.user_id>",
            "owner_display_name": "<contributor's display_name>",
            "tags": [
                {"id": "<uuid>", "name": "fintech", "created_at": "..."},
                ...
            ],
            "created_at": "...",
            "updated_at": "...",
            "deleted_at": null
        }

    Failure response on malformed JSON (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "Request body must be a JSON object.",
                "correlation_id": "...",
                "fields": [
                    {"loc": ["body"], "msg": "...", "type": "invalid_json"}
                ]
            }
        }

    Failure response on schema violation (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "The request payload failed validation.",
                "correlation_id": "...",
                "fields": [
                    {"loc": ["full_name"], "msg": "...", "type": "..."},
                    ...
                ]
            }
        }

    Failure response on duplicate URL (HTTP 409)::

        {
            "error": {
                "code": "duplicate_record",
                "message": "A connection with this LinkedIn URL already
                            exists in your organization.",
                "correlation_id": "...",
                "fields": []
            }
        }

    Failure response on forbidden role (HTTP 403, surfaced by the
    ``@requires_role`` decorator + global error handler)::

        {
            "error": {
                "code": "forbidden",
                "message": "You do not have permission to perform this action.",
                "correlation_id": "...",
                "fields": []
            }
        }

    Returns:
        A 2-tuple of ``(jsonified ConnectionRead, 201)``. Flask
        consumes the tuple as the response. Failure paths raise
        typed exceptions converted to the canonical envelope shape
        by the registered Flask error handlers; this function never
        returns a non-2xx response directly.

    Raises:
        ValidationFailedError: The request body is not a JSON object,
            or the body fails ``ConnectionCreate`` schema validation,
            or one or more ``tag_ids`` reference unknown / cross-org
            tags. Mapped to HTTP 422 by
            :func:`app.middleware.error_handlers._handle_validation_failed_error`.
        DuplicateRecordError: A non-soft-deleted record with the same
            normalized LinkedIn URL already exists in the actor's
            org. Mapped to HTTP 409 with ``error.code =
            "duplicate_record"`` by the generic
            :func:`app.middleware.error_handlers._handle_app_error`.
        ForbiddenError: Raised by the ``@requires_role`` decorator
            when ``g.session.role`` is ``UserRole.VIEWER``. Mapped
            to HTTP 403.
        AuthError: Raised by the auth middleware (BEFORE this
            handler) when the request has no valid session cookie.
            Mapped to HTTP 401.
    """
    # Step 1: parse the JSON body. ``request.get_json(silent=True)``
    # returns ``None`` if the body is missing, malformed, or carries
    # the wrong content type (i.e., NOT ``application/json``). This
    # avoids letting werkzeug raise its own ``BadRequest`` with an
    # HTML error page; we surface a clean 422 envelope with a
    # field-scoped ``invalid_json`` code so the SPA can render a
    # proper error toast.
    #
    # The ``isinstance(raw_body, dict)`` guard rejects valid-JSON
    # bodies that are not objects (e.g., a JSON array ``[1, 2, 3]``,
    # a JSON string, a JSON number). Without this guard,
    # ``model_validate`` would either coerce or fail in a less
    # helpful way; the explicit guard provides a uniform error
    # message.
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": "Expected a JSON object.",
                    "type": "invalid_json",
                }
            ],
        )

    # Step 2: validate against the pydantic schema. The schema
    # enforces:
    #   * The nine F-001 fields with their respective length caps.
    #   * LinkedIn URL format (via the field validator that delegates
    #     to ``app.utils.url.is_valid_linkedin_url``).
    #   * ``involvement`` is one of the three ``InvolvementType`` enum
    #     values (case-sensitive: "Warm Intro" / "Soft Reference" /
    #     "Target Only").
    #   * ``ai_notes`` empty-string-to-None normalization (so a
    #     blank textarea does not store a meaningless empty string).
    #   * ``extra='forbid'`` rejects any client-supplied
    #     ``owner_user_id``, ``owner_display_name``,
    #     ``outreach_status``, ``id``, etc. -- the FIRST line of
    #     defense for the F-006 / F-005 / F-007 tampering cases.
    #
    # We catch ``ValidationError`` explicitly and convert to
    # ``ValidationFailedError`` (which also produces 422) for two
    # reasons:
    #   1. Centralize the envelope shape under a single AppError
    #      subclass; the SPA's typed dispatch on
    #      ``error.code = "validation_failed"`` works uniformly
    #      whether the rejection originated from pydantic or from
    #      a service-layer business rule.
    #   2. Preserve the original pydantic exception via
    #      ``raise ... from exc`` so log forensics can recover the
    #      underlying error type and ``error.errors()`` payload.
    #
    # The fields normalization mirrors
    # ``app.middleware.error_handlers._serialize_pydantic_errors``
    # and the pattern in ``app.api.notes.generate`` /
    # ``app.api.tags.create_tag``: drop pydantic's
    # ``input``/``ctx``/``url`` keys (PII / internal-detail leakage
    # risks) and keep only ``loc``/``msg``/``type``.
    try:
        payload = ConnectionCreate.model_validate(raw_body)
    except ValidationError as exc:
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
    # ``connection_create_requested`` event name is stable so log
    # search/alerting tooling can pin to it. We log only structural
    # identifiers (``user_id``, ``org_id``, ``involvement``,
    # ``tag_count``, ``has_ai_notes``) and integer character counts
    # for the free-text fields, NEVER the raw text -- the
    # relationship_context field may carry PII about people in the
    # contributor's network (per AAP Section 0.7.4 security
    # invariant).
    _logger.info(
        "connection_create_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "involvement": payload.involvement.value,
            "tag_count": len(payload.tag_ids),
            "has_ai_notes": payload.ai_notes is not None,
            "context_chars": len(payload.relationship_context),
            "ai_notes_chars": (
                len(payload.ai_notes) if payload.ai_notes is not None else 0
            ),
        },
    )

    # Step 4: delegate to the service layer. The service opens its
    # own ``with session.begin():`` transaction, performs the
    # owner/org/tag validation, INSERTs into ``records`` and
    # ``record_tags``, emits the ``CREATE`` audit event, and returns
    # the persisted ``Record`` with ``record_tags`` eager-loaded.
    #
    # We do NOT catch ``DuplicateRecordError`` or
    # ``ValidationFailedError`` from the service: letting them
    # propagate preserves the thin-handler convention from AAP
    # Section 0.5.3 and centralizes envelope wiring in
    # ``app.middleware.error_handlers``. Both exceptions are
    # AppError subclasses; the registered ``_handle_app_error``
    # handler reads ``status_code`` (409 / 422) and ``error_code``
    # (``duplicate_record`` / ``validation_failed``) off the
    # instance.
    record = create_record(payload, g.session)

    # Step 5: serialize the response. ``ConnectionRead`` mirrors the
    # AAP Section 0.4.3 endpoint catalog response shape with all
    # nine business fields plus ``id``, ``submission_date``,
    # ``owner_user_id``, ``owner_display_name``,
    # ``normalized_linkedin_url``, ``outreach_status``, ``tags``,
    # ``created_at``, ``updated_at``, ``deleted_at``. The schema's
    # ``model_config = _OUTBOUND_CONFIG`` carries
    # ``from_attributes=True`` so the ORM ``Record`` instance is
    # accepted directly via ``model_validate``.
    #
    # The ``tags`` field on ``ConnectionRead`` is ``list[TagRead]``;
    # ``Record.record_tags`` is ``list[RecordTag]``, and each
    # ``RecordTag`` carries ``.tag``. We construct the tag list
    # inline so pydantic's ``from_attributes`` can serialize each
    # ``Tag`` directly, avoiding a custom ``before_validator`` on
    # ``ConnectionRead``.
    response_payload = ConnectionRead.model_validate(
        {
            "id": record.id,
            "full_name": record.full_name,
            "linkedin_url": record.linkedin_url,
            "normalized_linkedin_url": record.normalized_linkedin_url,
            "company": record.company,
            "job_title": record.job_title,
            "relationship_context": record.relationship_context,
            "ai_notes": record.ai_notes,
            "involvement": record.involvement,
            "outreach_status": record.outreach_status,
            "submission_date": record.submission_date,
            "owner_user_id": record.owner_user_id,
            "owner_display_name": record.owner_display_name,
            "tags": [rt.tag for rt in record.record_tags],
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "deleted_at": record.deleted_at,
        }
    ).model_dump(mode="json")

    return jsonify(response_payload), 201
