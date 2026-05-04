"""SQLAlchemy 2.x declarative model for the records table.

``Record`` is the central business entity of the Sales-Connections
platform. Each row represents a Connection Idea a contributor logs
about a person in their network.

Cross-cutting features touched by this model:
    F-001 Connection Idea Form         - nine business fields
    F-002 AI Note Generation           - ai_notes is editable, optional
    F-003 Involvement Indicator        - three-valued enum (required)
    F-004 Connection Feed              - composite index for fast paging
    F-005 Outreach Status              - four-valued enum (default Not Started)
    F-006 Owner Attribution            - owner_user_id NOT NULL,
                                         owner_display_name denormalized
    F-007 Soft Delete                  - deleted_at nullable timestamptz
    F-009 RBAC                         - org_id NOT NULL (multi-tenant scope)
    F-010 Duplicate LinkedIn Detection - normalized_linkedin_url + partial
                                         unique index (in __table_args__)
    F-011 Detail + Edit History        - relationship to AuditEvent
    F-013 Audit Trail                  - atomic emit on every state change

Indexes declared on the table (per AAP section 0.4.7):
    ix_records_owner_user_id            single-column on owner_user_id (via
                                        ``index=True``) - supports the
                                        owner-filter dimension on the feed.
    ix_records_org_deleted_submission   composite over
                                        (org_id, deleted_at, submission_date)
                                        for the F-004 feed query.
    ix_records_involvement              single-column for filter dim 1.
    ix_records_outreach_status          single-column for filter dim 2.
    uq_records_org_normalized_linkedin_url_active
                                        partial unique index over
                                        (org_id, normalized_linkedin_url)
                                        WHERE deleted_at IS NULL - for F-010
                                        (PostgreSQL-only via
                                        ``postgresql_where``).

Per AAP section 0.5.3, this module declares schema only. Business
logic (URL normalization, soft-delete-aware queries, audit emission)
lives in ``app.services.connections``, ``app.utils.url``, and
``app.services.audit``.

This module deliberately has no module-level side effects beyond
class registration on the SQLAlchemy mapper registry. Importing
``app.models.record`` does not log, does not make HTTP calls, does
not touch the filesystem, and does not connect to any database.
"""

from __future__ import annotations

# ``datetime`` and ``Mapped``/``mapped_column``/``relationship`` MUST be
# importable at module load time. SQLAlchemy 2.x's declarative system
# parses ``Mapped[...]`` annotations at class-construction time via
# ``de_stringify_annotation`` (see :mod:`sqlalchemy.util.typing`),
# which does ``eval()`` on the annotation string and looks up names
# in the originating module's ``__globals__``. Moving these names
# into a ``TYPE_CHECKING`` block would therefore raise
# ``MappedAnnotationError`` at import. The ``noqa`` suppressions
# below are necessary, not stylistic.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship  # noqa: TC002

from app.extensions import Base
from app.models.enums import InvolvementType, OutreachStatus

if TYPE_CHECKING:
    # Sibling-model imports are gated to type-check time only. The
    # SQLAlchemy declarative classes for ``Organization``, ``User``,
    # ``RecordTag``, and ``AuditEvent`` reference this class via
    # ``relationship(back_populates=...)`` and vice versa, which would
    # form a circular import graph at module load time if these were
    # unconditional imports. The string-based ``primaryjoin`` and
    # ``foreign_keys`` arguments on the :func:`relationship` calls
    # below are forward references that SQLAlchemy resolves lazily
    # during the registry's ``configure_mappers()`` pass once both
    # sides of every ``back_populates`` pair are loaded.
    from app.models.audit_event import AuditEvent
    from app.models.organization import Organization
    from app.models.tag import RecordTag
    from app.models.user import User


__all__ = ["Record"]


class Record(Base):
    """A Connection Idea record.

    The model encodes the nine business fields the contributor sees on
    the Add Connection form (F-001) plus the operational fields needed
    for multi-tenant scoping, owner attribution, soft delete, and
    duplicate detection.

    Atomic invariants enforced by the migration and services:
        * Every CREATE/UPDATE/DELETE on this table emits an audit
          event in the same transaction (per AAP section 0.7.1
          invariant 6).
        * Reads default to ``WHERE deleted_at IS NULL`` (the
          soft-delete invariant, AAP section 0.7.1 invariant 4).
        * Writes default to ``WHERE org_id = g.session.org_id`` (the
          multi-tenant invariant, AAP section 0.7.1 invariant 3).

    Attributes:
        id: Primary key. UUID v4 generated client-side via the
            ``default=uuid.uuid4`` factory when the caller does not
            supply a value (the typical case).
        org_id: Foreign key to ``organizations.id``. Non-null;
            ``ondelete="RESTRICT"`` because organization deletion is
            out of scope for MVP. Not separately indexed because the
            composite ``(org_id, deleted_at, submission_date)`` index
            also serves single-column ``org_id`` lookups via the
            leading-column property of B-tree indexes.
        owner_user_id: Foreign key to ``users.id``. Non-null per the
            F-006 invariant that every record has a permanent owner.
            ``ondelete="RESTRICT"`` prevents accidental loss of
            attribution when a user is deleted. Indexed via
            ``index=True`` for fast filter-by-owner queries on the
            feed (one of the F-004 filter dimensions).
        owner_display_name: Denormalized snapshot of the owner's
            display name at the time of record creation. Read by the
            F-004 feed query directly to avoid an inner join to
            ``users`` on every page load. Audit-style invariance: a
            later display-name change in ``users`` does NOT propagate
            to historical record rows, faithful to the F-006
            "permanent owner identity attached to every record"
            requirement.
        full_name: Required business field - the connection's full
            legal/preferred name as the contributor knows them.
        linkedin_url: Required business field - the connection's raw
            LinkedIn profile URL as the contributor entered it.
            Length 2048 matches IETF RFC 7230's conservative URL
            ceiling. The format is validated by ``app.utils.url``
            before insertion.
        normalized_linkedin_url: Required canonical form of
            ``linkedin_url`` used as the unique-index key for F-010
            duplicate detection. Computed by
            ``app.utils.url.normalize_linkedin_url`` (lower-case
            host, strip trailing slash, drop query parameters and
            fragments). The
            ``uq_records_org_normalized_linkedin_url_active`` partial
            unique index in ``__table_args__`` enforces uniqueness
            scoped by org and only for ACTIVE rows.
        company: Required business field - the connection's current
            employer.
        job_title: Required business field - the connection's current
            title at ``company``.
        relationship_context: Required free-form prose describing how
            the contributor knows the connection. Fed to the
            Anthropic Claude prompt (F-002) to generate
            ``ai_notes``. Stored as ``Text`` (no length cap at the
            DB layer); the pydantic schema layer enforces the
            ``AI_PROMPT_CONTEXT_MAX_CHARS`` ceiling per F-002
            sanitization.
        ai_notes: OPTIONAL AI-generated outreach copy
            (contributor-editable). Nullable because (a) AI
            generation may have failed and the contributor may have
            chosen to submit anyway per AAP section 0.7.6
            ("AI failure does not block submission") or (b) the
            contributor explicitly cleared the suggestion.
        involvement: Required three-state enum (F-003) declaring how
            the submitter wishes to participate in outreach: Warm
            Intro, Soft Reference, or Target Only. Bound to the
            PostgreSQL ``involvement_type`` enum via
            ``values_callable`` so that the human-friendly value
            string (e.g., ``"Warm Intro"``) round-trips to the
            database exactly. Indexed for cheap filter-by-involvement
            on the feed.
        outreach_status: Required four-state enum (F-005) tracking
            sales-team progression: Not Started, In Progress,
            Contacted, Closed. Both Python-side ``default`` and
            DB-side ``server_default`` set the initial value to
            ``"Not Started"`` so the column is correctly populated
            regardless of the insertion path (ORM, raw SQL, or data
            migration). Indexed for cheap filter-by-status on the
            feed.
        submission_date: Server-assigned creation timestamp.
            ``server_default=func.now()`` ensures the value
            originates in PostgreSQL (the canonical clock) rather
            than in the Python process, eliminating clock-skew
            discrepancies between Gunicorn workers.
        deleted_at: NULLABLE soft-delete sentinel (F-007). NULL means
            an active record; a non-null timestamp means the row was
            soft-deleted at that moment. All read paths inject
            ``WHERE deleted_at IS NULL`` per AAP invariant 4. The
            partial unique index on ``normalized_linkedin_url`` is
            scoped to active rows only so a soft-delete-then-recreate
            flow with the same URL is supported.
        created_at: Server-assigned timestamp at row insertion. Used
            for audit and forensics; distinct from
            ``submission_date`` only at the conceptual level (in MVP
            the two fire from the same ``NOW()`` call), but
            decoupled so that a future bulk-import flow can preserve
            the original submission timestamp while ``created_at``
            reflects the import time.
        updated_at: Server-assigned timestamp on row insertion AND
            on every ORM-driven update (via ``onupdate=func.now()``).
            Raw SQL UPDATEs would NOT trigger this; in MVP all
            updates flow through the service layer
            (``app.services.connections``) so the ORM-level
            ``onupdate`` is sufficient.
        organization: Many-to-one relationship to
            :class:`app.models.organization.Organization`. Bound via
            the ``organization.records`` collection on the
            organization side using ``back_populates``. The string
            forward reference ``"Organization"`` is resolved by the
            SQLAlchemy class registry during the lazy
            ``configure_mappers()`` pass.
        owner: Many-to-one relationship to
            :class:`app.models.user.User`. Bound via the
            ``user.records`` collection on the user side using
            ``back_populates``. ``foreign_keys`` is declared
            explicitly to keep the mapping unambiguous if the schema
            ever evolves to include additional FKs to ``users``.
        record_tags: One-to-many relationship to
            :class:`app.models.tag.RecordTag` association rows.
            ``cascade="all, delete-orphan"`` mirrors the database-
            level ``ondelete=CASCADE`` on the FK in ``record_tags``
            so that detaching a tag association in Python marks the
            row for deletion at flush time. Tag associations are
            pure metadata with no value beyond the parent record/tag
            pair; cascade-deletion on hard-delete is safe and
            intentional.
        audit_events: One-to-many relationship to
            :class:`app.models.audit_event.AuditEvent` rows whose
            ``target_record_id`` matches this record's ``id``.
            CRITICAL: NO cascade configured. Audit events MUST
            persist even if the record is hard-deleted (admin only).
            The DB-level FK on ``audit_events.target_record_id`` has
            ``ondelete="RESTRICT"`` which physically prevents
            hard-delete from succeeding while audit events reference
            the record; the admin hard-delete flow is responsible
            for archiving or null-ifying
            ``audit_events.target_record_id`` first if required.
            String-based ``primaryjoin`` and ``foreign_keys``
            arguments avoid circular imports between
            ``record.py``, ``audit_event.py``, and ``user.py``.
    """

    __tablename__ = "records"

    # __table_args__ declares all four extra indexes (the leading
    # owner_user_id index is created via ``index=True`` on that
    # mapped_column below). Listing the composite feed index FIRST so
    # the Postgres planner sees it during query planning at table
    # introspection time.
    __table_args__ = (
        # Primary feed-query index (F-004): scoped by org,
        # soft-delete-aware, ordered by ``submission_date DESC``.
        # PostgreSQL's B-tree index can use ``deleted_at`` as a range
        # predicate (WHERE deleted_at IS NULL) while the leading
        # ``org_id`` provides equality and the trailing
        # ``submission_date`` provides ordering. The DESC ordering is
        # EXPLICIT per decision log entry DL-0027: PostgreSQL CAN scan
        # an ascending index backward for ``ORDER BY ... DESC``, but a
        # forward scan on a DESC index is consistently 5-10% faster at
        # the 10K-record scale ceiling (AAP Section 0.7.3 feed-load
        # budget). Declaring ``text("submission_date DESC")`` here
        # rather than the bare column name keeps this model
        # character-for-character aligned with the canonical migration
        # at ``backend/migrations/versions/0001_initial_schema.py``,
        # which eliminates the alembic-autogenerate drift risk that
        # would otherwise emit a spurious ASC-recreation migration on
        # the next ``alembic revision --autogenerate`` invocation.
        Index(
            "ix_records_org_deleted_submission",
            "org_id",
            "deleted_at",
            text("submission_date DESC"),
        ),
        # Filter-dimension indexes (F-004): support cheap filtering
        # by involvement type and outreach status without scanning
        # the table.
        Index("ix_records_involvement", "involvement"),
        Index("ix_records_outreach_status", "outreach_status"),
        # Duplicate-detection partial unique index (F-010): scoped by
        # org, only enforced for active (non-soft-deleted) rows.
        # PostgreSQL-specific syntax via ``postgresql_where``.
        # Without this clause, uniqueness would apply to ALL rows
        # including soft-deleted ones, blocking the soft-delete-then-
        # recreate flow described in F-007.
        Index(
            "uq_records_org_normalized_linkedin_url_active",
            "org_id",
            "normalized_linkedin_url",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    # ------------------------------------------------------------------
    # Operational columns
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ------------------------------------------------------------------
    # Business fields - the nine F-001 fields
    # ------------------------------------------------------------------
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    linkedin_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_linkedin_url: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_context: Mapped[str] = mapped_column(Text, nullable=False)
    ai_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    involvement: Mapped[InvolvementType] = mapped_column(
        SQLEnum(
            InvolvementType,
            name="involvement_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    outreach_status: Mapped[OutreachStatus] = mapped_column(
        SQLEnum(
            OutreachStatus,
            name="outreach_status",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=OutreachStatus.NOT_STARTED,
        server_default=text("'Not Started'::outreach_status"),
    )
    submission_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Soft-delete and audit timestamps
    # ------------------------------------------------------------------
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    # ``organization`` is the many-to-one parent in the multi-tenant
    # scope. The ``Organization.records`` reciprocal collection is
    # declared on the Organization model (sibling) with
    # ``back_populates="organization"``.
    organization: Mapped[Organization] = relationship(back_populates="records")

    # ``owner`` is the WHO of every record (F-006). The reciprocal
    # ``User.records`` collection is declared on the User model
    # (sibling) with ``back_populates="owner"``. ``foreign_keys`` is
    # declared explicitly with the string forward reference
    # ``"Record.owner_user_id"`` so the mapping remains unambiguous
    # even if the schema ever evolves to include another FK to
    # ``users`` (e.g., a future ``last_edited_by`` column).
    owner: Mapped[User] = relationship(
        back_populates="records",
        foreign_keys="Record.owner_user_id",
    )

    # ``record_tags`` is the many-to-many bridge to :class:`Tag` via
    # the :class:`RecordTag` association class. The reciprocal
    # ``RecordTag.record`` is declared on the association class with
    # ``back_populates="record_tags"``. ``cascade="all,
    # delete-orphan"`` mirrors the DB-level ``ondelete=CASCADE`` on
    # the FK in ``record_tags`` so that detaching an association in
    # Python flushes a DELETE; tag associations are pure metadata
    # with no value beyond the parent record/tag pair.
    record_tags: Mapped[list[RecordTag]] = relationship(
        back_populates="record",
        cascade="all, delete-orphan",
    )

    # ``audit_events`` exposes the F-013 / F-011 edit-history feed
    # for this record. The reciprocal ``AuditEvent.target_record`` is
    # declared on the AuditEvent model with
    # ``back_populates="audit_events"``.
    #
    # CRITICAL (per AAP Section 0.7.1 invariant 5 -- "Append-only
    # audit table. No code path issues UPDATE or DELETE against
    # audit_events. Database-level grants enforce this in production"):
    #
    #   1. NO cascade is configured. Audit events MUST persist even
    #      if the record is hard-deleted (admin-only).
    #   2. ``passive_deletes=True`` is REQUIRED so SQLAlchemy does NOT
    #      auto-issue ``UPDATE audit_events SET target_record_id = NULL``
    #      when ``db_session.delete(record)`` is called. Without this
    #      flag, SQLAlchemy's default "nullify" behavior on a
    #      one-to-many relationship would emit an ORM-driven UPDATE on
    #      ``audit_events`` BEFORE the parent DELETE -- silently
    #      mutating audit rows from application code, which violates
    #      the append-only invariant. With ``passive_deletes=True``
    #      SQLAlchemy delegates dependent-row handling entirely to the
    #      database-level FK constraint (``ondelete="RESTRICT"`` on
    #      ``audit_events.target_record_id``). When audit history
    #      exists for a record, RESTRICT fires and PostgreSQL raises
    #      an IntegrityError; the admin service catches that and
    #      surfaces a clean 409 Conflict to the caller. Hard delete
    #      thus succeeds only for records with NO audit history --
    #      a rare condition in practice (every API-created record has
    #      at least a CREATE audit), so admins are pushed toward
    #      soft-delete which preserves audit history.
    #   3. The string-based ``primaryjoin`` and ``foreign_keys`` are
    #      required to break the circular import cycle between this
    #      module and ``app.models.audit_event``.
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="target_record",
        primaryjoin="Record.id == AuditEvent.target_record_id",
        foreign_keys="AuditEvent.target_record_id",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        Includes only the primary key and the outreach status as a
        discriminator. Deliberately EXCLUDES every business field
        (``full_name``, ``linkedin_url``, ``company``, ``job_title``,
        ``relationship_context``, ``ai_notes``) because those fields
        are personally identifiable information (PII). Even though
        the structlog redactor configured in
        :mod:`app.observability.logging` filters secret-named keys
        at the logging boundary, the safer pattern is to never put
        PII in ``__repr__`` in the first place: ``repr(record)``
        appears in exception tracebacks, debugger output, and
        ad-hoc logging that may bypass the redactor.

        The format is intentionally stable so that test assertions
        and operational tooling can match it exactly:
        ``<Record id={uuid} status={status_value}>``.
        """
        return f"<Record id={self.id} status={self.outreach_status.value}>"
