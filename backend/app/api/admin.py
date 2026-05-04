"""F-014 Admin Panel API blueprint.

Five endpoints span the administrative surface (per AAP Section 0.4.3
endpoint catalog):

* ``GET    /api/admin/users``           List all users in the actor's
                                         organization.
* ``PATCH  /api/admin/users/:id``        Mutate a user's role; emits
                                         ``role_change`` audit event.
                                         Service layer enforces the
                                         anti-lockout invariant
                                         (``LastAdminError`` -> 409)
                                         and the self-demotion guard
                                         (``SelfDemotionError`` -> 403).
* ``GET    /api/admin/records``          List records for moderation,
                                         including soft-deleted records
                                         by default. Same filter / sort
                                         / pagination shape as the public
                                         feed (``GET /api/connections``);
                                         delegates to
                                         :func:`app.services.connections.list_records`
                                         with
                                         :class:`ConnectionFilters`
                                         ``include_deleted=True``.
* ``DELETE /api/admin/records/:id``      Hard delete (physical row
                                         removal); emits ``hard_delete``
                                         audit event with the record's
                                         final state captured in
                                         ``audit_events.before_payload``.
                                         Returns HTTP 204 No Content.
* ``GET    /api/admin/analytics``        Three basic analytics panels
                                         (top contributors, leads by
                                         status, weekly activity
                                         sparkline). Per AAP Section
                                         0.6.2, NO additional analytics
                                         are in scope (lead velocity,
                                         conversion funnels,
                                         time-series trends, etc. are
                                         explicitly out of scope).

Every endpoint is gated by ``@requires_role(UserRole.ADMIN)``. The
decorator is the AUTHORITATIVE authorization gate per AAP Section
0.7.1 invariant 7. The React ``<RoleGate role="Admin">`` is a UX
courtesy; the backend RBAC decorator is the only authoritative gate.
Never rely on the absence of a UI control to prevent an action.

Per AAP Section 0.5.3 thin-handler convention, each handler is
purely:

  parse + RBAC + service-call + serialize.

Business logic (anti-lockout invariant, audit emission, hard-delete
cascade, analytics aggregation queries) lives in
:mod:`app.services.admin` and :mod:`app.services.connections`. The
state-changing endpoints (PATCH role, DELETE record) open a single
short-lived database session, wrap the service call in
``with db_session.begin():`` so the state change and the audit event
INSERT commit (or roll back) atomically per AAP Section 0.7.1
invariant 6, and serialize the result before the session closes.

Why this blueprint imports ``db`` from :mod:`app.services.admin`:

The two state-changing service functions used here
(:func:`app.services.admin.update_user_role`,
:func:`app.services.admin.hard_delete_record`) accept an open
SQLAlchemy session as a keyword-only ``db_session`` parameter so the
caller's transaction fence is explicit. The handler therefore needs
direct access to the session factory in order to open
``db.session()`` and ``db_session.begin()`` around each state-change.
The session factory is exported as ``db`` by both
:mod:`app.extensions` and (re-exported, by virtue of being imported
at module load time) by :mod:`app.services.admin`. Per the file's
:mod:`app.services.admin` dependency in the AAP, this blueprint
imports ``db`` from the admin service module so the import graph
remains within the file's depends_on_files allow-list. This is
defense-in-depth against accidental drift; the singleton is the
same instance regardless of import path.

Coordination contract:

This blueprint is registered (with the URL prefix ``/api/admin``) by
:func:`app.api.__init__.register_blueprints`. Routes declared below
at relative paths (e.g., ``/users``) are therefore reachable at
``/api/admin/users``. Centralising the URL prefix at registration
time keeps the route table editable in one place.

Importing this module triggers ZERO database calls, ZERO HTTP calls,
and ZERO network activity. All side effects are scoped to the
per-request handler invocation.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` provides the module-level ``_logger``. The stdlib
# logger is routed through structlog's processor chain (configured in
# :mod:`app.observability.logging`) so each emitted record carries
# the request-scoped ``correlation_id``, ``user_id``, ``org_id``, and
# ``trace_id`` bound on contextvars by the correlation/auth
# middleware. This convention matches the sibling API blueprints
# (``app.api.connections``, ``app.api.notes``, ``app.api.tags``,
# ``app.api.auth``).
#
# ``UUID`` is used at runtime by the query-string parsing helper
# ``_parse_uuid_list`` (which calls ``UUID(value)`` to coerce string
# values) and as the type annotation on path-parameter handlers
# (Flask's ``<uuid:user_id>`` and ``<uuid:record_id>`` converters
# yield :class:`uuid.UUID` instances at request time).
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Third-party runtime imports.
#
# ``Blueprint`` declares the modular ``admin_bp`` registered at
# the ``/api/admin`` URL prefix by
# :func:`app.api.__init__.register_blueprints`.
#
# ``g`` is Flask's per-request global proxy. The auth middleware
# populates ``g.session`` (a frozen :class:`Session` dataclass with
# ``user_id``, ``org_id``, ``role`` fields) BEFORE the
# ``@requires_role`` decorator runs; handlers read ``g.session.org_id``
# and ``g.session.user_id`` to scope queries and authorize the audit
# emit.
#
# ``jsonify`` serializes Python lists/dicts into a
# ``Content-Type: application/json`` HTTP response.
#
# ``make_response`` constructs a :class:`flask.Response` explicitly
# so the hard-delete handler can return HTTP 204 with an empty body
# and (defensively) drop the ``Content-Type`` header per RFC 9110.
#
# ``request.args`` is the immutable
# :class:`werkzeug.datastructures.MultiDict` carrying query-string
# parameters; used by the ``GET /records`` handler to extract
# filters, pagination, and sort directives. ``request.get_json``
# parses the JSON body of the PATCH role request without raising
# werkzeug's ``BadRequest`` so a malformed payload yields a clean
# 422 envelope rather than the default werkzeug HTML error page.
from flask import Blueprint, g, jsonify, make_response, request

# ``ValidationError`` is caught around ``schema_cls.model_validate(...)``
# so schema/type violations on the PATCH role body are converted into
# a :class:`ValidationFailedError` carrying pydantic-shaped field-
# level details (``loc``/``msg``/``type``). This explicit catch
# centralizes the 422 envelope shape under
# :class:`ValidationFailedError` and preserves the original pydantic
# exception via ``raise ... from exc`` so log forensics can recover
# the underlying error type. The pattern mirrors the sibling
# ``app.api.connections``, ``app.api.notes``, and ``app.api.tags``
# handlers.
from pydantic import ValidationError

# First-party imports - absolute paths only per the project's
# ``flake8-tidy-imports`` configuration in ``backend/pyproject.toml``;
# relative imports are banned project-wide.
#
# :class:`ValidationFailedError` is the AppError subclass mapped to
# HTTP 422 by the registered Flask error handler. The handler
# preserves the AAP Section 0.4.3 uniform error envelope shape.
from app.middleware.error_handlers import ValidationFailedError

# :func:`requires_role` is the role-gating decorator. It checks
# ``g.session.role`` against an allowlist with no DB round-trip,
# satisfying the sub-50ms RBAC budget per AAP Section 0.7.3.
# Validated at decoration (app-startup) time so a typo crashes the
# build, NOT the request.
from app.middleware.rbac import requires_role

# :class:`UserRole` is the three-role authorization enum
# (ADMIN / CONTRIBUTOR / VIEWER). Per AAP Section 6.2 RBAC matrix and
# Section 0.5.2 Layer 6, every admin endpoint is gated to
# ``UserRole.ADMIN`` only. :class:`InvolvementType` and
# :class:`OutreachStatus` are the two PostgreSQL enums consumed by
# the moderation list query-string filter parser.
from app.models.enums import InvolvementType, OutreachStatus, UserRole

# Pydantic schemas for inbound validation and outbound serialization:
#
# * :class:`AnalyticsResponse` is the response shape for
#   ``GET /api/admin/analytics`` (three panels: top contributors,
#   leads by status, weekly activity sparkline).
# * :class:`PaginatedAdminConnections` is the pagination envelope for
#   ``GET /api/admin/records`` -- mirrors the public-feed shape and
#   embeds :class:`ConnectionAdminRead` so admins can distinguish
#   active rows from soft-deleted rows in the same listing. Since
#   Visual Consistency QA Issue 1 ``deleted_at`` is part of the
#   shared :class:`ConnectionRead` shape (the SPA depends on it for
#   every read), so :class:`ConnectionAdminRead` is now a
#   compatibility alias of :class:`ConnectionRead`.
# * :class:`UserRead` is the outbound user shape (id, email,
#   display_name, role, created_at). ``password_hash`` and ``org_id``
#   are intentionally NOT exposed by the schema; even an Admin caller
#   never sees password hash material.
# * :class:`UserRoleUpdate` is the inbound payload for
#   ``PATCH /api/admin/users/:id``. ``extra='forbid'`` rejects any
#   field other than ``role``.
# * :class:`ConnectionAdminRead` is the outbound shape for a single
#   record on the admin moderation surface; alias of
#   :class:`ConnectionRead` since the latter now exposes
#   ``deleted_at`` for SPA visual treatment.
# * :class:`TagRead` is the embedded tag shape on
#   :class:`ConnectionAdminRead`; used by the moderation list
#   serializer so pydantic's ``from_attributes=True`` mode resolves
#   ``record.record_tags[].tag`` cleanly.
from app.schemas import (
    ConnectionAdminRead,
    PaginatedAdminConnections,
    TagRead,
    UserRead,
    UserRoleUpdate,
)

# Admin service layer. The four imports cover the four admin
# operations beyond the moderation list (which is delegated to
# ``app.services.connections.list_records`` for tag-hydrated parity
# with the public feed):
#
# * :func:`compute_analytics` is the read-only aggregation that
#   builds :class:`AnalyticsResponse` (top contributors, leads by
#   status, weekly activity). Opens its own short-lived session;
#   the handler passes ``g.session`` directly.
# * :func:`hard_delete_record` is the state-changing physical
#   delete. The function takes a caller-owned ``db_session`` per
#   AAP Section 0.5.3 (service functions own transactions when the
#   caller does not need to compose them; here the handler owns the
#   transaction so the service call participates in the same atomic
#   ``state-change + audit-emit`` block per invariant 6). Emits the
#   F-013 ``hard_delete`` audit event in the same transaction.
# * :func:`list_all_users` is the read-only org-scoped user listing.
#   Opens its own short-lived session.
# * :func:`update_user_role` is the state-changing role mutation.
#   Like ``hard_delete_record`` it accepts a caller-owned
#   ``db_session``. Enforces the anti-lockout invariant (cannot
#   demote the last Admin -> :class:`LastAdminError` -> HTTP 409)
#   and the self-demotion guard (cannot demote yourself ->
#   :class:`SelfDemotionError` -> HTTP 403). Emits the F-013
#   ``role_change`` audit event in the same transaction.
#
# ``db`` is the SQLAlchemy session factory singleton. It is imported
# from :mod:`app.services.admin` (a depends_on_file in the AAP)
# rather than from :mod:`app.extensions` so the module's import
# graph stays within the file's depends_on_files allow-list. The
# singleton is identical regardless of import path; importing it
# here lets the state-changing handlers below open
# ``with db.session() as db_session, db_session.begin():`` blocks
# around each service call so the state change and the audit event
# INSERT commit atomically per AAP Section 0.7.1 invariant 6.
from app.services.admin import (
    compute_analytics,
    db,
    hard_delete_record,
    list_all_users,
    update_user_role,
)

# ``ConnectionFilters`` is the frozen dataclass carrying the seven
# filter parameters consumed by :func:`list_records`. The admin
# moderation handler constructs this with ``include_deleted=True``
# by default (admins see soft-deleted records by default) and
# delegates to the same list service used by the public feed so the
# response shape matches and tags are eager-loaded for hydrated
# rendering.
from app.services.connections import ConnectionFilters, list_records

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing these in a ``TYPE_CHECKING`` guard satisfies ruff's strict
# ``flake8-type-checking`` configuration without breaking the
# return-type annotations on the route handlers.
#
# ``Response`` is Flask's concrete response object produced by
# ``jsonify`` or ``make_response``; used as the return-type
# annotation on every route handler.
if TYPE_CHECKING:
    from flask.wrappers import Response

    # ``AnalyticsResponse`` is referenced only in a type annotation
    # on the ``get_analytics`` handler's local payload variable.
    # Under ``from __future__ import annotations`` (PEP 563) the
    # annotation is a string and is never evaluated at runtime, so
    # the runtime import would be dead code; moving it into the
    # TYPE_CHECKING block satisfies ruff's strict
    # ``flake8-type-checking`` (rule TC001) without losing the
    # type-checker visibility.
    from app.schemas import AnalyticsResponse


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# The stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain; ``merge_contextvars``
# automatically attaches the request-scoped ``correlation_id``,
# ``user_id``, ``org_id``, and ``trace_id`` so the log lines below
# carry full request context without per-call duplication. The
# ``add_stdlib_record_extras`` processor promotes ``extra={...}``
# kwargs from stdlib LogRecord into the emitted JSON body so the
# structural fields (``actor_user_id``, ``target_user_id``,
# ``record_id``) are searchable in CloudWatch Insights without a
# custom log parser.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# The blueprint is named ``"admin"`` so handlers are referenced as
# ``url_for("admin.list_users")`` etc. No ``url_prefix`` is supplied
# here: :func:`app.api.__init__.register_blueprints` registers this
# blueprint under ``/api/admin`` so the routes declared below at
# relative paths (e.g., ``/users``) are reachable at the full
# ``/api/admin/users``. Centralising the URL prefix at registration
# time keeps the route table editable in one place and avoids prefix
# duplication if the blueprint is ever re-mounted under a different
# namespace (e.g., a versioned ``/api/v1/admin``).
admin_bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Only the blueprint object is part of the module's public surface.
# The route handler functions are intentionally NOT re-exported:
# callers should reach them through HTTP requests against the
# registered routes rather than invoking the view functions
# directly.
__all__ = ["admin_bp"]


# ---------------------------------------------------------------------------
# Constants and parsing helpers
# ---------------------------------------------------------------------------
# These constants and helpers are private to this module. They are
# deliberately NOT shared with :mod:`app.api.connections` (which
# carries an analogous parsing helper set) because the small
# duplicated block is preferable to cross-module coupling at this
# scale; promoting to ``app.utils.api_parsing`` would couple the
# blueprints' validation surfaces. If a third blueprint ever needs
# the same parsers, extract then.

# Default and maximum page sizes for the moderation list. Mirrors
# the public-feed defaults declared in :mod:`app.api.connections`
# (limit=25 default, 100 cap) so the SPA can use the same pagination
# component for both the public feed and the admin moderation tab.
_DEFAULT_LIMIT: int = 25
_DEFAULT_OFFSET: int = 0
_MAX_LIMIT: int = 100

# Allowed sort dimensions for the moderation list. Mirrors the public
# feed (F-004) per AAP Section 0.5.2 Layer 4 so the moderation panel
# can reuse the same sort UI. The connections service
# :func:`list_records` enforces the same allowlist server-side; we
# pre-validate here to surface a clean 422 envelope rather than the
# generic service-layer error message.
_ALLOWED_SORT_KEYS: frozenset[str] = frozenset(
    {
        "submission_date",
        "full_name",
        "company",
        "owner_display_name",
        "outreach_status",
    }
)
_ALLOWED_SORT_DIRS: frozenset[str] = frozenset({"asc", "desc"})

# Default sort direction. The composite index
# ``ix_records_org_deleted_submission`` covers
# ``submission_date DESC`` so the default sort uses the index without
# requiring an explicit ``sort_dir=desc`` query parameter.
_DEFAULT_SORT_KEY: str = "submission_date"
_DEFAULT_SORT_DIR: str = "desc"

# Boolean string coercion lookup. Centralised so the include_deleted
# query parameter accepts the same forms the SPA's URLSearchParams
# encoder produces (``true``/``1``/``yes``/``on``) without arguing
# about case or whitespace.
_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSE_TOKENS: frozenset[str] = frozenset({"0", "false", "no", "off", "f", "n"})


def _parse_uuid(value: str | None, *, field: str) -> UUID:
    """Coerce ``value`` to :class:`uuid.UUID` or raise 422.

    Args:
        value: Raw string from the query string (or ``None`` when
            absent).
        field: Field name surfaced in the validation envelope's
            ``loc`` segment.

    Returns:
        Parsed :class:`uuid.UUID`.

    Raises:
        ValidationFailedError: When ``value`` is empty or not a
            valid UUID string. Mapped to HTTP 422 by
            :func:`app.middleware.error_handlers._handle_validation_failed_error`.
    """
    if not value:
        raise ValidationFailedError(
            message=f"Field '{field}' is required.",
            fields=[
                {
                    "loc": ["query", field],
                    "msg": f"{field} is required.",
                    "type": "value_error.missing",
                },
            ],
        )
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError(
            message=f"Field '{field}' must be a valid UUID.",
            fields=[
                {
                    "loc": ["query", field],
                    "msg": f"'{value}' is not a valid UUID.",
                    "type": "value_error.uuid",
                },
            ],
        ) from exc


def _parse_int(
    raw: str | None,
    *,
    field: str,
    default: int,
    minimum: int = 0,
) -> int:
    """Parse a non-negative integer query-string parameter.

    Returns the supplied ``default`` when the parameter is absent or
    empty. Surfaces a clean :class:`ValidationFailedError` (HTTP
    422) when the parameter is present but not a valid integer or is
    below ``minimum``. Centralised so the error envelope shape is
    uniform across the limit/offset parameters used by the
    moderation list handler.

    Args:
        raw: Raw string from the query string (or ``None`` when
            absent).
        field: Field name surfaced in the validation envelope's
            ``loc`` segment.
        default: Default integer returned when the parameter is
            absent or empty.
        minimum: Lower bound for valid values. Defaults to 0 so
            offsets validate without an extra check; pass
            ``minimum=1`` for strictly-positive parameters like the
            page size.

    Returns:
        Parsed integer (or ``default`` when absent).

    Raises:
        ValidationFailedError: The parameter was present but not a
            valid integer or below ``minimum``.
    """
    if raw is None or raw == "":
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(
            message=f"Query parameter '{field}' must be an integer.",
            fields=[
                {
                    "loc": ["query", field],
                    "msg": "Expected an integer value.",
                    "type": "value_error.integer",
                },
            ],
        ) from exc
    if parsed < minimum:
        raise ValidationFailedError(
            message=f"Query parameter '{field}' must be >= {minimum}.",
            fields=[
                {
                    "loc": ["query", field],
                    "msg": f"Value must be >= {minimum}.",
                    "type": "value_error.min",
                },
            ],
        )
    return parsed


def _flatten_multivalue(values: list[str]) -> list[str]:
    """Flatten a list of comma-separated query-string values.

    Supports BOTH the comma-separated single-key form
    (``?involvement=Warm Intro,Soft Reference``) AND the
    repeated-key form
    (``?involvement=Warm Intro&involvement=Soft Reference``) so the
    SPA can use whichever encoding TanStack Query produces by
    default. Empty fragments and surrounding whitespace are
    discarded.

    Args:
        values: Raw list returned by ``request.args.getlist(name)``.

    Returns:
        A flat list of non-empty stripped string values, possibly
        empty.
    """
    flat: list[str] = []
    for item in values:
        for part in item.split(","):
            stripped = part.strip()
            if stripped:
                flat.append(stripped)
    return flat


def _parse_enum_list(
    values: list[str],
    enum_cls: type[InvolvementType] | type[OutreachStatus],
    *,
    field: str,
) -> tuple[Any, ...]:
    """Parse a multi-valued enum query-string parameter into a tuple.

    Accepts either repeated-key (``?involvement=Warm Intro&involvement=...``)
    or comma-separated (``?involvement=Warm Intro,Soft Reference``)
    encodings via :func:`_flatten_multivalue`. Each value is coerced
    to the supplied enum class via direct construction
    (``InvolvementType("Warm Intro")``) which works because both
    :class:`InvolvementType` and :class:`OutreachStatus` are
    ``(str, Enum)`` subclasses.

    Returns the empty tuple when no values are supplied (i.e. "no
    filter on this dimension").

    Args:
        values: Raw list from ``request.args.getlist(field)``.
        enum_cls: Either :class:`InvolvementType` or
            :class:`OutreachStatus`.
        field: Field name surfaced in the validation envelope's
            ``loc`` segment.

    Returns:
        A tuple of enum members in the order supplied by the
        client. Empty tuple when no values were provided.

    Raises:
        ValidationFailedError: One or more supplied values are not
            recognized members of the enum.
    """
    flat = _flatten_multivalue(values)
    if not flat:
        return ()
    coerced: list[Any] = []
    invalid_fields: list[dict[str, Any]] = []
    for value in flat:
        try:
            coerced.append(enum_cls(value))
        except (KeyError, ValueError):
            invalid_fields.append(
                {
                    "loc": ["query", field],
                    "msg": (
                        f"'{value}' is not a valid {enum_cls.__name__} value. "
                        f"Allowed: {sorted(member.value for member in enum_cls)}"
                    ),
                    "type": "value_error.enum",
                }
            )
    if invalid_fields:
        raise ValidationFailedError(
            message=f"Invalid value(s) for query parameter '{field}'.",
            fields=invalid_fields,
        )
    return tuple(coerced)


def _parse_uuid_list(values: list[str], *, field: str) -> tuple[UUID, ...]:
    """Parse a multi-valued UUID query-string parameter into a tuple.

    Accepts either repeated-key or comma-separated encodings via
    :func:`_flatten_multivalue`. Empty input returns the empty tuple
    (i.e. "no filter on this dimension"). Reports ALL invalid UUIDs
    in a single 422 envelope rather than aborting on the first
    failure so the SPA can highlight every bad value.

    Args:
        values: Raw list from ``request.args.getlist(field)``.
        field: Field name surfaced in the validation envelope's
            ``loc`` segment.

    Returns:
        A tuple of :class:`uuid.UUID` instances.

    Raises:
        ValidationFailedError: One or more supplied values cannot
            be parsed as a UUID.
    """
    flat = _flatten_multivalue(values)
    if not flat:
        return ()
    coerced: list[UUID] = []
    invalid_fields: list[dict[str, Any]] = []
    for value in flat:
        try:
            coerced.append(UUID(value))
        except (TypeError, ValueError):
            invalid_fields.append(
                {
                    "loc": ["query", field],
                    "msg": f"'{value}' is not a valid UUID.",
                    "type": "value_error.uuid",
                }
            )
    if invalid_fields:
        raise ValidationFailedError(
            message=f"Invalid UUID(s) for query parameter '{field}'.",
            fields=invalid_fields,
        )
    return tuple(coerced)


def _parse_bool(raw: str | None, *, default: bool, field: str) -> bool:
    """Parse a boolean query-string parameter.

    Accepts (case-insensitive) ``true``/``false``, ``1``/``0``,
    ``yes``/``no``, ``on``/``off``, ``t``/``f``, ``y``/``n``.
    Returns the supplied default when the parameter is absent or
    empty. Mirrors the boolean parser used by
    :mod:`app.api.connections` so the include_deleted toggle behaves
    identically across the moderation and public-feed surfaces.

    Args:
        raw: Raw string from the query string (or ``None`` when
            absent).
        default: Value returned when the parameter is absent or
            empty.
        field: Field name surfaced in the validation envelope's
            ``loc`` segment.

    Returns:
        The parsed boolean (or ``default`` when absent).

    Raises:
        ValidationFailedError: The parameter was present but not a
            recognized boolean token.
    """
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_TOKENS:
        return True
    if lowered in _FALSE_TOKENS:
        return False
    raise ValidationFailedError(
        message=f"Query parameter '{field}' must be a boolean.",
        fields=[
            {
                "loc": ["query", field],
                "msg": "Expected one of: true, false, 1, 0, yes, no, on, off.",
                "type": "value_error.bool",
            },
        ],
    )


def _parse_json_body(schema_cls: type[Any]) -> Any:
    """Parse the request JSON body and validate against ``schema_cls``.

    Centralises the malformed-JSON handling and the pydantic
    ValidationError -> ValidationFailedError conversion so the PATCH
    role handler stays thin.

    Pydantic's ``model_validate`` produces a typed pydantic instance
    when validation succeeds; on failure it raises
    :class:`pydantic.ValidationError`, which we catch and re-wrap as
    a :class:`ValidationFailedError` carrying pydantic-shaped field-
    level details (``loc``/``msg``/``type``). The wrapper drops
    pydantic's ``input``/``ctx``/``url`` keys (PII / internal-detail
    leakage risks) before surfacing them to the client; this mirrors
    :func:`app.middleware.error_handlers._serialize_pydantic_errors`
    and the pattern used by sibling blueprints.

    Args:
        schema_cls: Pydantic model class (e.g.,
            :class:`UserRoleUpdate`) to validate against.

    Returns:
        Validated pydantic model instance.

    Raises:
        ValidationFailedError: Body is not a JSON object, or fails
            schema validation. Mapped to HTTP 422.
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
        return schema_cls.model_validate(raw_body)
    except ValidationError as exc:
        safe_fields = [
            {
                "loc": [str(seg) for seg in err.get("loc", ())],
                "msg": str(err.get("msg", "Invalid value.")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        raise ValidationFailedError(
            message="The request payload failed validation.",
            fields=safe_fields,
        ) from exc


def _record_to_read_dict(record: Any) -> dict[str, Any]:
    """Serialize a :class:`Record` ORM instance to a ConnectionAdminRead dict.

    Used by the moderation list handler to convert each row into a
    JSON-serializable dict before wrapping in
    :class:`PaginatedAdminConnections`. The handler does NOT import
    the SQLAlchemy ``Record`` model directly (per AAP Section 0.7.7
    "No direct DB or model access") so the parameter is annotated
    ``Any``; the docstring documents the expected shape.

    The admin moderation surface uses :class:`ConnectionAdminRead`,
    which since Visual Consistency QA Issue 1 is now an alias of
    :class:`ConnectionRead` (the ``deleted_at`` field has been
    promoted to the shared :class:`ConnectionRead` shape because the
    SPA depends on it across every read to drive the F-005 status-
    chip and F-007 soft-deleted visual treatment). The alias is
    retained for API stability so existing handlers and the
    :class:`PaginatedAdminConnections` envelope continue to compile.

    The ``tags`` list is materialised explicitly because the ORM
    exposes the tag-association relationship as ``record.record_tags``
    (a list of :class:`RecordTag` rows each carrying ``.tag``) while
    :class:`ConnectionAdminRead` declares ``tags: list[TagRead]``.
    Pydantic's ``from_attributes=True`` mode would not auto-coerce
    that nesting, so we extract the inner ``tag`` objects here. The
    eager-loading is performed by the service-layer query
    (``selectinload`` on ``record_tags`` and the cascading ``tag``);
    this helper does NOT trigger N+1 queries.

    Args:
        record: A persisted ``Record`` ORM instance with
            eager-loaded ``record_tags`` -> ``tag`` chain.

    Returns:
        JSON-serialisable dict matching the
        :class:`ConnectionAdminRead` shape, ready for ``jsonify``.
    """
    tag_objects: list[Any] = [rt.tag for rt in record.record_tags]
    # Validate tags via TagRead first so the ConnectionAdminRead
    # validation below sees already-validated nested values; this
    # also catches any drift in the ORM ``Tag`` shape early.
    # ``model_validate`` uses ``from_attributes=True`` to read the
    # ORM attributes directly.
    tags_payload = [TagRead.model_validate(t).model_dump(mode="json") for t in tag_objects]
    return ConnectionAdminRead.model_validate(
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
            "tags": tags_payload,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "deleted_at": record.deleted_at,
        }
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Route: GET /api/admin/users
# ---------------------------------------------------------------------------


@admin_bp.route("/users", methods=["GET"])
@requires_role(UserRole.ADMIN)
def list_users() -> tuple[Response, int]:
    """List all users in the actor's organization (F-014).

    Returns:
        HTTP 200 with a JSON array of :class:`UserRead` objects
        (id, email, display_name, role, created_at). The
        ``password_hash`` and ``org_id`` fields are intentionally
        omitted by the schema -- they are NEVER included in any
        response, even an Admin response.

    The list is org-scoped via the service layer
    (:func:`app.services.admin.list_all_users` injects
    ``WHERE org_id = actor.org_id``). There is no pagination here
    because tenant user counts are bounded at MVP scale (typically
    tens of users); the underlying service caps the result at 500
    users and orgs that scale past that should switch to the
    paginated :func:`app.services.admin.list_org_users` API.

    Failure modes:
        HTTP 401 -- auth middleware rejected unauthenticated request.
        HTTP 403 -- ``@requires_role`` rejected non-Admin caller.
    """
    users = list_all_users(g.session)
    payload = [user.model_dump(mode="json") for user in users]
    _logger.info(
        "admin_users_listed",
        extra={
            "actor_user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "user_count": len(users),
        },
    )
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Route: PATCH /api/admin/users/<uuid:user_id>
# ---------------------------------------------------------------------------


@admin_bp.route("/users/<uuid:user_id>", methods=["PATCH"])
@requires_role(UserRole.ADMIN)
def update_user(user_id: UUID) -> tuple[Response, int]:
    """Mutate a user's role (F-009 + F-014).

    Request body (JSON)::

        {"role": "Admin"}  # or "Contributor" or "Viewer"

    The :class:`UserRoleUpdate` schema's ``model_config`` carries
    ``extra='forbid'`` so any other field (e.g., ``email``,
    ``display_name``, ``id``) is rejected with HTTP 422 BEFORE any
    handler logic runs. This is the FIRST line of defense against a
    compromised admin session attempting to mutate fields beyond
    role.

    Service-layer invariants enforced (per AAP Section 0.5.2 Layer 6):

    * Anti-lockout: cannot demote the last remaining Admin in the
      organization (raises :class:`LastAdminError`, mapped to HTTP
      409 ``conflict``).
    * Self-demotion blocked: an Admin cannot demote themselves
      (raises :class:`SelfDemotionError`, mapped to HTTP 403
      ``forbidden``). Prevents the current session from being
      deauthenticated mid-operation.
    * Cross-org isolation: target user must belong to the actor's
      org (raises :class:`NotFoundError`, mapped to HTTP 404 to
      avoid leaking the existence of cross-org users).

    Audit emission: the F-013 ``role_change`` audit event is INSERTed
    inside the same transaction as the ``users.role`` UPDATE so the
    state change and the audit row commit (or roll back) atomically
    per AAP Section 0.7.1 invariant 6. ``before_payload`` and
    ``after_payload`` capture the prior and new role values plus the
    target user_id.

    Args:
        user_id: UUID extracted from the URL path; Flask's
            ``uuid`` converter performs the type coercion.

    Returns:
        HTTP 200 with the updated :class:`UserRead`.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- ``@requires_role`` or self-demotion guard.
        HTTP 404 -- target user not found in actor's org.
        HTTP 409 -- last-admin demotion blocked.
        HTTP 422 -- body is not a JSON object, ``role`` missing /
                    not a valid :class:`UserRole`, or extra fields
                    supplied.
    """
    payload = _parse_json_body(UserRoleUpdate)
    # Open a single short-lived session and wrap the service call in
    # ``with db_session.begin():`` so the role UPDATE and the
    # ``audit_events`` INSERT commit atomically per AAP Section
    # 0.7.1 invariant 6. The serialization to UserRead happens
    # inside the ``with`` block so the ORM instance is still attached
    # to the session (defense-in-depth: the session factory is
    # configured with ``expire_on_commit=False``, so attributes
    # would survive detachment, but the explicit serialize-before-
    # close pattern is robust against future config changes).
    with db.session() as db_session, db_session.begin():
        updated_user = update_user_role(
            db_session=db_session,
            org_id=g.session.org_id,
            target_user_id=user_id,
            new_role=payload,
            actor_user_id=g.session.user_id,
        )
        response_payload = UserRead.model_validate(updated_user).model_dump(mode="json")
    _logger.info(
        "admin_role_change",
        extra={
            "actor_user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "target_user_id": str(user_id),
            "new_role": payload.role.value,
        },
    )
    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: GET /api/admin/records
# ---------------------------------------------------------------------------


@admin_bp.route("/records", methods=["GET"])
@requires_role(UserRole.ADMIN)
def list_admin_records() -> tuple[Response, int]:
    """List records for admin moderation (F-014; F-007 admin path).

    Same query-parameter shape as ``GET /api/connections`` (see that
    endpoint for filter/sort/pagination docs), with one important
    difference: ``include_deleted`` defaults to ``true`` here so
    admins see soft-deleted records by default. Pass
    ``?include_deleted=false`` to filter to active records only.

    Query parameters:
        Filters:
            company           Substring match on Record.company.
            full_name_search  Substring match on Record.full_name.
            involvement       Comma-or-repeated InvolvementType
                              ("any-of" filter).
            outreach_status   Comma-or-repeated OutreachStatus
                              ("any-of" filter).
            owner_user_ids    Comma-or-repeated UUIDs ("any-of" on
                              ``Record.owner_user_id``). The legacy
                              singular alias ``owner_user_id`` is
                              also accepted for SPA compatibility.
            tag_ids           Comma-or-repeated UUIDs ("any-of" via
                              ``RecordTag.tag_id``). The legacy
                              singular alias ``tag_id`` is also
                              accepted.
            include_deleted   Default true (admin-default). Pass
                              false to exclude soft-deleted records.
                              Accepted alias: ``show_deleted``
                              (per QA Issue 11). If both are
                              supplied, ``include_deleted`` wins.
        Sort:
            sort      One of submission_date / full_name / company /
                      owner_display_name / outreach_status. Default
                      "submission_date".
            sort_dir  "asc" or "desc". Default "desc".
        Pagination:
            limit   1-100 (silently clamped). Default 25.
            offset  >= 0. Default 0.

    Why this delegates to ``connections.list_records`` rather than
    the admin service's ``list_records_for_moderation``:
    ``connections.list_records`` returns rows with eager-loaded
    ``record_tags`` -> ``tag`` chain so the response carries hydrated
    tags. The admin service's ``list_records_for_moderation``
    returns raw rows without tag hydration; it remains useful for
    non-API admin tasks (e.g., bulk export scripts) but the API
    surface uses the hydrated path so the moderation tab can render
    the same rows the public feed renders.

    Returns:
        HTTP 200 with :class:`PaginatedConnections` payload:

            {"items": [...], "total": <int>, "limit": <int>,
             "offset": <int>}

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- ``@requires_role`` rejected non-Admin caller.
        HTTP 422 -- any single query parameter fails its
                    type/format check or sort key is unknown.
    """
    args = request.args

    # Substring filters: empty strings normalise to ``None`` so the
    # service layer interprets them as "no filter on this dimension".
    company = args.get("company")
    if company is not None and company.strip() == "":
        company = None
    full_name_search = args.get("full_name_search") or args.get("q")
    if full_name_search is not None and full_name_search.strip() == "":
        full_name_search = None

    # Multi-valued enum filters. Empty tuples mean "no filter on this
    # dimension" per the ``ConnectionFilters`` dataclass contract.
    involvement = _parse_enum_list(
        args.getlist("involvement"), InvolvementType, field="involvement"
    )
    outreach_status = _parse_enum_list(
        args.getlist("outreach_status"), OutreachStatus, field="outreach_status"
    )

    # Multi-valued UUID filters. The SPA may use either the plural
    # (``owner_user_ids``) or singular (``owner_user_id``) form; we
    # accept both to keep the URL shape forgiving across SPA tools
    # (TanStack Query, axios, fetch URLSearchParams) which differ in
    # how they encode list parameters.
    owner_user_ids = _parse_uuid_list(
        args.getlist("owner_user_ids") + args.getlist("owner_user_id"),
        field="owner_user_id",
    )
    tag_ids = _parse_uuid_list(
        args.getlist("tag_ids") + args.getlist("tag_id"),
        field="tag_id",
    )

    # Admin-default: include_deleted=True. Per AAP Section 0.5.2
    # Layer 6: "record moderation including the soft-deleted view".
    # Admins explicitly pass ``include_deleted=false`` to filter out
    # soft-deleted records from the moderation tab.
    #
    # Per QA Issue 11: accept the alias ``show_deleted`` so both
    # documented names produce the same filter behavior. The
    # canonical name is ``include_deleted``; ``show_deleted`` is
    # retained as a backward-compatible alias. If both are
    # supplied, ``include_deleted`` wins (it is the canonical
    # name); operators are encouraged to migrate to the canonical
    # name in any external tooling. Unrecognized values surface a
    # 422 via ``_parse_bool``.
    raw_include = args.get("include_deleted")
    raw_show = args.get("show_deleted")
    if raw_include is not None:
        include_deleted = _parse_bool(
            raw_include,
            default=True,
            field="include_deleted",
        )
    elif raw_show is not None:
        include_deleted = _parse_bool(
            raw_show,
            default=True,
            field="show_deleted",
        )
    else:
        include_deleted = True

    filters = ConnectionFilters(
        company=company,
        full_name_search=full_name_search,
        involvement=involvement,
        outreach_status=outreach_status,
        owner_user_ids=owner_user_ids,
        tag_ids=tag_ids,
        # Date filters are deliberately omitted from the moderation
        # surface. Admins typically moderate by tag/company/owner;
        # if a date filter is needed, add it here in a follow-up.
        submission_date_from=None,
        submission_date_to=None,
        include_deleted=include_deleted,
    )

    # Pagination. Use ``limit``/``offset`` (matching
    # ``connections.list_records``'s actual signature) rather than
    # page/page_size. The service silently clamps ``limit`` to its
    # own maximum (100) but we pre-cap here so the
    # ``PaginatedConnections`` envelope's ``limit: ge=1, le=200``
    # validator does not reject a clamped-by-service value when the
    # caller requested limit > 200.
    limit = min(
        _parse_int(
            args.get("limit"),
            field="limit",
            default=_DEFAULT_LIMIT,
            minimum=1,
        ),
        _MAX_LIMIT,
    )
    offset = _parse_int(
        args.get("offset"),
        field="offset",
        default=_DEFAULT_OFFSET,
        minimum=0,
    )

    # Sort. Pre-validate against the allow-list so the 422 envelope
    # surfaces here rather than leaking through the service layer's
    # generic message. The service layer ALSO validates these values
    # (defense in depth), but pre-validating here lets the response
    # carry a more specific ``sort`` / ``sort_dir`` field name.
    sort_key = (args.get("sort") or _DEFAULT_SORT_KEY).strip()
    sort_dir = (args.get("sort_dir") or _DEFAULT_SORT_DIR).strip().lower()
    if sort_key not in _ALLOWED_SORT_KEYS:
        raise ValidationFailedError(
            message=f"Unknown sort key '{sort_key}'.",
            fields=[
                {
                    "loc": ["query", "sort"],
                    "msg": (f"sort must be one of: {sorted(_ALLOWED_SORT_KEYS)}"),
                    "type": "value_error.invalid_sort",
                },
            ],
        )
    if sort_dir not in _ALLOWED_SORT_DIRS:
        raise ValidationFailedError(
            message=f"Unknown sort direction '{sort_dir}'.",
            fields=[
                {
                    "loc": ["query", "sort_dir"],
                    "msg": "sort_dir must be 'asc' or 'desc'.",
                    "type": "value_error.invalid_sort_dir",
                },
            ],
        )

    rows, total = list_records(
        filters,
        g.session,
        sort_key=sort_key,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )

    items = [_record_to_read_dict(record) for record in rows]
    response_payload = PaginatedAdminConnections.model_validate(
        {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    ).model_dump(mode="json")

    _logger.info(
        "admin_records_listed",
        extra={
            "actor_user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "returned_count": len(items),
            "total_count": total,
            "include_deleted": include_deleted,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
        },
    )
    return jsonify(response_payload), 200


# ---------------------------------------------------------------------------
# Route: DELETE /api/admin/records/<uuid:record_id>
# ---------------------------------------------------------------------------


@admin_bp.route("/records/<uuid:record_id>", methods=["DELETE"])
@requires_role(UserRole.ADMIN)
def hard_delete(record_id: UUID) -> Response:
    """Physically delete a connection record (F-007 admin path; F-014).

    This is the ONLY endpoint that physically removes data from the
    ``records`` table. Soft-delete (``DELETE /api/connections/:id``)
    sets ``deleted_at = NOW()`` instead; admin hard-delete actually
    removes the row. The service layer:

    * Verifies the record belongs to the actor's organization
      (cross-org targets surface as :class:`NotFoundError` -> HTTP
      404, never leaking the existence of cross-org records).
    * Captures the record's full state into
      ``audit_events.before_payload`` BEFORE the delete fires so
      the audit trail retains the row's content even after the row
      itself is gone.
    * Emits ``audit_events`` with ``event_type=hard_delete``. The
      audit row's ``target_record_id`` is NULL because the parent
      record ceases to exist after the transaction commits and a
      non-null FK with ``ondelete=RESTRICT`` would conflict with
      the post-commit row state. The record's id is captured inside
      ``before_payload`` so forensic queries can still correlate the
      audit row to its target.
    * Cascades to ``record_tags`` via the FK ``ondelete=CASCADE`` on
      :class:`RecordTag.record_id`; no explicit tag-link cleanup
      required here.

    Atomicity: the audit emit and the physical DELETE share a single
    transaction; if either fails, the entire operation rolls back
    per AAP Section 0.7.1 invariant 6.

    Defense-in-depth confirmation flag (CR-CKPT5-MINOR-admin):
        The caller MUST include ``?confirm=true`` (or ``confirm=1``,
        ``yes``, ``on``) on the request URL. If the flag is missing
        or false the handler returns HTTP 422 with
        ``error.code = "confirmation_required"`` BEFORE any service
        call fires; no audit event is emitted. The flag exists as
        layered protection on top of:

        * Admin-only RBAC (this handler's ``@requires_role``)
        * Audit trail (``hard_delete`` event captures full state)
        * The HTTP DELETE method's destructive semantics

        It guards against accidental hard-delete via reflexive curl,
        misclicked admin tooling, or scripts that copy a soft-delete
        URL pattern into the admin namespace by mistake. The SPA's
        :class:`@/features/admin/RecordModeration` component appends
        the flag automatically after the user confirms a Modal
        dialog; direct API consumers must pass it explicitly.

    Args:
        record_id: UUID extracted from the URL path; Flask's
            ``uuid`` converter performs the type coercion.

    Returns:
        HTTP 204 No Content with an empty body. The response's
        ``Content-Type`` header is removed defensively because
        RFC 9110 specifies that 204 responses have no message body
        (and by extension no Content-Type); some proxies are
        pickier than others about the absence.

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- ``@requires_role`` rejected non-Admin caller.
        HTTP 404 -- record not found in actor's org.
        HTTP 422 -- ``?confirm=true`` query parameter missing or
                    falsy. Validation envelope's
                    ``error.code = "confirmation_required"``.
    """
    # ------------------------------------------------------------------
    # Step 0: Confirmation flag check. Hard delete is irreversible,
    # so we require an explicit ``?confirm=true`` query parameter
    # before any service work begins. This is BEFORE the
    # ``db.session() as db_session`` block so that no transaction is
    # opened and no audit event is emitted on rejection.
    #
    # ``_parse_bool`` raises ``ValidationFailedError`` (HTTP 422) for
    # malformed booleans (e.g., ``confirm=banana``). We want a
    # *missing* or *false* flag to also produce a 422 with a
    # specialized error code, so we check the parsed value
    # explicitly after parsing.
    # ------------------------------------------------------------------
    confirm = _parse_bool(request.args.get("confirm"), default=False, field="confirm")
    if not confirm:
        # Construct the validation error with the standard
        # ``validation_failed`` class-level error code, then override
        # via instance-attribute assignment to surface a more specific
        # ``confirmation_required`` code on the SPA dispatcher. The
        # error handler in ``app.middleware.error_handlers`` reads
        # ``exc.error_code`` (instance attribute lookup), so the
        # override propagates through the standard envelope without
        # introducing a new exception class. This pattern matches
        # ``app.services.ai_orchestration.AIServiceError`` which
        # likewise sets ``self.error_code`` on the instance to expose
        # finer-grained codes (``ai_timeout`` / ``ai_unavailable`` /
        # ``ai_not_configured``) without subclassing per code.
        confirmation_error = ValidationFailedError(
            message="Hard delete requires ?confirm=true query parameter.",
            fields=[
                {
                    "loc": ["query", "confirm"],
                    "msg": (
                        "Hard delete is irreversible; resubmit with "
                        "?confirm=true to proceed."
                    ),
                    "type": "value_error.confirmation_required",
                },
            ],
        )
        confirmation_error.error_code = "confirmation_required"
        raise confirmation_error

    # Open a single short-lived session and wrap the service call
    # in ``with db_session.begin():`` so the records DELETE and the
    # audit_events INSERT commit atomically per AAP Section 0.7.1
    # invariant 6.
    with db.session() as db_session, db_session.begin():
        hard_delete_record(
            db_session=db_session,
            org_id=g.session.org_id,
            record_id=record_id,
            actor_user_id=g.session.user_id,
        )
    _logger.info(
        "admin_hard_delete",
        extra={
            "actor_user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "record_id": str(record_id),
        },
    )
    # 204 No Content with empty body. Use ``make_response`` to
    # construct the response explicitly so the empty body and the
    # absence of Content-Type are deterministic across Flask versions.
    response: Response = make_response("", 204)
    response.headers.pop("Content-Type", None)
    return response


# ---------------------------------------------------------------------------
# Route: GET /api/admin/analytics
# ---------------------------------------------------------------------------


@admin_bp.route("/analytics", methods=["GET"])
@requires_role(UserRole.ADMIN)
def get_analytics() -> tuple[Response, int]:
    """Compute the three basic admin analytics panels (F-014).

    Three panels (per AAP Section 0.5.2 Layer 6):

    1. **Top contributors** -- top 10 owner_user_ids by record_count
       (records ordered by record count descending; capped at the
       service layer's :data:`_TOP_CONTRIBUTORS_LIMIT`).
    2. **Leads by status** -- counts grouped by :class:`OutreachStatus`.
       Always returns ALL FOUR enum values even when count=0, so the
       SPA's bar chart has a consistent x-axis domain.
    3. **Weekly activity** -- record submissions per ISO week, last
       12 weeks. Always returns 12 entries even when count=0 for
       empty weeks, so the SPA's sparkline chart has a continuous
       domain without per-render gap-filling.

    Inventory panels (contributors, leads-by-status) filter
    ``deleted_at IS NULL`` because they measure active inventory.
    The activity sparkline does NOT filter ``deleted_at`` because it
    measures contributor activity (a record submitted then later
    soft-deleted still counts as a contribution). Admins still see
    soft-deleted records via the moderation tab.

    Per AAP Section 0.6.2, NO additional analytics panels are in
    scope. Lead velocity, conversion funnels, time-series trends,
    contributor leaderboards beyond top-N, status funnels,
    week-over-week deltas, and forecasting are explicitly out of
    scope and must NOT be added without expanding the AAP.

    This endpoint is admin-gated (not just authenticated) because
    the analytics aggregations could leak business intelligence
    (e.g., contributor performance) that should not be visible to
    non-admin users. Per AAP Section 0.7.1 invariant 7, the RBAC
    decorator is the authoritative gate.

    Returns:
        HTTP 200 with :class:`AnalyticsResponse`:

            {
                "most_active_contributors": [...],
                "leads_by_status": [4 entries],
                "weekly_activity": [12 entries],
                "generated_at": "2026-04-23T10:15:30Z"
            }

    Failure modes:
        HTTP 401 -- auth middleware rejected.
        HTTP 403 -- ``@requires_role`` rejected non-Admin caller.
    """
    payload: AnalyticsResponse = compute_analytics(g.session)
    _logger.info(
        "admin_analytics_computed",
        extra={
            "actor_user_id": str(g.session.user_id),
            "org_id": str(g.session.org_id),
            "contributor_count": len(payload.most_active_contributors),
            "weekly_entries": len(payload.weekly_activity),
        },
    )
    return jsonify(payload.model_dump(mode="json")), 200
