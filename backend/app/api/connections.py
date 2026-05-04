"""Connection Record API blueprint (F-001 / F-004 / F-005 / F-007 / F-010 / F-011).

Endpoints registered at the ``/api/connections`` URL prefix:

* ``POST   /api/connections``                   F-001 (create record)
* ``GET    /api/connections``                   F-004 (paginated feed)
* ``GET    /api/connections/duplicate-check``   F-010 (non-blocking warning)
* ``GET    /api/connections/<uuid:record_id>``  F-011 (detail view)
* ``GET    /api/connections/<uuid:record_id>/history``  F-011 (audit history)
* ``PATCH  /api/connections/<uuid:record_id>``          F-007 (edit)
* ``PATCH  /api/connections/<uuid:record_id>/status``   F-005 (status mutation)
* ``DELETE /api/connections/<uuid:record_id>``          F-007 (soft delete)

CRITICAL ROUTE ORDERING: ``/duplicate-check`` is registered BEFORE the
``/<uuid:record_id>`` dynamic routes. Flask matches the most specific
route first, but registration order is the tiebreaker for routes that
might both match (a malformed UUID like ``duplicate-check`` would match
``<uuid:record_id>`` ONLY if the converter accepts it, but since the
``uuid`` converter rejects non-UUID strings the static route wins
regardless). Registering the static route first is defense-in-depth.

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
from datetime import date as _runtime_date
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID as _runtime_uuid  # noqa: N811 - runtime alias used as a callable in helpers

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
# error page. ``request.args`` is the immutable
# :class:`werkzeug.datastructures.MultiDict` carrying query-string
# parameters; used by the F-004 list and the F-010 duplicate-check
# handlers to extract filters, pagination, and the ``linkedin_url``
# parameter respectively.
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
# ``InvolvementType`` and ``OutreachStatus`` are the two PostgreSQL
# enums consumed by the F-004 feed query-string filter parser. They
# must be importable at runtime (NOT type-only) because the parser
# below resolves the supplied filter values against the enum members.
# ``UserRole`` gates the route decorators (already imported above
# under ``rbac``-section).
from app.models.enums import InvolvementType, OutreachStatus, UserRole

# ``ConnectionCreate`` validates ``POST /api/connections`` body.
# ``ConnectionUpdate`` validates ``PATCH /api/connections/:id`` body
# (excludes ``outreach_status`` per F-005/F-007 separation; see
# DL-0035).
# ``ConnectionStatusUpdate`` validates
# ``PATCH /api/connections/:id/status`` body (single ``outreach_status``
# field).
# ``ConnectionRead`` serializes the persisted ``Record`` for the
# response payload of POST/PATCH/DELETE/GET-detail.
# ``ConnectionHistoryEntry`` is the per-row shape for the F-011
# audit-history endpoint.
# ``PaginatedConnections`` is the pagination envelope for the F-004
# feed.
# All schemas live in ``app.schemas.connection`` and are re-exported
# from ``app.schemas.__init__``.
from app.schemas import (
    ConnectionCreate,
    ConnectionDuplicateCheckResponse,
    ConnectionHistoryEntry,
    ConnectionRead,
    ConnectionStatusUpdate,
    ConnectionUpdate,
    PaginatedConnections,
)

# Service-layer functions invoked by each handler. The handler is
# thin per AAP Section 0.5.3 convention -- it parses input, gates
# RBAC via the decorator, and delegates business logic to the
# service. ``DuplicateRecordError`` is imported only to document the
# failure-mode contract at the import boundary; the handler does
# NOT catch this exception (it propagates to the global ``AppError``
# handler in ``app.middleware.error_handlers``).
#
# * ``create_record``      -- F-001 form-driven create.
# * ``get_record``         -- F-011 detail fetch (org-scoped,
#                              soft-delete-aware).
# * ``get_record_history`` -- F-011 audit history feed.
# * ``list_records``       -- F-004 feed query (filterable, sortable,
#                              paginated). ``ConnectionFilters`` is
#                              the frozen dataclass carrying the
#                              seven filter parameters.
# * ``soft_delete_record`` -- F-007 soft delete (idempotent).
# * ``update_record``      -- F-007 edit (ownership enforced for
#                              Contributor; Admin bypass).
# * ``update_status``      -- F-005 outreach-status mutation
#                              (idempotent no-op when value
#                              unchanged).
from app.services.connections import (
    ConnectionFilters,
    DuplicateRecordError,
    create_record,
    get_record,
    get_record_history,
    list_records,
    soft_delete_record,
    update_record,
    update_status,
)

# ``check_duplicate`` is the F-010 entry point. It validates the URL
# format, normalizes it, queries the unique partial index, and
# returns the (non-blocking) :class:`ConnectionDuplicateCheckResponse`.
# Co-located in ``services.duplicate_detection`` rather than
# ``services.connections`` because it owns the F-010 normalization
# state machine and is reused from inside ``create_record`` for the
# in-transaction duplicate guard.
from app.services.duplicate_detection import check_duplicate

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing these in a ``TYPE_CHECKING`` guard satisfies ruff's strict
# ``flake8-type-checking`` configuration without breaking the
# return-type and parameter annotations on the route handlers.
#
# * ``Response``  -- Flask's concrete response object produced by
#   ``jsonify``; used as the return-type annotation.
# * ``UUID``      -- Type of the ``record_id`` parameter on every
#   path-parameter route handler; bound by Flask's ``uuid``
#   converter at routing time.
if TYPE_CHECKING:
    from uuid import UUID

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
            "tags": [{"id": "<uuid>", "name": "fintech", "created_at": "..."}, ...],
            "created_at": "...",
            "updated_at": "...",
            "deleted_at": null,
        }

    Failure response on malformed JSON (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "Request body must be a JSON object.",
                "correlation_id": "...",
                "fields": [{"loc": ["body"], "msg": "...", "type": "invalid_json"}],
            }
        }

    Failure response on schema violation (HTTP 422)::

        {
            "error": {
                "code": "validation_failed",
                "message": "The request payload failed validation.",
                "correlation_id": "...",
                "fields": [{"loc": ["full_name"], "msg": "...", "type": "..."}, ...],
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
                "fields": [],
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
            "ai_notes_chars": (len(payload.ai_notes) if payload.ai_notes is not None else 0),
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
    response_payload = _record_to_read_dict(record)

    # Per RFC 7231 Section 6.3.2 ("If the resource is created, the
    # 201 (Created) response SHOULD include a Location header field
    # that contains an identifier for the primary resource created
    # by the request"), attach the canonical detail URL of the new
    # record to the response. The path matches the registered
    # ``GET /api/connections/<uuid:record_id>`` route so subsequent
    # GET calls hit the cached resource directly.
    #
    # The header value is a relative reference (path-only). Per
    # RFC 7231, a Location header may be relative or absolute; we
    # use the relative form because it is origin-agnostic
    # (production, staging, and dev all serve the same SPA from
    # different hostnames).
    response = jsonify(response_payload)
    response.headers["Location"] = f"/api/connections/{record.id}"
    return response, 201


# ---------------------------------------------------------------------------
# Private helpers: response serialization
# ---------------------------------------------------------------------------


def _record_to_read_dict(record: Any) -> dict[str, Any]:
    """Serialize a :class:`app.models.Record` into a ConnectionRead dict.

    Centralized helper invoked by every handler that returns a single
    record (POST, GET-detail, PATCH, PATCH-status, DELETE). The
    ``ConnectionRead`` schema has ``model_config = _OUTBOUND_CONFIG``
    which carries ``from_attributes=True``, but we materialize the
    intermediate dict explicitly so the ``tags`` list (which on the
    ORM is ``record.record_tags[].tag`` rather than a flat ``tags``
    attribute) is constructed before ``model_validate`` runs.

    Why ``Any`` for the parameter type: the handler module avoids
    importing the SQLAlchemy ``Record`` class to keep the import
    graph small and to align with the project's thin-handler
    convention (handlers do not touch ORM types directly). The
    ``Any`` type is documented in the docstring rather than narrowed
    to ``Record`` so contributors maintaining the handler do not
    need to learn the ORM model surface.

    Args:
        record: A persisted ``Record`` ORM instance with eager-loaded
            ``record_tags`` -> ``tag`` chain. The service-layer
            functions ALL eager-load this chain before returning, so
            this helper does NOT trigger N+1 queries.

    Returns:
        A JSON-serializable dict matching the
        :class:`app.schemas.connection.ConnectionRead` shape, ready
        for ``jsonify``.
    """
    # NOTE: ``record.deleted_at`` is intentionally NOT supplied here.
    # Per QA Issue 8 the public :class:`ConnectionRead` shape does
    # not expose the soft-delete timestamp; non-admin callers always
    # see ``WHERE deleted_at IS NULL`` results, and admin moderation
    # consumes :class:`ConnectionAdminRead` via
    # ``GET /api/admin/records``. The Pydantic schema would silently
    # drop the key with the default ``extra='ignore'`` config, but
    # omitting it from the input dict makes the intent explicit and
    # avoids a subtle reader-trap.
    return ConnectionRead.model_validate(
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
        }
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Private helpers: query-string parsers (F-004 list, F-007 list, F-011 history)
# ---------------------------------------------------------------------------


def _parse_int_arg(name: str, default: int, *, minimum: int = 0) -> int:
    """Parse a non-negative integer query-string parameter.

    Returns the supplied default when the parameter is absent.
    Surfaces a clean :class:`ValidationFailedError` (HTTP 422) when
    the parameter is present but not a valid integer or is below
    the supplied minimum. Centralized so the error envelope shape
    is uniform across the limit/offset/page parameters used by the
    F-004 feed and F-011 history handlers.

    Args:
        name: Query-string parameter name (e.g., ``"limit"``).
        default: Default integer returned when the parameter is
            absent.
        minimum: Lower bound for valid values. The service layer
            applies its own clamps (e.g., ``_MAX_PAGE_SIZE = 100``)
            so we do NOT enforce an upper bound here.

    Returns:
        The parsed integer (or ``default`` when the parameter is
        absent).

    Raises:
        ValidationFailedError: The parameter was present but not a
            valid non-negative integer.
    """
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(
            message=f"Query parameter '{name}' must be an integer.",
            fields=[
                {
                    "loc": ["query", name],
                    "msg": "Expected an integer value.",
                    "type": "value_error.integer",
                },
            ],
        ) from exc
    if value < minimum:
        raise ValidationFailedError(
            message=f"Query parameter '{name}' must be >= {minimum}.",
            fields=[
                {
                    "loc": ["query", name],
                    "msg": f"Value must be >= {minimum}.",
                    "type": "value_error.min",
                },
            ],
        )
    return value


def _parse_enum_args(
    name: str,
    enum_cls: type[InvolvementType] | type[OutreachStatus],
) -> tuple[Any, ...]:
    """Parse a multi-valued enum query-string parameter into a tuple.

    Supports BOTH the comma-separated single-key form
    (``?involvement=Warm Intro,Soft Reference``) AND the repeated-key
    form (``?involvement=Warm Intro&involvement=Soft Reference``)
    so the SPA can use whichever encoding TanStack Query produces by
    default.

    The supplied :class:`InvolvementType` and :class:`OutreachStatus`
    enums are str-subclasses, so direct construction
    (``InvolvementType(value)``) succeeds when the value matches an
    enum member's ``value`` attribute. Unrecognized values surface
    as HTTP 422.

    Args:
        name: Query-string parameter name (e.g., ``"involvement"``,
            ``"outreach_status"``).
        enum_cls: The enum class to coerce values into.

    Returns:
        A tuple of enum members (possibly empty) in the order
        supplied by the client.

    Raises:
        ValidationFailedError: One or more supplied values are not
            valid members of the enum.
    """
    raw_values = request.args.getlist(name)
    if not raw_values:
        return ()
    # Split each value on comma so the client can use either
    # form. Strip whitespace defensively.
    flat: list[str] = []
    for item in raw_values:
        for part in item.split(","):
            stripped = part.strip()
            if stripped:
                flat.append(stripped)
    if not flat:
        return ()
    coerced: list[Any] = []
    invalid_fields: list[dict[str, Any]] = []
    for value in flat:
        try:
            coerced.append(enum_cls(value))
        except ValueError:
            invalid_fields.append(
                {
                    "loc": ["query", name],
                    "msg": (
                        f"'{value}' is not a valid {enum_cls.__name__} value. "
                        f"Allowed: {sorted(member.value for member in enum_cls)}"
                    ),
                    "type": "value_error.enum",
                }
            )
    if invalid_fields:
        raise ValidationFailedError(
            message=f"Invalid value(s) for query parameter '{name}'.",
            fields=invalid_fields,
        )
    return tuple(coerced)


def _parse_uuid_args(name: str, *aliases: str) -> tuple[Any, ...]:
    """Parse a multi-valued UUID query-string parameter into a tuple.

    Used by the F-004 feed handler to parse ``?owner_user_ids=...``
    and ``?tag_ids=...``. Supports both comma-separated single-key
    form and repeated-key form, mirroring :func:`_parse_enum_args`.

    Per QA Issue 6 the function accepts optional aliases so the
    canonical plural names (``owner_user_ids``, ``tag_ids``) can
    coexist with the singular forms (``owner_user_id``, ``tag_id``)
    that some SPA tooling and external integrations emit. All
    matching keys are concatenated; the aggregate of values is
    deduplicated by virtue of UUID equality at the ORM filter
    layer.

    Args:
        name: The canonical parameter name (e.g.,
            ``"owner_user_ids"``).
        *aliases: Zero or more legacy / alternate parameter names
            (e.g., ``"owner_user_id"``) whose values are appended
            after the canonical values.

    Returns:
        Tuple of :class:`uuid.UUID` instances.

    Raises:
        ValidationFailedError: One or more supplied values cannot
            be parsed as a UUID.
    """
    raw_values = request.args.getlist(name)
    for alias in aliases:
        raw_values = raw_values + request.args.getlist(alias)
    if not raw_values:
        return ()
    flat: list[str] = []
    for item in raw_values:
        for part in item.split(","):
            stripped = part.strip()
            if stripped:
                flat.append(stripped)
    if not flat:
        return ()
    coerced: list[Any] = []
    invalid_fields: list[dict[str, Any]] = []
    for value in flat:
        try:
            coerced.append(_runtime_uuid(value))
        except (TypeError, ValueError):
            invalid_fields.append(
                {
                    "loc": ["query", name],
                    "msg": f"'{value}' is not a valid UUID.",
                    "type": "value_error.uuid",
                }
            )
    if invalid_fields:
        raise ValidationFailedError(
            message=f"Invalid UUID(s) for query parameter '{name}'.",
            fields=invalid_fields,
        )
    return tuple(coerced)


def _parse_date_arg(name: str) -> Any:
    """Parse an ISO-8601 date query-string parameter (``YYYY-MM-DD``).

    Returns ``None`` when the parameter is absent. Surfaces 422 when
    present but not a valid ISO date. The service-layer
    :class:`ConnectionFilters` accepts ``date`` objects for the
    submission_date_from/to filter; this helper produces them.

    Returns:
        :class:`datetime.date` or ``None``.

    Raises:
        ValidationFailedError: The supplied value is not a valid
            ISO-8601 date.
    """
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        return _runtime_date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(
            message=f"Query parameter '{name}' must be an ISO-8601 date.",
            fields=[
                {
                    "loc": ["query", name],
                    "msg": "Expected a YYYY-MM-DD date.",
                    "type": "value_error.date",
                },
            ],
        ) from exc


def _parse_bool_arg(name: str, default: bool = False) -> bool:
    """Parse a boolean query-string parameter.

    Accepts (case-insensitive) ``true``/``false``, ``1``/``0``,
    ``yes``/``no``. Returns the supplied default when the parameter
    is absent.

    Args:
        name: Query-string parameter name.
        default: Default returned when the parameter is absent.

    Returns:
        The parsed boolean (or ``default`` when absent).
    """
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise ValidationFailedError(
        message=f"Query parameter '{name}' must be a boolean.",
        fields=[
            {
                "loc": ["query", name],
                "msg": "Expected one of: true, false, 1, 0, yes, no.",
                "type": "value_error.bool",
            },
        ],
    )


def _build_filters_from_query(actor_role: Any) -> ConnectionFilters:
    """Construct a :class:`ConnectionFilters` from the request query string.

    Parses every filter dimension supported by F-004 plus the
    admin-only ``include_deleted`` opt-out.

    The ``include_deleted`` flag is admit-listed to ``Admin`` only.
    Non-Admin callers requesting ``include_deleted=true`` have the
    flag silently coerced to ``False``; the request is NOT rejected
    (a 403 here would leak information about the moderation surface
    to non-admins).

    Args:
        actor_role: ``g.session.role`` of the caller. Used to gate
            ``include_deleted=True``.

    Returns:
        Frozen :class:`ConnectionFilters` ready to pass to
        :func:`list_records`.

    Raises:
        ValidationFailedError: Any single filter parameter fails its
            type/format check.
    """
    company = request.args.get("company")
    if company is not None and company.strip() == "":
        company = None

    full_name_search = request.args.get("full_name_search")
    if full_name_search is not None and full_name_search.strip() == "":
        full_name_search = None

    involvement = _parse_enum_args("involvement", InvolvementType)
    outreach_status = _parse_enum_args("outreach_status", OutreachStatus)
    # Per QA Issue 6: accept singular ``owner_user_id`` as an alias
    # for the canonical plural ``owner_user_ids`` so external SPA
    # tooling that emits the singular form (matching the older
    # spec text) is not silently ignored. Same convenience for
    # ``tag_id`` -> ``tag_ids``.
    owner_user_ids = _parse_uuid_args("owner_user_ids", "owner_user_id")
    tag_ids = _parse_uuid_args("tag_ids", "tag_id")
    submission_date_from = _parse_date_arg("submission_date_from")
    submission_date_to = _parse_date_arg("submission_date_to")

    requested_include_deleted = _parse_bool_arg("include_deleted", default=False)
    # Admin-only opt-out (per AAP §0.7.1 invariant 4 + DL-0005).
    # Non-Admins silently get ``False`` so the moderation flag is
    # not exposed.
    include_deleted = requested_include_deleted and actor_role == UserRole.ADMIN

    return ConnectionFilters(
        company=company,
        involvement=involvement,
        outreach_status=outreach_status,
        owner_user_ids=owner_user_ids,
        tag_ids=tag_ids,
        submission_date_from=submission_date_from,
        submission_date_to=submission_date_to,
        full_name_search=full_name_search,
        include_deleted=include_deleted,
    )


# ---------------------------------------------------------------------------
# Route: GET /api/connections/duplicate-check (F-010)
# ---------------------------------------------------------------------------
#
# CRITICAL: This route MUST be registered BEFORE the
# ``GET /<uuid:record_id>`` route. Flask matches the most specific
# pattern first, but for static-vs-dynamic ties the registration
# order is the tiebreaker. Although the ``uuid`` converter rejects
# the literal string "duplicate-check" (so the static route would
# win on URL-converter grounds alone), declaring the static route
# first preserves correctness even if the path ever changes to a
# format the converter would accept (e.g., a hex string with
# hyphens). Per QA Issue #7 of Checkpoint 2, this ordering is
# explicitly verified.


@connections_bp.route("/duplicate-check", methods=["GET"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)
def duplicate_check() -> tuple[Response, int]:
    """Check whether a LinkedIn URL already exists in the actor's org (F-010).

    NON-BLOCKING: this endpoint always returns HTTP 200 (or HTTP 422
    for malformed input). A duplicate match does NOT produce a 409;
    duplicate detection is a warning per AAP §0.7.6 and DL-0016.

    Query parameters:
        linkedin_url   Required. The raw URL to check. Server
                       normalizes it via
                       :func:`app.utils.url.normalize_linkedin_url`
                       before the lookup.
        exclude_record_id  Optional. UUID of the record being edited;
                           prevents the record from self-reporting as
                           a duplicate of itself.

    Returns:
        HTTP 200 with :class:`ConnectionDuplicateCheckResponse`
        payload regardless of whether a duplicate was found. The
        ``duplicate_found`` boolean is the SPA's branching signal.

    Failure modes:
        HTTP 401 -- auth middleware rejected unauthenticated request.
        HTTP 403 -- ``@requires_role`` rejected (Viewer / Sales Rep
                    role; per AAP Section 0.7.6 the duplicate-check
                    is a pre-submit warning for the connection-creation
                    flow, and Viewers do not submit records, so the
                    feature semantically belongs only to Contributor
                    and Admin).
        HTTP 422 -- ``linkedin_url`` missing, malformed, or
                    unnormalizable; ``exclude_record_id`` not a UUID.

    RBAC scope rationale (CR-CKPT5-MINOR#2):
        F-010 duplicate detection is a *pre-submit* warning surfaced
        as the contributor types a LinkedIn URL into the
        AddEditConnectionForm (POST /api/connections, F-001). Sales
        Reps (Viewer role) do not submit new records: per AAP
        Section 0.5.4 / Section 1.2 their primary surface is the
        Connection Feed and Connection Detail views (read-only
        consumption + outreach status mutation). The duplicate-check
        endpoint therefore belongs only to roles that author records:
        Contributor and Admin. Viewers can still discover existing
        records via the standard list and detail endpoints
        (``GET /api/connections`` / ``GET /api/connections/:id``);
        rejecting them from /duplicate-check is a scope decision,
        not a security measure.

    Raises:
        ValidationFailedError: missing ``linkedin_url`` or invalid
            ``exclude_record_id`` UUID. The service layer raises its
            own :class:`ValidationFailedError` for malformed-URL
            cases.
    """
    linkedin_url = request.args.get("linkedin_url")
    if linkedin_url is None or linkedin_url == "":
        raise ValidationFailedError(
            message="Query parameter 'linkedin_url' is required.",
            fields=[
                {
                    "loc": ["query", "linkedin_url"],
                    "msg": "linkedin_url is required.",
                    "type": "value_error.missing",
                },
            ],
        )

    exclude_raw = request.args.get("exclude_record_id")
    exclude_uuid: Any = None
    if exclude_raw is not None and exclude_raw != "":
        try:
            exclude_uuid = _runtime_uuid(exclude_raw)
        except (TypeError, ValueError) as exc:
            raise ValidationFailedError(
                message="Query parameter 'exclude_record_id' must be a UUID.",
                fields=[
                    {
                        "loc": ["query", "exclude_record_id"],
                        "msg": "Expected a valid UUID.",
                        "type": "value_error.uuid",
                    },
                ],
            ) from exc

    response = check_duplicate(
        linkedin_url=linkedin_url,
        actor=g.session,
        exclude_record_id=exclude_uuid,
    )
    # Verify the response shape at runtime against the canonical
    # ``ConnectionDuplicateCheckResponse`` schema. This pins the
    # service-layer return contract at the API boundary so any
    # future return-type drift in ``check_duplicate`` surfaces as a
    # ``TypeError`` at request time rather than as a silent envelope
    # drift in production. The check is sub-microsecond cost.
    if not isinstance(response, ConnectionDuplicateCheckResponse):
        raise TypeError(
            "check_duplicate did not return a ConnectionDuplicateCheckResponse "
            f"(got {type(response).__name__})"
        )

    _logger.info(
        "duplicate_check_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "duplicate_found": response.duplicate_found,
            "has_exclude_record_id": exclude_uuid is not None,
        },
    )

    return jsonify(response.model_dump(mode="json")), 200


# ---------------------------------------------------------------------------
# Route: GET /api/connections (F-004)
# ---------------------------------------------------------------------------


@connections_bp.route("", methods=["GET"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER)
def list_connections() -> tuple[Response, int]:
    """Paginated, filterable, sortable list of records (F-004).

    Query parameters:
        Filters:
            company                Substring match on Record.company.
            full_name_search       Substring match on Record.full_name.
            involvement            Comma-or-repeated InvolvementType.
            outreach_status        Comma-or-repeated OutreachStatus.
            owner_user_ids         Comma-or-repeated UUIDs.
            tag_ids                Comma-or-repeated UUIDs (any-of).
            submission_date_from   YYYY-MM-DD inclusive lower bound.
            submission_date_to     YYYY-MM-DD inclusive upper bound.
            include_deleted        bool. Admin-only opt-out of the
                                   default soft-delete filter; non-
                                   Admins silently get False.
        Sort:
            sort                   One of submission_date / full_name
                                   / company / owner_display_name /
                                   outreach_status. Default
                                   "submission_date".
            sort_dir               "asc" or "desc". Default "desc".
        Pagination:
            limit                  1-100 (silently clamped). Default
                                   25.
            offset                 >= 0. Default 0.

    Returns:
        HTTP 200 with :class:`PaginatedConnections` payload:

            {"items": [...], "total": <int>, "limit": <int>,
             "offset": <int>}

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 422 -- any single query parameter fails its
                    type/format check or sort key is unknown.
    """
    actor_role = getattr(g.session, "role", None)
    filters = _build_filters_from_query(actor_role=actor_role)
    sort_key = request.args.get("sort", "submission_date")
    sort_dir = request.args.get("sort_dir", "desc")
    limit = _parse_int_arg("limit", default=25, minimum=1)
    offset = _parse_int_arg("offset", default=0, minimum=0)

    rows, total = list_records(
        filters,
        g.session,
        sort_key=sort_key,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )

    items = [_record_to_read_dict(record) for record in rows]

    response_payload = PaginatedConnections.model_validate(
        {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    ).model_dump(mode="json")

    _logger.info(
        "connections_list_returned",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "returned_count": len(items),
            "total_count": total,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )

    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: GET /api/connections/<uuid:record_id> (F-011)
# ---------------------------------------------------------------------------


@connections_bp.route("/<uuid:record_id>", methods=["GET"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER)
def get_connection(record_id: UUID) -> tuple[Response, int]:
    """Fetch a single record's full detail (F-011).

    Org-scoped, soft-delete-aware. Cross-org or unknown-id requests
    return 404 (not 403) per AAP §0.7.4 (info-disclosure defense).

    Admin callers MAY request soft-deleted records via
    ``?include_deleted=true``; non-Admin callers asking for the
    flag are silently downgraded.

    Args:
        record_id: UUID extracted from the URL path; Flask's
            ``uuid`` converter performs the type coercion.

    Returns:
        HTTP 200 with :class:`ConnectionRead` payload.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 404 -- record not found in actor's org (or soft-deleted
                    and caller did not opt in via include_deleted).
    """
    actor_role = getattr(g.session, "role", None)
    requested_include_deleted = _parse_bool_arg("include_deleted", default=False)
    include_deleted = requested_include_deleted and actor_role == UserRole.ADMIN

    record = get_record(
        record_id,
        g.session,
        include_deleted=include_deleted,
    )

    response_payload = _record_to_read_dict(record)

    _logger.info(
        "connection_detail_returned",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
            "include_deleted": include_deleted,
        },
    )

    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: GET /api/connections/<uuid:record_id>/history (F-011)
# ---------------------------------------------------------------------------


@connections_bp.route("/<uuid:record_id>/history", methods=["GET"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER)
def get_connection_history(record_id: UUID) -> tuple[Response, int]:
    """Return paginated audit-event history for a record (F-011).

    The org-scope check is performed BEFORE the audit query so a
    cross-org caller receives 404, not a 200 with empty events
    (which would leak the existence of records in other orgs).

    Query parameters:
        limit            1-100 (clamped). Default 25.
        offset           >= 0. Default 0.
        include_deleted  When ``true``, allows Admin to fetch history
                         for a soft-deleted record (the F-011 admin
                         moderation flow). Silently coerced to
                         ``false`` for non-Admin actors per DL-0037.

    Args:
        record_id: UUID extracted from the URL path.

    Returns:
        HTTP 200 with envelope::

            {"items": [ConnectionHistoryEntry...],
             "total": <int>, "limit": <int>, "offset": <int>}

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 404 -- record not found in actor's org (or soft-deleted
                    when include_deleted not granted).
    """
    limit = _parse_int_arg("limit", default=25, minimum=1)
    offset = _parse_int_arg("offset", default=0, minimum=0)
    include_deleted_requested = _parse_bool_arg("include_deleted", default=False)
    # Per DL-0037: silently coerce to False for non-Admin (info-disclosure
    # defense). Admins opt in for the moderation-of-deleted-records flow.
    include_deleted = include_deleted_requested and g.session.role == UserRole.ADMIN

    events, total = get_record_history(
        record_id,
        g.session,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )

    items = []
    for event in events:
        actor_display = event.actor.display_name if event.actor is not None else None
        item = ConnectionHistoryEntry.model_validate(
            {
                "id": event.id,
                "event_type": event.event_type,
                "event_timestamp": event.event_timestamp,
                "actor_user_id": event.actor_user_id,
                "actor_display_name": actor_display,
                "before_payload": event.before_payload,
                "after_payload": event.after_payload,
            }
        ).model_dump(mode="json")
        items.append(item)

    response_payload = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }

    _logger.info(
        "connection_history_returned",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
            "returned_count": len(items),
            "total_count": total,
        },
    )

    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: PATCH /api/connections/<uuid:record_id> (F-007 edit)
# ---------------------------------------------------------------------------


@connections_bp.route("/<uuid:record_id>", methods=["PATCH"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)
def update_connection(record_id: UUID) -> tuple[Response, int]:
    """Edit a record (F-007).

    RBAC at the route layer admits ``Admin`` and ``Contributor``;
    ``Viewer`` is rejected with HTTP 403. The service layer
    additionally enforces ownership for Contributors (Contributor
    can edit only records they own; Admin bypasses).

    Per the project-wide PATCH semantics:
        * Fields absent from the body are NOT modified.
        * ``tag_ids`` absent => tags untouched. Empty list => all
          tags removed. Non-empty => replace.
        * ``outreach_status`` is INTENTIONALLY excluded from the
          edit schema (mutation flows through the dedicated
          status endpoint, RBAC-gated to Sales Rep + Admin only).
        * ``owner_user_id`` and ``owner_display_name`` are rejected
          by ``extra='forbid'`` (owner attribution is permanent
          per AAP §0.7.4 and DL-0012).

    Args:
        record_id: UUID extracted from the URL path.

    Returns:
        HTTP 200 with the updated :class:`ConnectionRead`.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- Viewer role; or Contributor editing another
                    user's record.
        HTTP 404 -- record not found in actor's org.
        HTTP 409 -- new normalized LinkedIn URL collides with another
                    active record in the same org.
        HTTP 422 -- payload schema violation (LinkedIn URL format,
                    length caps, ``extra='forbid'`` rejection,
                    cross-org tag id, etc.).
    """
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": "Expected a JSON object.",
                    "type": "invalid_json",
                },
            ],
        )

    try:
        payload = ConnectionUpdate.model_validate(raw_body)
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

    # Defense-in-depth log line. The structlog redactor filters
    # secret-named keys before emission; the relationship_context
    # field is intentionally NOT logged (it carries PII per AAP
    # §0.7.4).
    _logger.info(
        "connection_update_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
            "field_count": len(payload.model_dump(exclude_unset=True)),
        },
    )

    record = update_record(record_id, payload, g.session)

    response_payload = _record_to_read_dict(record)
    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: PATCH /api/connections/<uuid:record_id>/status (F-005)
# ---------------------------------------------------------------------------


@connections_bp.route("/<uuid:record_id>/status", methods=["PATCH"])
@requires_role(UserRole.ADMIN, UserRole.VIEWER)
def update_connection_status(record_id: UUID) -> tuple[Response, int]:
    """Mutate the outreach status of a record (F-005).

    RBAC: ``Admin`` and ``Viewer`` (Sales Rep) only. ``Contributor``
    is rejected with HTTP 403, even if the contributor is the
    record's owner. This separation is the entire point of the
    Viewer/Contributor distinction per AAP §0.1.2 ("preserve sales
    team accountability") and DL-0019.

    Idempotent: setting the status to its current value is a no-op
    that returns 200 without emitting an audit event.

    Body:
        {"outreach_status": "Not Started" | "In Progress" |
                            "Contacted"   | "Closed"}

    Args:
        record_id: UUID extracted from the URL path.

    Returns:
        HTTP 200 with the (possibly unchanged) record's
        :class:`ConnectionRead`.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- Contributor role.
        HTTP 404 -- record not found in actor's org.
        HTTP 422 -- body shape violation (unknown enum value, extra
                    fields, etc.).
    """
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": "Expected a JSON object.",
                    "type": "invalid_json",
                },
            ],
        )

    try:
        payload = ConnectionStatusUpdate.model_validate(raw_body)
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

    _logger.info(
        "connection_status_update_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
            "new_status": payload.outreach_status.value,
        },
    )

    record = update_status(record_id, payload, g.session)

    response_payload = _record_to_read_dict(record)
    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: DELETE /api/connections/<uuid:record_id> (F-007 soft delete)
# ---------------------------------------------------------------------------


@connections_bp.route("/<uuid:record_id>", methods=["DELETE"])
@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER)
def soft_delete_connection(record_id: UUID) -> tuple[Response, int]:
    """Soft-delete a record (F-007).

    Sets ``deleted_at = NOW()`` and emits a single ``soft_delete``
    audit event in the same transaction. Hard delete is a SEPARATE
    Admin-only path that ships with F-014 (Admin Panel) -- this
    route NEVER hard-deletes, regardless of the caller's role.

    RBAC at the route layer admits all three roles; the service
    layer enforces ownership (Contributor and Viewer can soft-delete
    only records they own; Admin bypasses).

    Idempotency: a second soft-delete on the same record is a no-op.
    The endpoint returns the record's :class:`ConnectionRead` with
    ``deleted_at`` populated regardless of whether the deletion was
    fresh or idempotent. The audit-event count is incremented EXACTLY
    ONCE per record (the idempotent path emits no new event).

    Args:
        record_id: UUID extracted from the URL path.

    Returns:
        HTTP 200 with the soft-deleted :class:`ConnectionRead`.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- non-owner non-Admin attempting to delete.
        HTTP 404 -- record not found in actor's org.
    """
    _logger.info(
        "connection_soft_delete_requested",
        extra={
            "user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
        },
    )

    record = soft_delete_record(record_id, g.session)

    response_payload = _record_to_read_dict(record)
    return jsonify(response_payload), 200
