"""F-008 Tagging API blueprint.

Two endpoints serve tag classification:

- ``GET /api/tags`` -- List all tags in the actor's organization. Used
  by the Connection Feed filter UI and by the Add/Edit Connection
  Form's autocomplete control. Open to any authenticated user (Viewer
  can read tags to filter, Contributor and Admin to filter and to
  attach to records they create or edit).

- ``POST /api/tags`` -- Create a tag in the actor's organization.
  Idempotent on ``(org_id, name)``: if a tag with the same name
  already exists, the existing tag is returned with HTTP 200; a
  newly-created tag is returned with HTTP 201. RBAC-gated to
  Contributor and Admin to mirror the connection-create permission
  matrix from AAP Section 6.2 (Viewer is rejected with 403).

Per AAP Section 0.4.3 endpoint catalog::

    | GET  | /api/tags | F-008 | backend/app/api/tags.py |
    | POST | /api/tags | F-008 | backend/app/api/tags.py |

Why this blueprint uses direct SQLAlchemy queries (no service layer)
--------------------------------------------------------------------

The AAP Section 0.6.1 in-scope service modules are: ``audit.py``,
``auth.py``, ``connections.py``, ``ai_orchestration.py``,
``duplicate_detection.py``, ``admin.py`` -- there is NO ``tags.py``
service module. Tag operations are simple list/insert queries that:

* Do NOT require complex business invariants (the
  ``(org_id, name)`` unique constraint is enforced at the database
  layer by the ``uq_tags_org_name`` ``UniqueConstraint`` declared on
  :class:`app.models.tag.Tag`).
* Do NOT require audit emission. The eight
  :class:`app.models.enums.AuditEventType` values
  (``create``, ``status_change``, ``edit``, ``soft_delete``,
  ``hard_delete``, ``role_change``, ``authentication``,
  ``admin_op``) are scoped to records, users, and authentication --
  there is no ``tag_*`` event type. Tags are subordinate metadata;
  the records that REFERENCE tags ARE audited via the
  ``EDIT``/``CREATE`` events on those records.
* Do NOT open multi-statement transactions. Each tag operation is a
  single statement (one ``SELECT`` for list, one ``SELECT`` + one
  ``INSERT`` for the idempotent create).

Per AAP Section 0.5.3 thin-handler convention, "business logic lives
in services" -- but trivial CRUD without business invariants is not
"business logic". Per the absence of a tags service in
Section 0.6.1, this blueprint uses direct SQLAlchemy queries via
``db.session()``. If future requirements add complexity (audit
emission, soft delete on tags, tag merge, tag aliasing), this logic
should be extracted into ``app/services/tags.py`` at that time.

Idempotency strategy
--------------------

The ``POST /api/tags`` endpoint is idempotent on ``(org_id, name)``
via a SELECT-then-INSERT pattern with an ``IntegrityError`` race
fallback:

1. SELECT the tag by ``(org_id, name)``. If found, return HTTP 200
   with the existing tag's id (the common-case fast path).
2. If not found, INSERT the new tag and COMMIT. If the COMMIT
   raises ``IntegrityError`` (a concurrent request created the same
   tag between our SELECT and our INSERT), re-fetch the winning row
   and return HTTP 200 with the winning tag's id (the race-condition
   fallback).
3. If the SELECT-INSERT-SELECT sequence still fails to find a row,
   surface a ``ConflictError`` (HTTP 409) so the SPA can present a
   clear error -- this branch is reached only if the integrity
   violation is something other than the partial unique index (e.g.,
   a future foreign-key constraint we have not anticipated).

This is preferable to a blind ``INSERT ... ON CONFLICT DO NOTHING``
upsert because (a) the common case is a fast read with no write
contention, and (b) the response semantics are clear: HTTP 201
distinguishes a newly-created tag from HTTP 200 returning an
existing tag.

Coordination contract
---------------------

This blueprint is registered (without overriding the URL prefix) by
``app.api.__init__.register_blueprints`` at the prefix ``/api/tags``;
the routes declared below at the empty relative path ``""`` are
therefore reachable at ``GET /api/tags`` and ``POST /api/tags``.

Importing this module triggers ZERO database calls, ZERO HTTP calls,
ZERO network activity. All side effects are scoped to the per-request
handler invocation.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` provides the module-level ``_logger``; the stdlib logger
# is routed through structlog's processor chain (configured in
# ``app.observability.logging``) so each emitted record carries the
# request-scoped ``correlation_id``, ``user_id``, ``org_id`` bound on
# contextvars by the correlation/auth middleware. This convention
# matches ``app.api.notes`` and ``app.api.health``.
#
# ``typing.TYPE_CHECKING`` keeps the Flask ``Response`` import out of
# the runtime import graph (the annotation is a PEP 563 string under
# ``from __future__ import annotations``), satisfying ruff's strict
# ``flake8-type-checking`` configuration without breaking the
# return-type annotation on the route handlers.
import logging
from typing import TYPE_CHECKING

# Third-party imports.
#
# ``Blueprint`` declares the modular ``tags_bp`` registered at the
# ``/api/tags`` URL prefix by ``app.api.__init__.register_blueprints``.
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

# ``ValidationError`` is caught around ``TagCreate.model_validate(...)``
# so schema/type violations (missing ``name``, ``name`` length
# out-of-range, or ``extra='forbid'`` rejection of user-supplied
# overrides like ``id`` or ``org_id``) are converted into a
# ``ValidationFailedError`` carrying pydantic-shaped field-level
# details (``loc``/``msg``/``type``). This explicit catch centralizes
# the 422 envelope shape under ``ValidationFailedError`` and preserves
# the original pydantic exception via ``raise ... from exc`` so log
# forensics can recover the underlying error type. The pattern
# mirrors ``app.api.notes.generate``.
from pydantic import ValidationError

# SQLAlchemy 2.x ORM/Core primitives used for tag queries.
#
# * ``select()`` builds the SELECT statement for listing tags and for
#   the idempotent existence check.
# * ``and_()`` composes the two-predicate WHERE clause
#   (``org_id == ... AND name == ...``) for the idempotent existence
#   check.
# * ``asc()`` sorts the ``list_tags`` response alphabetically by
#   ``name`` so the autocomplete UX is deterministic.
# * ``IntegrityError`` (from ``sqlalchemy.exc``) is caught after
#   ``session.commit()`` to detect the partial-unique-index race
#   condition during concurrent tag creation so the handler can
#   re-fetch the winning row and return idempotent 200 semantics.
from sqlalchemy import and_, asc, select
from sqlalchemy.exc import IntegrityError

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration in ``backend/pyproject.toml``;
# relative imports are banned project-wide.
#
# ``db`` is the SQLAlchemy 2.x wrapper singleton from
# ``app.extensions``. ``db.session()`` returns a fresh session bound
# to the configured engine; the session is closed on context-manager
# exit. We call ``session.commit()`` explicitly because the operation
# is single-statement; we do NOT use ``with session.begin():``
# because there is no audit pair to atomically commit (per AAP
# Section 0.5.3, the ``with session.begin():`` pattern is reserved
# for atomic state-change-plus-audit transactions).
from app.extensions import db

# ``ConflictError`` and ``ValidationFailedError`` are AppError
# subclasses mapped to HTTP 409 and 422 respectively by the registered
# Flask error handlers. The handlers preserve the
# AAP Section 0.4.3 uniform error envelope shape.
#
# * ``ValidationFailedError`` is raised for: malformed request body
#   (non-dict JSON), pydantic schema violations, post-trim empty
#   ``name`` (defensive belt-and-suspenders -- the schema's
#   ``str_strip_whitespace=True`` already covers this).
# * ``ConflictError`` is raised for unexpected ``IntegrityError``
#   that does not resolve to the duplicate-row race (i.e., a
#   constraint violation we have not anticipated). The expected
#   duplicate-row race is handled by re-fetching the winning row and
#   returning HTTP 200, NOT a 409.
from app.middleware.error_handlers import ConflictError, ValidationFailedError

# ``requires_role`` is the role-gating decorator. It checks
# ``g.session.role`` against an allowlist with no DB round-trip,
# satisfying the sub-50ms RBAC budget per AAP Section 0.7.3. Validated
# at decoration (app-startup) time so a typo crashes the build, NOT
# the request.
from app.middleware.rbac import requires_role

# ``Tag`` is the SQLAlchemy ORM model for the ``tags`` table,
# re-exported from ``app.models.__init__`` (which itself imports it
# from ``app.models.tag``). Used here to construct ``select(Tag)``
# statements and to instantiate new ``Tag(...)`` rows.
from app.models import Tag

# ``UserRole`` is the three-role authorization enum
# (ADMIN / CONTRIBUTOR / VIEWER) used as arguments to the
# ``@requires_role(...)`` decorator on ``create_tag``. Listing
# (``GET /api/tags``) is open to all authenticated users; only
# creation requires Contributor or Admin.
from app.models.enums import UserRole

# ``TagCreate`` validates the ``POST /api/tags`` request body
# (``name`` field with 1-64 char length, ``extra='forbid'``).
# ``TagRead`` serializes ``Tag`` ORM rows for both ``GET /api/tags``
# and ``POST /api/tags`` responses with the canonical
# ``id``/``name``/``created_at`` shape. Both schemas live in
# ``app.schemas.connection`` and are re-exported from
# ``app.schemas.__init__``.
from app.schemas import TagCreate, TagRead

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing ``Response`` in a ``TYPE_CHECKING`` guard satisfies ruff's
# strict ``flake8-type-checking`` configuration without breaking the
# return-type annotations on the route handlers. ``Response`` is the
# concrete Flask response object produced by ``jsonify``; using it as
# the return-type annotation is consistent with ``app.api.notes`` and
# ``app.api.health``.
if TYPE_CHECKING:
    from flask.wrappers import Response


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# The stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain; ``merge_contextvars``
# automatically attaches the request-scoped ``correlation_id``,
# ``user_id``, ``org_id``, and ``trace_id`` so log lines below carry
# full request context without per-call duplication.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# The blueprint is named ``"tags"`` so handlers are referenced as
# ``url_for("tags.list_tags")`` and ``url_for("tags.create_tag")``. No
# ``url_prefix`` is supplied here:
# ``app.api.__init__.register_blueprints`` registers this blueprint
# under ``/api/tags`` so the routes declared below at the empty
# relative path ``""`` are reachable at ``GET /api/tags`` and
# ``POST /api/tags``. Centralizing the URL prefix at registration
# time keeps the route table editable in one place and avoids prefix
# duplication if the blueprint is ever re-mounted under a different
# namespace.
tags_bp = Blueprint("tags", __name__)


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Only the blueprint object is part of the module's public surface.
# The route handlers ``list_tags`` and ``create_tag`` are intentionally
# NOT re-exported: callers should reach them through HTTP requests
# against the registered routes rather than invoking the view
# functions directly.
__all__ = ["tags_bp"]


# ---------------------------------------------------------------------------
# Route: GET /api/tags
# ---------------------------------------------------------------------------


@tags_bp.route("", methods=["GET"])
def list_tags() -> tuple[Response, int]:
    """List all tags in the actor's organization, alphabetized.

    Open to any authenticated user (no ``@requires_role`` decorator);
    the auth middleware enforces 401 for unauthenticated callers
    BEFORE this handler runs. Used for tag filter chips on the
    Connection Feed (F-004) and tag autocomplete on the Add/Edit
    Connection Form (F-001).

    Org scoping is mandatory per AAP Section 0.7.1 invariant 3
    ("Org-scoped multi-tenancy at the data model layer"). The
    ``WHERE org_id = g.session.org_id`` predicate is the architectural
    defense against cross-org enumeration; a user in Org A cannot see
    Org B's tags, and the response strictly contains only tags whose
    ``org_id`` matches the caller's session.

    Pagination is intentionally absent. Tag counts are bounded by org
    curation behavior (typical: 10-100 tags across the canonical
    industry / use-case / geography dimensions per the AAP Section
    0.1.2 user examples) and well under any reasonable pagination
    threshold. If a tenant ever exceeds ~1000 tags, this endpoint can
    be refactored to add ``limit``/``offset`` query parameters; the
    SPA's TanStack Query cache layer would then drive paginated
    autocomplete.

    Sort order is ``name ASC`` for predictable autocomplete UX: a
    user typing "fi" expects the dropdown to render "fintech",
    "first-mover" in alphabetical order without surprise.

    Response (HTTP 200)::

        [
            {"id": "...", "name": "fintech", "created_at": "..."},
            {"id": "...", "name": "logistics", "created_at": "..."},
        ]

    Returns:
        A 2-tuple of ``(jsonified list, 200)``. Flask consumes the
        tuple as the response. No failure paths are reachable from
        within this handler beyond the auth middleware's 401 for
        unauthenticated callers; the empty-org case returns an empty
        list with HTTP 200.

    Raises:
        Nothing. The auth middleware rejects unauthenticated callers
        with 401 BEFORE this handler runs; the database query is
        read-only and cannot fail with a uniqueness violation. Any
        unexpected ``Exception`` propagates to the global error
        handler in ``app.middleware.error_handlers`` and surfaces as
        a generic 500 envelope.
    """
    # Read the org id from the session populated by the auth
    # middleware. ``g.session`` is a frozen ``Session`` dataclass
    # (see ``app.middleware.auth.Session``) so attribute access is
    # safe and side-effect-free.
    org_id = g.session.org_id

    # Build the SELECT statement. The ``where`` clause enforces
    # org-scoped isolation; the ``order_by`` ensures deterministic
    # alphabetical ordering. The ``Tag`` model is imported from
    # ``app.models`` so the columns reference the same SQLAlchemy
    # mapping as the migration that created the table.
    stmt = select(Tag).where(Tag.org_id == org_id).order_by(asc(Tag.name))

    # Execute the query in a fresh session. The ``with`` block
    # ensures the connection is returned to the pool on exit.
    # ``scalars().all()`` yields a list of ORM ``Tag`` instances;
    # we then serialize each via ``TagRead.model_validate`` (which
    # respects the schema's ``from_attributes=True`` ORM-mode config)
    # and dump to JSON-serializable primitives via
    # ``model_dump(mode='json')`` so ``jsonify`` can produce a valid
    # JSON response without a custom encoder (UUIDs become strings,
    # datetimes become ISO-8601 strings).
    with db.session() as session:
        rows = session.execute(stmt).scalars().all()
        payload = [TagRead.model_validate(tag).model_dump(mode="json") for tag in rows]

    # Explicit 200 status (rather than relying on Flask's default)
    # so contract tests can pin to the literal value.
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Route: POST /api/tags
# ---------------------------------------------------------------------------


@tags_bp.route("", methods=["POST"])
@requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
def create_tag() -> tuple[Response, int]:
    """Create a tag in the actor's organization (idempotent on name).

    Request body (JSON)::

        {"name": "fintech"}

    Validation:
        * ``name`` is required, 1-64 characters (per the ``TagCreate``
          schema's ``Field(min_length=1, max_length=_TAG_NAME_MAX_CHARS)``
          constraint, where ``_TAG_NAME_MAX_CHARS = 64`` matches the
          ``Tag.name`` column's ``String(64)`` length in the
          PostgreSQL schema).
        * No additional fields permitted: the schema's
          ``model_config = _STRICT_CONFIG`` carries
          ``extra='forbid'``, rejecting attempts to set ``id``,
          ``org_id``, ``created_at``, or any other column that is
          server-derived. This defends against client tampering per
          AAP Section 0.7.4 (Security Invariants).
        * Whitespace is stripped before length validation:
          ``str_strip_whitespace=True`` on ``_STRICT_CONFIG`` ensures
          a payload of ``"   "`` becomes ``""`` post-strip and is
          rejected by ``min_length=1``. The defensive
          ``normalized_name = payload.name.strip()`` below is a
          belt-and-suspenders check; it should never trigger because
          pydantic strips first, but it remains as a guard against
          schema-config drift.

    Idempotency: if a tag with the same ``(org_id, name)`` pair
    already exists, the existing tag is returned with HTTP 200 (no
    new row created). A newly-created tag is returned with HTTP 201.
    The ``(org_id, name)`` uniqueness is enforced at the database
    layer by the ``uq_tags_org_name`` ``UniqueConstraint`` declared
    on :class:`app.models.tag.Tag`, so even concurrent requests with
    the same ``name`` resolve to a single row.

    RBAC: ``Contributor`` and ``Admin`` may create tags; ``Viewer``
    is rejected with HTTP 403. This mirrors the connection-create
    permission policy from the AAP Section 6.2 RBAC matrix because
    tag authorship is part of the curate-the-record workflow rather
    than the consume-the-record workflow.

    Audit: NO audit event is emitted. The eight
    :class:`app.models.enums.AuditEventType` values
    (``create``, ``status_change``, ``edit``, ``soft_delete``,
    ``hard_delete``, ``role_change``, ``authentication``,
    ``admin_op``) are scoped to records, users, and authentication.
    Tag CRUD is metadata-only and not audited; the records that
    REFERENCE tags are audited via the ``EDIT``/``CREATE`` events on
    those records, where tag-attribution traceability lives.

    Response (HTTP 201, newly created)::

        {"id": "...", "name": "fintech", "created_at": "..."}

    Response (HTTP 200, idempotent re-fetch)::

        {"id": "<existing-id>", "name": "fintech", "created_at": "..."}

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
                "fields": [{"loc": ["name"], "msg": "...", "type": "..."}],
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
        A 2-tuple of ``(jsonified TagRead, 201)`` for newly-created
        tags or ``(jsonified TagRead, 200)`` for idempotent re-fetch
        of an existing tag. Flask consumes the tuple as the response.
        Failure paths raise typed exceptions converted to the
        canonical envelope shape by the registered Flask error
        handlers; this function never returns a non-2xx response
        directly.

    Raises:
        ValidationFailedError: The request body is not a JSON object,
            or the body fails ``TagCreate`` schema validation, or the
            ``name`` is empty after whitespace trim. Mapped to HTTP
            422 by
            :func:`app.middleware.error_handlers._handle_validation_failed_error`.
        ConflictError: An unexpected ``IntegrityError`` was raised by
            ``session.commit()`` AND the post-rollback re-fetch did
            not find the duplicate row. This branch is unreachable
            under the current schema (the only integrity constraint
            on ``tags`` is the ``uq_tags_org_name`` unique constraint,
            which the SELECT-INSERT-SELECT race fallback resolves
            cleanly), but the explicit ``ConflictError`` raise
            remains as defense in depth against future schema
            evolutions that add new constraints. Mapped to HTTP 409
            by :func:`app.middleware.error_handlers._handle_conflict_error`.
        ForbiddenError: Raised by the ``@requires_role`` decorator
            when ``g.session.role`` is ``UserRole.VIEWER``. Mapped to
            HTTP 403 by
            :func:`app.middleware.error_handlers._handle_forbidden_error`.
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
    # ``model_validate`` would either coerce or fail in a less helpful
    # way; the explicit guard provides a uniform error message.
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": "Expected a JSON object containing 'name'.",
                    "type": "invalid_json",
                }
            ],
        )

    # Step 2: validate against the pydantic schema. The schema
    # enforces:
    #   * ``name`` is required (no default).
    #   * 1 <= len(name) <= 64 (post-whitespace-strip via
    #     ``str_strip_whitespace=True`` on the schema's
    #     ``_STRICT_CONFIG``).
    #   * ``extra='forbid'`` rejects any other field
    #     (e.g., a malicious attempt to set ``id`` or ``org_id``).
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
    # and the pattern in ``app.api.notes.generate``: drop pydantic's
    # ``input``/``ctx``/``url`` keys (PII / internal-detail leakage
    # risks) and keep only ``loc``/``msg``/``type``.
    try:
        payload = TagCreate.model_validate(raw_body)
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

    # Step 3: derive the org id and the normalized name. ``org_id``
    # is sourced exclusively from ``g.session`` (populated by the
    # auth middleware which runs BEFORE this handler) -- the client
    # CANNOT influence the org scope, defending against cross-tenant
    # tag injection per AAP Section 0.7.4.
    #
    # The ``normalized_name = payload.name.strip()`` is defense in
    # depth: the schema's ``str_strip_whitespace=True`` already
    # strips before length validation, so ``payload.name`` is
    # already trimmed by the time we get here. The explicit strip
    # below ensures the post-trim emptiness check below is correct
    # even if ``_STRICT_CONFIG`` is ever changed to drop
    # ``str_strip_whitespace``. Belt-and-suspenders.
    org_id = g.session.org_id
    normalized_name = payload.name.strip()
    if not normalized_name:
        raise ValidationFailedError(
            message="Tag name cannot be empty after trimming whitespace.",
            fields=[
                {
                    "loc": ["name"],
                    "msg": "Tag name cannot be empty after trimming whitespace.",
                    "type": "value_error.empty",
                }
            ],
        )

    # Step 4: open a session and execute the SELECT-then-INSERT
    # idempotent flow. The ``with db.session() as session:`` context
    # ensures the connection is returned to the pool on exit (whether
    # the handler returns successfully, raises, or commits).
    with db.session() as session:
        # Idempotency: look up the existing tag first via a
        # composite ``(org_id, name)`` predicate. The
        # ``uq_tags_org_name`` unique index (b-tree on the same
        # composite) makes this lookup sub-millisecond at any tenant
        # scale we care about (tag counts are bounded; the index
        # selectivity is high).
        existing_stmt = select(Tag).where(and_(Tag.org_id == org_id, Tag.name == normalized_name))
        existing = session.execute(existing_stmt).scalar_one_or_none()
        if existing is not None:
            # Common-case fast path: the tag already exists. Return
            # HTTP 200 (NOT 201) so the SPA can distinguish "you
            # created a new tag" from "the tag was already there".
            # The structured log line emits the resolved tag id for
            # observability without echoing user-supplied names that
            # might carry PII (rare but possible if the org curates
            # tags using employee surnames or similar).
            _logger.info(
                "tag_create_idempotent_existing",
                extra={
                    "org_id": str(org_id),
                    "tag_id": str(existing.id),
                    "user_id": str(g.session.user_id),
                },
            )
            return (
                jsonify(TagRead.model_validate(existing).model_dump(mode="json")),
                200,
            )

        # Step 5: not found via the SELECT; insert the new tag.
        # ``Tag(org_id=..., name=...)`` constructs an ORM instance
        # with ``id`` defaulted via ``default=uuid.uuid4`` and
        # ``created_at`` defaulted via ``server_default=func.now()``
        # at INSERT time (so the timestamp originates in PostgreSQL,
        # the canonical clock).
        tag = Tag(org_id=org_id, name=normalized_name)
        session.add(tag)

        try:
            session.commit()
        except IntegrityError as exc:
            # Race condition: another concurrent request created the
            # same tag between our SELECT and our INSERT. Roll back
            # the failed transaction (releasing the row-level lock
            # we may have acquired during the failed INSERT) and
            # re-fetch the winning row.
            #
            # Idempotent semantics from the caller's perspective: the
            # response is the same shape as the SELECT-found path
            # above, with HTTP 200 indicating "the tag now exists in
            # this org" without distinguishing whether THIS request
            # or a concurrent request was the one to create it. The
            # log line ``tag_create_race_condition_resolved``
            # documents the rare path for observability.
            session.rollback()
            concurrent = session.execute(existing_stmt).scalar_one_or_none()
            if concurrent is not None:
                _logger.info(
                    "tag_create_race_condition_resolved",
                    extra={
                        "org_id": str(org_id),
                        "tag_id": str(concurrent.id),
                        "user_id": str(g.session.user_id),
                    },
                )
                return (
                    jsonify(TagRead.model_validate(concurrent).model_dump(mode="json")),
                    200,
                )

            # Defensive branch: the INSERT raised ``IntegrityError``
            # but the post-rollback re-fetch did NOT find the
            # duplicate row. This is unreachable under the current
            # schema (the only constraint on ``tags`` is the
            # ``uq_tags_org_name`` unique index, which the
            # SELECT-INSERT-SELECT race fallback resolves cleanly),
            # but the explicit ``ConflictError`` raise remains as
            # defense in depth against future schema evolutions that
            # add new constraints (e.g., a future check constraint
            # on ``name`` format, or a foreign key to a tag
            # taxonomy). Surface as HTTP 409 so the SPA can render a
            # clear error rather than a generic 500. ``from exc``
            # preserves the original cause chain for log forensics.
            raise ConflictError(
                message="Could not create tag due to a constraint violation.",
                fields=[
                    {
                        "loc": ["name"],
                        "msg": "Tag could not be created due to a database constraint violation.",
                        "type": "constraint",
                    }
                ],
            ) from exc

        # Step 6: refresh to populate server-defaulted columns
        # (``id`` is set by Python's ``default=uuid.uuid4`` factory
        # at flush time; ``created_at`` is set by PostgreSQL's
        # ``NOW()`` via ``server_default=func.now()``). The refresh
        # is necessary because ``expire_on_commit=False`` on the
        # session factory means committed instances do NOT
        # auto-refresh -- we must explicitly request the round-trip
        # to read the server-assigned ``created_at`` value.
        session.refresh(tag)

        # Step 7: log the successful creation and return HTTP 201.
        # The ``tag_created`` event name is stable so log
        # search/alerting tooling can pin to it. We log the resolved
        # ``tag_name`` here (not the user-supplied raw name) because
        # at this point the name has passed validation and is the
        # authoritative identifier for the tag in the org's
        # taxonomy; engineers triaging "why does my org have N
        # tags?" need to see the names in the audit trail.
        _logger.info(
            "tag_created",
            extra={
                "org_id": str(org_id),
                "tag_id": str(tag.id),
                "tag_name": tag.name,
                "user_id": str(g.session.user_id),
            },
        )
        return (
            jsonify(TagRead.model_validate(tag).model_dump(mode="json")),
            201,
        )
