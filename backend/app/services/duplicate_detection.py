"""Duplicate-LinkedIn-URL detection service (F-010).

This module is the SOLE owner of the F-010 (Duplicate LinkedIn URL
Detection) business rule. The duplicate check is a non-blocking warning
per AAP section 0.7.6: the user is informed when a normalized LinkedIn
URL already exists in their organization's records but is NEVER
prevented from submitting. The HTTP API endpoint
``GET /api/connections/duplicate-check`` (defined in
:mod:`app.api.connections`) accepts the raw user-supplied URL,
delegates to :func:`check_duplicate` here, and surfaces the
informational :class:`app.schemas.connection.ConnectionDuplicateCheckResponse`
back to the SPA. The 409 ``ConflictError`` is reserved for
``app.services.connections.create_record`` when the actual UNIQUE
constraint fires (race condition or schema-bypass attempt); duplicate
checks themselves never produce a 409.

Public API
----------

:func:`find_duplicate`
    Low-level lookup function. Accepts an already-normalized URL and
    returns the matching :class:`app.models.Record` instance or
    ``None``. Reusable by ``app.services.connections.create_record``
    for the in-transaction duplicate guard so the create-flow does
    not need to re-implement the lookup.

:func:`check_duplicate`
    High-level entry point invoked by the API layer. Validates and
    normalizes the raw URL, performs the lookup, and packages the
    result in a :class:`ConnectionDuplicateCheckResponse` payload
    suitable for direct JSON serialization. Raises
    :class:`app.middleware.error_handlers.ValidationFailedError` if
    the URL is malformed or unnormalizable so the registered Flask
    error handler emits HTTP 422 with field-scoped error metadata.

Architectural invariants (per AAP section 0.7.1)
-------------------------------------------------

* **Org-scoped multi-tenancy (invariant 3).** Every query injects
  ``Record.org_id == actor.org_id`` so a record in a different org
  CANNOT match. Cross-org isolation is preserved.
* **Soft-delete-aware reads (invariant 4).** Every query injects
  ``Record.deleted_at IS NULL`` so a soft-deleted record's
  ``normalized_linkedin_url`` does NOT collide with a fresh
  insertion. The partial unique index
  ``uq_records_org_normalized_linkedin_url_active`` matches this
  predicate, allowing the lookup to be a single B-tree seek.
* **No audit emission.** A duplicate check is a read-only
  observation; per AAP section 0.7.1 invariant 6, audit events are
  emitted only on state-changing paths.
* **Centralized normalization.** This module never re-implements URL
  normalization; the canonical form is owned by
  :func:`app.utils.url.normalize_linkedin_url` so write-time and
  duplicate-check-time canonicalization stay in lockstep.

Performance budget (per AAP section 0.7.3)
-------------------------------------------

The duplicate check has a sub-second budget at the 10K-record-per-org
scale ceiling. The implementation hits the partial unique index
directly via the ``(org_id, normalized_linkedin_url) WHERE
deleted_at IS NULL`` predicate, so the database performs a single
B-tree seek; no table scan is required. Empty-string inputs short
circuit BEFORE any database round-trip to keep the check cheap on
malformed input.

Logging
-------

Two structured log lines are emitted on the happy path:

* ``duplicate_check_clean`` - no match found; carries the
  ``normalized_url`` for forensic correlation.
* ``duplicate_check_match`` - a match was found; carries the
  ``normalized_url`` and the ``existing_record_id`` so an operator
  can reconstruct the warning the contributor saw.

The ``correlation_id``, ``user_id``, ``org_id``, and ``role`` are NOT
emitted explicitly here because the auth middleware
(:mod:`app.middleware.auth`) and correlation middleware
(:mod:`app.middleware.correlation`) bind them into ``structlog``'s
contextvars on every request; ``merge_contextvars`` automatically
surfaces them on every log line emitted within the request scope.
"""

from __future__ import annotations

# Standard library imports - alphabetized within sections per ruff's
# isort configuration with ``force-sort-within-sections=true``.
#
# ``typing.TYPE_CHECKING`` gates the type-only imports of ``UUID``,
# ``DBSession``, and ``Session`` so the project's strict
# ``flake8-type-checking`` configuration is satisfied. ``typing.Any``
# is consumed at runtime in the typed local that constructs the
# ``fields`` list passed to :class:`ValidationFailedError` so it
# CANNOT live inside the ``TYPE_CHECKING`` block - it must be
# imported unconditionally.
from typing import TYPE_CHECKING, Any

# Third-party runtime imports.
#
# ``sqlalchemy.and_`` combines the three predicates that hit the
# partial unique index (``org_id`` equality, ``normalized_linkedin_url``
# equality, ``deleted_at IS NULL``). ``sqlalchemy.select`` builds the
# 2.x-style SELECT statement against the ``records`` table.
#
# ``structlog.get_logger(__name__)`` produces a JSON-emitting bound
# logger. The ``merge_contextvars`` processor configured in
# :mod:`app.observability.logging` automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id``
# bound by the correlation/auth middleware, so log lines emitted
# here are automatically correlated by request without any per-call
# bookkeeping.
from sqlalchemy import and_, select
import structlog

# First-party imports - absolute paths only per the project's
# ``flake8-tidy-imports`` configuration (relative imports are banned
# under AAP section 0.3.7).
#
# ``db`` is the SQLAlchemy 2.x wrapper singleton from
# :mod:`app.extensions`. ``find_duplicate`` uses ``db.session()`` to
# open a fresh database session when no caller-supplied session is
# provided, mirroring the convention used elsewhere in
# :mod:`app.services`.
#
# ``ValidationFailedError`` is the typed exception raised by
# :func:`check_duplicate` when the supplied URL is malformed or
# unnormalizable. The registered Flask error handler in
# :mod:`app.middleware.error_handlers` converts it to a 422 JSON
# envelope with the ``fields`` array surfaced to the SPA.
#
# ``Record`` is the SQLAlchemy declarative model for the ``records``
# table. The duplicate check filters by three of its columns
# (``org_id``, ``normalized_linkedin_url``, ``deleted_at``) and
# returns the matching row entity to the caller.
#
# ``ConnectionDuplicateCheckResponse`` is the pydantic 2.x response
# schema returned by :func:`check_duplicate`. The schema is the
# AUTHORITATIVE serialization contract; the field name
# ``duplicate_found`` (NOT ``is_duplicate``) is the canonical key
# the SPA reads to render the non-blocking warning banner.
#
# ``is_valid_linkedin_url`` and ``normalize_linkedin_url`` are pure
# helpers - they never raise; they return ``False`` / empty string on
# malformed input. :func:`check_duplicate` promotes their results
# into typed exceptions appropriate for the HTTP 422 envelope.
from app.extensions import db
from app.middleware.error_handlers import ValidationFailedError
from app.models import Record
from app.schemas.connection import ConnectionDuplicateCheckResponse
from app.utils.url import is_valid_linkedin_url, normalize_linkedin_url

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are strings (PEP 563) and the symbols inside the
# ``TYPE_CHECKING`` block are never evaluated at runtime, satisfying
# the project's strict ``flake8-type-checking`` configuration.
#
# ``uuid.UUID`` annotates the ``org_id`` (required) and
# ``exclude_record_id`` (optional) parameters of :func:`find_duplicate`
# and :func:`check_duplicate`. The functions never call ``UUID(...)``
# or ``isinstance(x, UUID)`` at runtime - the values flow straight
# through to SQLAlchemy column-equality predicates which accept the
# UUID instances directly via psycopg's UUID adapter. So the type
# is needed only at type-check time.
#
# ``sqlalchemy.orm.Session`` (imported as ``DBSession`` alias) is the
# typed parameter for the optional caller-supplied session in
# :func:`find_duplicate`. The function calls ``session.execute(stmt)``
# at runtime, but Python is duck-typed; the parameter object itself
# carries the method regardless of the annotation.
#
# ``app.middleware.auth.Session`` annotates the ``actor`` parameter
# of :func:`check_duplicate`. The function reads ``actor.org_id`` at
# runtime, again via duck-typed attribute access without needing the
# class to be importable at runtime.
if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session as DBSession

    from app.middleware.auth import Session


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# structlog's ``merge_contextvars`` processor (configured in
# :mod:`app.observability.logging`) automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id`` bound
# by the correlation/auth middleware so log lines emitted here are
# automatically correlated by request without any per-call work.
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------

# ``__all__`` is sorted alphabetically (RUF022 "isort-style" sorting)
# so the surface mirrors the same convention used in sibling
# ``app.services.*`` modules.
__all__ = [
    "check_duplicate",
    "find_duplicate",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Stable error codes consumed by the SPA's typed ``ApiError`` dispatch
# and matched against by the SPA's translation layer. Adding a new
# code is a non-breaking change; renaming an existing code is
# breaking and requires SPA coordination per AAP section 0.7.5
# Explainability rule.
_FIELD_CODE_INVALID_FORMAT: str = "invalid_format"
_FIELD_CODE_UNNORMALIZABLE: str = "unnormalizable"

# The canonical pydantic field name for the LinkedIn URL parameter
# in the inbound payload. Matches the field name used by
# :class:`app.schemas.connection.ConnectionCreate.linkedin_url` and
# :class:`app.schemas.connection.ConnectionUpdate.linkedin_url` so
# the SPA can apply field-scoped error messaging consistently
# across the create-flow validator and the duplicate-check flow.
_FIELD_NAME_LINKEDIN_URL: str = "linkedin_url"


# ---------------------------------------------------------------------------
# Public function: find_duplicate
# ---------------------------------------------------------------------------


def find_duplicate(
    org_id: UUID,
    normalized_url: str,
    *,
    exclude_record_id: UUID | None = None,
    db_session: DBSession | None = None,
) -> Record | None:
    """Return the first non-soft-deleted record matching ``normalized_url``.

    The lookup is scoped to the caller's organization and to active
    (non-soft-deleted) rows only, hitting the partial unique index
    ``uq_records_org_normalized_linkedin_url_active`` for a single
    B-tree seek at MVP scale. Empty-string inputs short-circuit
    BEFORE any database round-trip so callers can pass the raw
    output of :func:`app.utils.url.normalize_linkedin_url` (which
    returns ``""`` on malformed input) without an extra guard.

    The function is the low-level building block reused by:

    * :func:`check_duplicate` - the API-friendly wrapper that
      packages the result into the
      :class:`ConnectionDuplicateCheckResponse` schema.
    * ``app.services.connections.create_record`` - the create-flow
      handler that performs an in-transaction duplicate guard
      (passing its own open session via ``db_session=...`` so it
      sees uncommitted writes within the same transaction).

    Per AAP section 0.7.1 invariant 3 (org-scoped multi-tenancy),
    the lookup MUST inject ``Record.org_id == org_id``. Per AAP
    section 0.7.1 invariant 4 (soft-delete-aware reads), the lookup
    MUST inject ``Record.deleted_at IS NULL``. Both predicates are
    structurally aligned with the partial unique index so the
    PostgreSQL planner can use it directly without re-checking
    against the heap.

    The ``exclude_record_id`` parameter is the edit-flow escape
    hatch: when a contributor edits an existing record (record X),
    the duplicate check needs to know if some OTHER record has the
    same URL - NOT record X itself. Without this exclusion, an edit
    that does not change the URL would always self-report as a
    duplicate and confuse the contributor. The handler in
    :mod:`app.api.connections` passes the record id from the URL
    path parameter when invoking the duplicate check during the
    PATCH flow.

    Args:
        org_id: The caller's organization id. The SQL predicate
            ``Record.org_id == org_id`` ensures cross-org records
            CANNOT match, satisfying the multi-tenant scoping
            invariant.
        normalized_url: A LinkedIn URL already normalized via
            :func:`app.utils.url.normalize_linkedin_url`. Empty
            strings yield ``None`` without a database round-trip
            because the empty string is the malformed-input
            sentinel from the normalizer (it cannot collide with
            any real LinkedIn URL since real URLs always begin
            with ``https://``).
        exclude_record_id: Optional record id to exclude from the
            lookup. Used by the edit path so editing a record does
            not flag itself as a duplicate.
        db_session: Optional caller-supplied session. When
            provided, the lookup runs inside the caller's
            transaction so it sees uncommitted writes the caller
            has staged (the in-transaction duplicate guard pattern
            used by ``connections.create_record``). When ``None``,
            a fresh session is opened and closed automatically via
            ``with db.session() as session:`` so the caller does
            not need to manage lifecycle.

    Returns:
        The first matching :class:`app.models.Record` instance, or
        ``None`` when no match exists. Only one row can match in
        practice because the partial unique index forbids
        duplicates among active rows; ``LIMIT 1`` is applied
        defensively so a hypothetical schema regression does not
        cause an unbounded scan.

    Examples:
        >>> # Inside a transaction-owning service:
        >>> with db.session() as session, session.begin():
        ...     existing = find_duplicate(
        ...         org_id=actor.org_id,
        ...         normalized_url="https://linkedin.com/in/jane",
        ...         db_session=session,
        ...     )
        ...     if existing is not None:
        ...         raise ConflictError("Record already exists.")
        ...     session.add(record)
    """
    # Empty-string short-circuit. The normalizer returns "" for any
    # malformed input, and a real LinkedIn URL always begins with
    # "https://", so an empty string can never match a real row.
    # Returning early avoids a wasted database round-trip on
    # malformed input that the API layer should have already
    # rejected via :func:`is_valid_linkedin_url`.
    if not normalized_url:
        return None

    # Build the SELECT statement using SQLAlchemy 2.x core style.
    # ``and_`` is structurally aligned with the partial unique index
    # ``uq_records_org_normalized_linkedin_url_active`` so the
    # PostgreSQL planner uses it directly:
    #
    #   CREATE UNIQUE INDEX uq_records_org_normalized_linkedin_url_active
    #     ON records (org_id, normalized_linkedin_url)
    #     WHERE deleted_at IS NULL;
    #
    # The leading ``org_id`` provides equality scoping; the trailing
    # ``normalized_linkedin_url`` provides the lookup key; the
    # partial-index predicate ``WHERE deleted_at IS NULL`` matches
    # the ``Record.deleted_at.is_(None)`` predicate one-for-one so
    # the planner does not need to re-check any heap rows.
    stmt = select(Record).where(
        and_(
            Record.org_id == org_id,
            Record.normalized_linkedin_url == normalized_url,
            Record.deleted_at.is_(None),
        )
    )

    # Edit-flow exclusion. Applied as an additional WHERE predicate
    # so the planner can still use the partial unique index for the
    # primary equality lookup; the index entry is fetched and the
    # ``Record.id != exclude_record_id`` check happens in-memory on
    # the at-most-one matching row.
    if exclude_record_id is not None:
        stmt = stmt.where(Record.id != exclude_record_id)

    # Defensive ``LIMIT 1``. The partial unique index forbids
    # duplicate active rows in practice, but applying the limit
    # keeps the query bounded if a hypothetical schema regression
    # ever allows multiple matches. ``scalar_one_or_none()`` raises
    # ``MultipleResultsFound`` if more than one row is returned, so
    # the limit also prevents a confusing exception from leaking
    # out of the service layer.
    stmt = stmt.limit(1)

    # Choose the session source. Caller-supplied session is used
    # directly so the caller's transaction state is honored
    # (uncommitted writes are visible). When no session is supplied,
    # a fresh session is opened via the SQLAlchemy 2.x context
    # manager so the connection is automatically returned to the
    # pool on exit.
    if db_session is not None:
        return db_session.execute(stmt).scalar_one_or_none()

    with db.session() as session:
        return session.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Public function: check_duplicate
# ---------------------------------------------------------------------------


def check_duplicate(
    linkedin_url: str,
    actor: Session,
    *,
    exclude_record_id: UUID | None = None,
) -> ConnectionDuplicateCheckResponse:
    """Public entry point for ``GET /api/connections/duplicate-check`` (F-010).

    Workflow:

    1. Validate the raw LinkedIn URL format via
       :func:`app.utils.url.is_valid_linkedin_url`. Malformed URLs
       are rejected with a 422 response BEFORE any database access.
    2. Normalize via :func:`app.utils.url.normalize_linkedin_url`.
       The normalizer returns ``""`` on any input it cannot
       canonicalize; that case is rejected as ``unnormalizable`` to
       give the SPA a distinct error code from
       ``invalid_format``.
    3. Query for an active (non-soft-deleted) record with the same
       normalized URL in the actor's organization, leveraging
       :func:`find_duplicate`.
    4. Return :class:`ConnectionDuplicateCheckResponse` with the
       canonical normalized URL echoed back and (when matched) the
       existing record's id, owner display name, and submission
       date for the SPA's non-blocking warning UI.

    The response is INFORMATIONAL only - per AAP section 0.7.6,
    duplicate detection is a warning, not a block. The API layer
    NEVER converts a duplicate match into an HTTP error: it always
    returns 200 OK with ``duplicate_found=true|false``. The 409
    ``ConflictError`` is reserved for
    ``app.services.connections.create_record`` when the actual
    UNIQUE constraint fires (race condition or schema-bypass
    attempt).

    Args:
        linkedin_url: Raw LinkedIn URL as supplied by the client via
            the ``?linkedin_url=...`` query string parameter. Must
            be a syntactically valid LinkedIn profile URL per
            :func:`is_valid_linkedin_url`; malformed values surface
            as HTTP 422 with ``fields[0].code = "invalid_format"``.
        actor: Authenticated session populated by
            :mod:`app.middleware.auth`. The ``actor.org_id`` field
            is read to enforce cross-org isolation; no other
            attribute of ``actor`` is consumed.
        exclude_record_id: Optional record id to exclude from the
            search. Set by the edit-flow handler so editing a
            record without changing its URL does NOT self-report
            as a duplicate. Defaults to ``None`` for the
            new-record-flow handler.

    Returns:
        :class:`ConnectionDuplicateCheckResponse` with:

        * ``duplicate_found`` set to ``True`` iff a matching
          non-soft-deleted record exists in ``actor.org_id``.
        * ``normalized_linkedin_url`` always populated with the
          canonical form so the SPA can echo it back to the user.
        * ``existing_record_id``, ``existing_owner_display_name``,
          and ``existing_submission_date`` populated only when a
          duplicate was found; ``None`` otherwise.

    Raises:
        ValidationFailedError: when the URL is not a valid LinkedIn
            profile URL (``code = "invalid_format"``) or when
            normalization yields an empty string
            (``code = "unnormalizable"``). The registered Flask
            error handler converts the exception to HTTP 422 with
            the field-scoped envelope so the SPA can surface the
            error inline against the LinkedIn URL input.

    Examples:
        >>> # In the duplicate-check endpoint handler:
        >>> response = check_duplicate(
        ...     linkedin_url="https://www.LinkedIn.com/in/JaneDoe/",
        ...     actor=g.session,
        ... )
        >>> response.duplicate_found  # bool
        >>> response.normalized_linkedin_url  # 'https://linkedin.com/in/janedoe'
    """
    # Step 1: Format validation. The validator is a pure function
    # that returns False on any malformed input (None, empty,
    # non-string, wrong host, wrong path) without raising. This
    # module promotes the False result to a typed
    # ``ValidationFailedError`` so the registered Flask error
    # handler can produce the 422 JSON envelope the SPA expects.
    if not is_valid_linkedin_url(linkedin_url):
        # The ``fields`` payload follows the project's field-scoped
        # error convention: a list of dicts each with a ``field``
        # key (matching the canonical pydantic field name) and a
        # ``code`` key (a stable identifier the SPA dispatches on).
        # The typed local makes the ``Any`` import meaningful while
        # also documenting the dict shape for downstream
        # maintainers.
        invalid_fields: list[dict[str, Any]] = [
            {
                "field": _FIELD_NAME_LINKEDIN_URL,
                "code": _FIELD_CODE_INVALID_FORMAT,
            }
        ]
        raise ValidationFailedError(
            message="LinkedIn URL is not in a recognized format.",
            fields=invalid_fields,
        )

    # Step 2: Normalization. The normalizer returns "" on any input
    # it cannot canonicalize. Under normal flow this branch is
    # unreachable because :func:`is_valid_linkedin_url` returning
    # True implies the URL is structurally well-formed and will
    # normalize successfully. The defensive branch exists so a
    # future divergence between validator and normalizer
    # (e.g., a hypothetical bug fix to one without the other) does
    # NOT silently produce a duplicate-check payload with an empty
    # ``normalized_linkedin_url`` echo.
    normalized = normalize_linkedin_url(linkedin_url)
    if not normalized:
        unnormalizable_fields: list[dict[str, Any]] = [
            {
                "field": _FIELD_NAME_LINKEDIN_URL,
                "code": _FIELD_CODE_UNNORMALIZABLE,
            }
        ]
        raise ValidationFailedError(
            message="LinkedIn URL could not be normalized.",
            fields=unnormalizable_fields,
        )

    # Step 3: Database lookup. ``find_duplicate`` opens its own
    # short-lived session because :func:`check_duplicate` is the
    # API-tier entry point and is not invoked from inside an
    # already-open transaction. The org-scope and
    # soft-delete-aware predicates are enforced by
    # :func:`find_duplicate` so this call site does not need to
    # re-state them.
    existing = find_duplicate(
        actor.org_id,
        normalized,
        exclude_record_id=exclude_record_id,
    )

    # Step 4a: Clean-state response. No duplicate found - return a
    # response with ``duplicate_found=False`` and only the
    # normalized URL echo populated. The other ``existing_*``
    # fields default to ``None`` per the schema definition.
    if existing is None:
        # Log the clean check at info level. The normalized URL is
        # safe to log because it is public-facing data the
        # contributor supplied (and the auth middleware has already
        # filtered out any secrets via the structlog redactor
        # processor configured in app.observability.logging).
        logger.info(
            "duplicate_check_clean",
            normalized_url=normalized,
        )
        return ConnectionDuplicateCheckResponse(
            duplicate_found=False,
            normalized_linkedin_url=normalized,
            existing_record_id=None,
            existing_owner_display_name=None,
            existing_submission_date=None,
        )

    # Step 4b: Match-state response. Duplicate found - emit the
    # warning log line and return the response with the existing
    # record's identifying fields populated so the SPA can render
    # "This contact was already submitted by <Owner> on <Date>".
    #
    # The ``existing_record_id`` is logged as a string for
    # consistent JSON serialization across log aggregators that
    # may not natively serialize uuid.UUID instances. The
    # ``owner_display_name`` is INTENTIONALLY NOT logged because
    # it is PII; the SPA receives it via the response payload
    # only.
    logger.info(
        "duplicate_check_match",
        normalized_url=normalized,
        existing_record_id=str(existing.id),
    )
    return ConnectionDuplicateCheckResponse(
        duplicate_found=True,
        normalized_linkedin_url=normalized,
        existing_record_id=existing.id,
        existing_owner_display_name=existing.owner_display_name,
        existing_submission_date=existing.submission_date,
    )
