"""SQLAlchemy 2.x declarative model for the audit_events table (F-013).

The audit_events table is **append-only**. The application role's
PostgreSQL grants permit ``INSERT`` only; ``UPDATE`` and ``DELETE`` are
revoked at the database privilege layer (per AAP section 0.7.4 security
invariant). The corresponding Alembic migration applies these grants;
this model file simply declares the schema and disables ORM-side
staleness checks via ``__mapper_args__`` so SQLAlchemy never attempts
speculative UPDATE/DELETE operations.

Eight event types are tracked (per AAP section 0.4.7):

    create, status_change, edit, soft_delete, hard_delete,
    role_change, authentication, admin_op

Each row captures:

    * actor_user_id      WHO performed the action (always non-null)
    * target_record_id   ON WHICH record (nullable for events like
                         authentication that have no record target)
    * event_type         WHAT kind of action (one of eight enum values)
    * event_timestamp    WHEN (server-side default NOW())
    * before_payload     OPTIONAL prior state (JSONB)
    * after_payload      OPTIONAL new state (JSONB)

The emitter helper :func:`app.services.audit.emit_audit_event` is the
ONLY production code path that creates rows in this table. Per AAP
section 0.7.1 invariant 6, every state change must call the emitter
inside the same database transaction so the state change and audit row
commit atomically (or roll back together).

The append-only invariant has THREE enforcement layers (defense in
depth):

    1. Database (Alembic migration):
       ``REVOKE UPDATE, DELETE ON audit_events FROM <app_role>;``
    2. ORM (this file):
       ``__mapper_args__ = {"confirm_deleted_rows": False}`` so the
       session never speculatively flushes deletes.
    3. Application (services/audit.py):
       ``emit_audit_event(...)`` is the only writer; code review and
       tests enforce the convention.

A single mistake in one layer does not break the audit trail's
integrity.

This module is part of the Layer 2 implementation per AAP section 0.5.1
(F-006 owner attribution + F-009 RBAC + F-013 audit emitter), and
declares schema only - per AAP section 0.5.3 ("Models hold no behavior
beyond declarations"). All audit emission logic lives in
:mod:`app.services.audit`.
"""

from __future__ import annotations

# ``datetime`` and ``Mapped`` MUST be importable at module load time.
# SQLAlchemy 2.x's declarative system parses ``Mapped[...]`` annotations
# at class-construction time via ``de_stringify_annotation``
# (see :mod:`sqlalchemy.util.typing`), which does ``eval()`` on the
# annotation string and looks up names in the originating module's
# ``__globals__``. Moving these names into a ``TYPE_CHECKING`` block
# would therefore raise ``MappedAnnotationError`` at import. The
# ``noqa`` suppressions below are necessary, not stylistic.
#
# ``Any`` is also evaluated at runtime because it appears inside the
# ``Mapped[dict[str, Any] | None]`` annotation on the JSONB payload
# columns; the same de_stringification pass that resolves ``Mapped``
# resolves the inner generic arguments too.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import (
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship  # noqa: TC002

from app.extensions import Base
from app.models.enums import AuditEventType

if TYPE_CHECKING:
    # Sibling-model imports are gated to type-check time only. The
    # SQLAlchemy declarative classes for ``User`` and ``Record`` reference
    # this class via ``relationship(back_populates="audit_events", ...)``
    # and vice versa, which would form a circular import graph at module
    # load time if these were unconditional imports. The string-based
    # ``primaryjoin`` and ``foreign_keys`` arguments on the
    # :func:`relationship` calls below are forward references that
    # SQLAlchemy resolves lazily during the registry's
    # ``configure_mappers()`` pass once both sides of every
    # ``back_populates`` pair are loaded.
    from app.models.record import Record
    from app.models.user import User


__all__ = ["AuditEvent"]


class AuditEvent(Base):
    """An append-only audit record capturing a state-changing operation.

    Append-only invariant:

        * Database-level: PostgreSQL grants restrict the application role
          to INSERT (and SELECT) only on this table. The Alembic
          migration applies the REVOKE UPDATE, DELETE clauses.
        * ORM-level: :attr:`__mapper_args__` disables row-version
          staleness checks so SQLAlchemy never issues an UPDATE or
          DELETE during ``session.flush``.
        * Application-level: :func:`app.services.audit.emit_audit_event`
          is the sole writer; tests verify by static analysis that no
          other code path mutates :class:`AuditEvent` instances.

    Use cases:

        * F-013 audit trail visible at ``GET /api/connections/:id/history``
        * Compliance-grade timeline of every state change in the system
        * Forensics: who did what, when, and what changed

    Attributes:
        id: Primary key. UUID v4 generated client-side via the
            ``default=uuid.uuid4`` factory when the caller does not
            supply a value (the typical case).
        actor_user_id: Foreign key to ``users.id``. Identifies WHO
            performed the action. Non-null on every row - even
            ``authentication`` events have a known actor (the user
            logging in or out). ``ondelete="RESTRICT"`` protects the
            audit trail from accidental orphaning if a user row were
            ever hard-deleted.
        target_record_id: Foreign key to ``records.id``. Identifies
            WHICH record the action targeted. NULLABLE because the
            ``authentication`` event type has no record target, and the
            ``role_change`` event type targets a user (captured in the
            payload) rather than a record. Logging code populates this
            field only when a record is the target.
            ``ondelete="RESTRICT"`` protects audit history from being
            silently lost when a record is hard-deleted; admins must
            explicitly archive or null out audit events before purging
            the record.
        event_type: One of the eight :class:`AuditEventType` values.
            Bound to the PostgreSQL ``audit_event_type`` enum via
            ``values_callable`` so that the Python enum's ``.value``
            (lowercase snake_case strings) round-trips to the database
            exactly. Indexed for fast filtering by event type in admin
            moderation views.
        event_timestamp: Server-side timestamp set by
            ``server_default=func.now()`` on INSERT. Database-side
            timestamping ensures monotonicity within a transaction and
            removes a class of clock-skew bugs across multiple Gunicorn
            workers. Per AAP section 0.7.3 the audit emission budget is
            100 ms; the fastest path is a single round-trip with NOW()
            filled in by Postgres.
        before_payload: OPTIONAL JSONB blob capturing the prior state
            of the affected entity. NULL for ``create`` events (no
            prior state); for ``status_change`` carries the old status;
            for ``edit`` carries the affected fields' prior values.
            JSONB (binary, indexable, deduplicated) is preferred over
            JSON (text) because writes are frequent and ad-hoc forensic
            queries are valuable.
        after_payload: OPTIONAL JSONB blob capturing the new state of
            the affected entity. NULL for ``hard_delete`` events (no
            new state); for ``create`` carries the entire serialized
            record; for ``edit`` carries the affected fields' new
            values.
        actor: Many-to-one relationship to :class:`app.models.user.User`,
            paired with the ``user.audit_events`` collection on the user
            side via ``back_populates="audit_events"``. String-based
            ``primaryjoin`` and ``foreign_keys`` arguments avoid
            circular imports between ``audit_event.py``, ``user.py``,
            and ``record.py``.
        target_record: Many-to-one relationship to
            :class:`app.models.record.Record` (nullable), paired with
            the ``record.audit_events`` collection on the record side
            via ``back_populates="audit_events"``. String-based
            ``primaryjoin`` and ``foreign_keys`` arguments avoid
            circular imports.
    """

    __tablename__ = "audit_events"

    # The composite index supports the F-011 per-record edit-history
    # query: ``WHERE target_record_id = :id ORDER BY event_timestamp
    # DESC``. Without this index, history queries at scale (10K records
    # per organization, multiple events per record) would force a full
    # table scan plus an in-memory sort, blowing past the documented
    # detail-page response budget. The single-column indexes on
    # ``actor_user_id`` and ``event_type`` are declared via
    # ``index=True`` on those ``mapped_column`` calls below; their
    # names follow the project naming convention
    # (``ix_%(column_0_label)s``) defined in
    # :data:`app.extensions.NAMING_CONVENTION`, producing
    # ``ix_audit_events_actor_user_id`` and
    # ``ix_audit_events_event_type`` in the migration.
    __table_args__ = (
        Index(
            "ix_audit_events_target_record_event_timestamp",
            "target_record_id",
            "event_timestamp",
        ),
    )

    # ``confirm_deleted_rows=False`` disables SQLAlchemy's row-version
    # staleness check. The default behavior, ``True``, causes
    # ``session.flush`` to compare the number of rows expected to be
    # affected by an UPDATE or DELETE statement against the number
    # actually affected, raising ``StaleDataError`` if they diverge.
    # Since this table accepts only INSERTs and the application role
    # has its UPDATE and DELETE privileges revoked, an accidental
    # session-level mutation would otherwise raise the staleness error
    # rather than the more informative ``InsufficientPrivilege`` from
    # PostgreSQL. Disabling the check ensures any errant attempt to
    # mutate audit rows surfaces as a clean database-level permission
    # error, where the diagnostic is most actionable.
    #
    # The trailing suppression silences ruff's RUF012 ("mutable class
    # attribute should be annotated with typing.ClassVar") warning.
    # Annotating with :class:`typing.ClassVar` here would conflict with
    # SQLAlchemy's :class:`DeclarativeBase` parent class, which declares
    # ``__mapper_args__`` as an instance-style attribute; mypy raises
    # "Cannot override instance variable with class variable" if we
    # add the ClassVar annotation. The dict is read-only in practice
    # (SQLAlchemy consumes it once at mapper-configure time and never
    # mutates it), so the ruff warning about shared mutable state
    # across instances does not apply.
    __mapper_args__ = {"confirm_deleted_rows": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("records.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_type: Mapped[AuditEventType] = mapped_column(
        SQLEnum(
            AuditEventType,
            name="audit_event_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    before_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    after_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # ``actor`` is the WHO of the audit event - always non-null because
    # ``actor_user_id`` is non-null. The reciprocal collection
    # ``User.audit_events`` is declared on the User model (sibling) with
    # ``back_populates="actor"``. SQLAlchemy validates the pair once
    # both classes are in the registry, during the lazy
    # ``configure_mappers()`` pass triggered by the first ORM operation.
    #
    # String-based ``primaryjoin`` and ``foreign_keys`` are required
    # because the ``User`` class is unavailable at module load time
    # (it sits in a sibling module that imports this one transitively).
    # The strings are evaluated against the SQLAlchemy class registry
    # at mapper-configure time, after every model module has been
    # imported via :mod:`app.models.__init__`.
    actor: Mapped[User] = relationship(
        back_populates="audit_events",
        primaryjoin="AuditEvent.actor_user_id == User.id",
        foreign_keys="AuditEvent.actor_user_id",
    )

    # ``target_record`` is the WHICH-RECORD of the audit event - nullable
    # because some event types (authentication, role_change, certain
    # admin_op events) have no record target. The reciprocal
    # collection ``Record.audit_events`` is declared on the Record model
    # (sibling) with ``back_populates="target_record"``. The
    # ``Mapped[Record | None]`` annotation faithfully expresses the
    # nullability that the FK constraint already enforces at the
    # database level.
    target_record: Mapped[Record | None] = relationship(
        back_populates="audit_events",
        primaryjoin="AuditEvent.target_record_id == Record.id",
        foreign_keys="AuditEvent.target_record_id",
    )

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        Includes only the primary key and the event type. Deliberately
        excludes ``before_payload`` and ``after_payload``: those JSONB
        blobs may contain personally identifiable information (PII) -
        e.g., a record snapshot embedded in an ``edit`` event's payload
        carries the connection's full name, LinkedIn URL, and
        relationship context. Audit logs and exception tracebacks must
        not leak that data.

        The structlog redaction processor configured in
        :mod:`app.observability.logging` provides a secondary defense
        by filtering keys named ``*_payload``, ``password``, ``token``,
        ``*_key``, and ``*_secret``. Tests verify that ``repr()`` of an
        AuditEvent never contains the substring ``"payload"``.
        """
        return f"<AuditEvent id={self.id} type={self.event_type.value}>"
