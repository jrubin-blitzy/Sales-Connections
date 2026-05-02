"""SQLAlchemy 2.x declarative models for tag classification (F-008).

Two models live in this module:

    Tag        Organization-scoped classification token (industry, use-case,
               geography, etc.). Unique name within an org.
    RecordTag  Many-to-many association between Record and Tag with a
               composite primary key on ``(record_id, tag_id)``.

Per AAP section 0.5.3, this module declares schema only. Business logic
(normalization, duplicate detection, filter compilation) lives in
:mod:`app.services.connections`.

Schema-level facts mirrored by ``backend/migrations/versions/0001_initial_schema.py``:

    * ``tags`` carries a ``UniqueConstraint("org_id", "name")`` named
      ``uq_tags_org_name``; the same display name may be reused across
      organizations once multi-tenant runtime is enabled, but is unique
      within a single organization.
    * ``tags.org_id`` foreign key uses ``ondelete=RESTRICT`` because
      organization deletion is out of scope for MVP (per AAP section
      0.6.2 the runtime is single-organization). RESTRICT is the
      conservative default; a future multi-tenant flow that needs to
      delete an organization will explicitly cascade through tags via
      a service-layer transaction rather than an FK action.
    * ``record_tags`` is a pure association table: composite primary
      key on ``(record_id, tag_id)`` enforces deduplication of the
      pair, and both foreign keys use ``ondelete=CASCADE`` because
      hard-deleting a record (admin only, F-007) or deleting a tag
      (admin housekeeping) should remove the association rows
      automatically; tag associations have no value beyond their
      parent record/tag.
    * Soft-deleted records (the common F-007 "delete" path) are NOT
      hard-deleted, so their association rows are retained; the F-004
      feed query filters out soft-deleted records via
      ``WHERE deleted_at IS NULL`` rather than relying on FK cascades.

This module is part of the Layer 5 implementation per AAP section
0.5.1 (F-008 Tagging & Categorization).
"""

from __future__ import annotations

# ``datetime`` and ``Mapped`` MUST be importable at module load time -
# SQLAlchemy 2.x's declarative system parses ``Mapped[...]`` annotations
# at class-construction time via ``de_stringify_annotation`` (see
# :mod:`sqlalchemy.util.typing`), which does ``eval()`` on the
# annotation string and looks up names in the originating module's
# ``__globals__``. Moving either name into a ``TYPE_CHECKING`` block
# would therefore raise ``MappedAnnotationError`` at import. The
# ``noqa`` suppressions below are necessary, not stylistic.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship  # noqa: TC002

from app.extensions import Base

if TYPE_CHECKING:
    # Sibling-model imports gated to type-check time only. SQLAlchemy
    # declarative classes that reference each other via
    # ``relationship(back_populates=...)`` create a circular-import
    # graph at module load time if these were unconditional imports.
    # The ``Mapped["Organization"]`` and ``Mapped["Record"]``
    # annotations are forward references that SQLAlchemy resolves
    # lazily during the class registry's ``configure_mappers()`` pass
    # once both sides of every ``back_populates`` pair are loaded.
    from app.models.organization import Organization
    from app.models.record import Record


__all__ = ["RecordTag", "Tag"]


class Tag(Base):
    """An organization-scoped tag.

    Tags are user-supplied classification tokens applied to records to
    enable filter dimensions like industry, use-case, and geography
    (per the user's prompt: "tags for industry, use case, geography").

    Constraints:
        * ``(org_id, name)`` is unique - a tag's display name is unique
          within its organization, but the same name may be reused
          across organizations once the runtime is multi-tenant.
        * Tag deletion cascades to ``record_tags`` via the
          ``ondelete=CASCADE`` foreign key in :class:`RecordTag`; the
          parent-side ORM cascade ``"all, delete-orphan"`` mirrors
          this so SQLAlchemy does not raise an orphan warning during
          ``session.flush``.

    Attributes:
        id: Primary key (UUID v4 generated client-side via the
            ``default=uuid.uuid4`` factory when not supplied).
        org_id: Foreign key to ``organizations.id``. Non-null;
            ``RESTRICT`` on delete because organization deletion is
            out of scope for MVP (AAP section 0.6.2). Indexed for
            org-scoped tag listing (``GET /api/tags``).
        name: Tag display name (max 64 characters). The 64-character
            cap is long enough for compound classification tokens
            like ``"saas-fintech-mid-market"`` and short enough to
            keep the unique-index B-tree compact.
        created_at: Server-assigned timestamp at insertion. The
            ``server_default=func.now()`` directive emits ``NOW()``
            on INSERT, ensuring the timestamp originates in
            PostgreSQL (the canonical clock) rather than in the
            Python process.
        organization: Many-to-one relationship to :class:`Organization`.
            Bound via the ``organization.tags`` collection on the
            organization side using ``back_populates``.
        record_tags: One-to-many relationship to :class:`RecordTag`
            association rows. ``cascade="all, delete-orphan"`` ensures
            that detaching a :class:`RecordTag` from this collection
            via Python (e.g., ``tag.record_tags.remove(rt)``) marks
            the row for deletion at flush time.
    """

    __tablename__ = "tags"

    # The ``UniqueConstraint`` name matches the project's naming
    # convention from :data:`app.extensions.NAMING_CONVENTION` and is
    # explicitly stated so that Alembic autogenerate produces the same
    # constraint name in every environment.
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_tags_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # The ``Organization.tags`` reciprocal relationship is declared on
    # the Organization model (sibling) with
    # ``back_populates="organization"``. SQLAlchemy validates the pair
    # once both classes are in the registry.
    organization: Mapped[Organization] = relationship(back_populates="tags")

    # The ``RecordTag.tag`` reciprocal relationship is declared on the
    # association class with ``back_populates="tag"``. The
    # ``cascade="all, delete-orphan"`` directive ensures that
    # disassociating a ``RecordTag`` from this collection in Python
    # marks it for deletion at flush time, complementing the
    # database-level ``ondelete=CASCADE`` on the foreign key.
    #
    # ``RecordTag`` is a forward reference because the class is defined
    # later in this module. ``from __future__ import annotations`` turns
    # the entire annotation into a string at module-execution time, so
    # SQLAlchemy resolves the reference lazily during the registry's
    # ``configure_mappers()`` pass (which runs the first time any class
    # in the registry is queried). No explicit quoting is required.
    record_tags: Mapped[list[RecordTag]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        The format intentionally uses ``!r`` on the ``name`` field so
        that quote characters in the display string are visible (e.g.,
        ``<Tag id=... name='saas'>``); aiding test failure diagnostics
        without coupling consumers to the exact format string.
        """
        return f"<Tag id={self.id} name={self.name!r}>"


class RecordTag(Base):
    """Many-to-many association between :class:`Record` and :class:`Tag`.

    The composite primary key ``(record_id, tag_id)`` acts as both the
    row identifier and the deduplication constraint - a (record, tag)
    pair can appear at most once. Attempting to insert a duplicate pair
    raises ``IntegrityError`` from PostgreSQL.

    Why a model rather than a bare ``sqlalchemy.Table``:
        * ``Mapped[...]`` typed annotations require a class.
        * Future evolution: if RecordTag ever needs additional columns
          (e.g., a ``tagged_at`` timestamp or ``tagged_by`` user_id), a
          class is the natural extension point. A bare ``Table`` would
          force a schema migration to a class at that time, which is
          more disruptive than starting with a class today.
        * Trade-off: slightly more boilerplate now for cleaner
          extensibility later.

    Attributes:
        record_id: Foreign key to ``records.id``. Part of the composite
            primary key. ``ondelete=CASCADE`` removes association rows
            when an admin hard-deletes a record (F-007).
        tag_id: Foreign key to ``tags.id``. Part of the composite
            primary key. ``ondelete=CASCADE`` removes association rows
            when an admin removes a tag during housekeeping.
        record: Many-to-one relationship to :class:`Record`, paired
            with the ``record.record_tags`` collection on the record
            side via ``back_populates``.
        tag: Many-to-one relationship to :class:`Tag`, paired with the
            ``tag.record_tags`` collection on the tag side via
            ``back_populates``.
    """

    __tablename__ = "record_tags"

    # No ``__table_args__`` is needed: the composite primary key is
    # declared by setting ``primary_key=True`` on both columns, and
    # SQLAlchemy emits the canonical ``pk_record_tags`` constraint
    # name automatically courtesy of the project's
    # :data:`app.extensions.NAMING_CONVENTION`.

    record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("records.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Reciprocal sides of these relationships:
    #   * Record.record_tags (declared in app.models.record) uses
    #     ``back_populates="record"`` and
    #     ``cascade="all, delete-orphan"`` to mirror the FK CASCADE.
    #   * Tag.record_tags (declared above) uses
    #     ``back_populates="tag"`` and
    #     ``cascade="all, delete-orphan"``.
    record: Mapped[Record] = relationship(back_populates="record_tags")
    tag: Mapped[Tag] = relationship(back_populates="record_tags")

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        Includes both UUIDs of the composite primary key so that test
        failures and log statements can immediately identify which
        association row is being inspected.
        """
        return f"<RecordTag record_id={self.record_id} tag_id={self.tag_id}>"
