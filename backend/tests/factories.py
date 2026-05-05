"""Factory-Boy factories for the Sales-Connections SQLAlchemy models.

Every factory is rooted in :class:`factory.alchemy.SQLAlchemyModelFactory`
so calling, e.g., ``UserFactory()`` creates AND persists a row in the
bound SQLAlchemy session. The session is bound at fixture setup time
by ``conftest.py``'s ``_bind_factories_session`` autouse fixture, which
walks the public factory classes and assigns ``Factory._meta.sqlalchemy_session``
to the test-scoped session obtained from :data:`app.extensions.db`. The
factory module deliberately leaves the session unbound at import time
(``sqlalchemy_session = None``) so that importing this module is
side-effect-free and does not require a live database.

Architectural invariants enforced by these factories
----------------------------------------------------
- Org-scoping (AAP Section 0.7.1 invariant 3): Every entity factory
  that needs an organization sources its ``org_id`` from
  :class:`OrganizationFactory` via a :class:`factory.SubFactory`
  reference. The local ``org`` declaration is excluded from the model
  constructor kwargs via ``Meta.exclude = ("org",)`` because
  :class:`app.models.User` and :class:`app.models.Tag` use the
  ``organization`` relationship name, not ``org`` -- the factory
  helper attribute exists to compute ``org_id`` consistently and is
  never passed to the model.
- Owner attribution (AAP Section 0.7.1 invariant 7): :class:`RecordFactory`
  derives ``owner_user_id`` and ``owner_display_name`` from a single
  :class:`UserFactory` instance to mirror the F-006 contract in which
  the owner identity is server-derived (never client-supplied).
- Soft-delete semantics (AAP Section 0.7.1 invariant 4): :class:`RecordFactory`
  defaults ``deleted_at=None``. Tests opt into soft-deleted records
  via the dedicated :class:`SoftDeletedRecordFactory` subclass so
  pytest parametrization remains explicit.
- Append-only audit (AAP Section 0.7.1 invariant 5): :class:`AuditEventFactory`
  is provided for direct construction in unit tests, but most
  service-layer integration tests rely on the parent operation to
  emit the audit row inside its transaction (per AAP Section 0.5.3
  "Service functions own transactions"). Constructing audit rows
  directly is a tool of last resort.
- LinkedIn URL normalization (F-010): :class:`RecordFactory` derives
  ``normalized_linkedin_url`` from the random ``linkedin_url`` using
  :func:`app.utils.url.normalize_linkedin_url`, exactly mirroring
  the production code path so duplicate-detection tests exercise
  the same canonicalization that the API layer applies.

Public API
----------
- :class:`OrganizationFactory`        Creates an Organization.
- :class:`UserFactory`                Creates a User in an Organization
                                      with Contributor role by default.
- :class:`AdminUserFactory`           UserFactory with role=Admin.
- :class:`ContributorUserFactory`     Explicit Contributor (alias).
- :class:`ViewerUserFactory`          UserFactory with role=Viewer (Sales Rep).
- :class:`OAuthUserFactory`           UserFactory with password_hash=None
                                      (OAuth-only user; no password set).
- :class:`TagFactory`                 Creates a Tag in an Organization.
- :class:`RecordFactory`              Creates a non-soft-deleted Record.
- :class:`SoftDeletedRecordFactory`   RecordFactory with deleted_at set.
- :class:`RecordTagFactory`           Creates a record-tag association.
- :class:`AuditEventFactory`          Creates an audit_events row directly.

Convention
----------
Every factory's Meta class binds ``sqlalchemy_session_persistence = "commit"``
so the resulting object is committed to the bound session by the time
the factory call returns. The session itself is supplied by the
``_bind_factories_session`` fixture in ``conftest.py``. Both
:class:`UserFactory` and :class:`TagFactory` declare
``exclude = ("org",)`` because the User and Tag models name their
relationship column ``organization`` (not ``org``); the factory's
``org`` attribute is a private helper that drives the
:class:`factory.SelfAttribute` path for ``org_id`` and is filtered
out of the model constructor kwargs by factory-boy.

This module deliberately has no module-level side effects beyond the
class declarations and the lazy password-hash cache. Importing
``tests.factories`` does not log, does not make HTTP calls, does not
touch the filesystem, and does not connect to any database.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import factory
from factory.alchemy import SQLAlchemyModelFactory
from factory.fuzzy import FuzzyChoice

from app.models import (
    AuditEvent,
    Organization,
    Record,
    RecordTag,
    Tag,
    User,
)
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from app.utils.url import normalize_linkedin_url

__all__ = [
    "AdminUserFactory",
    "AuditEventFactory",
    "ContributorUserFactory",
    "OAuthUserFactory",
    "OrganizationFactory",
    "RecordFactory",
    "RecordTagFactory",
    "SoftDeletedRecordFactory",
    "TagFactory",
    "UserFactory",
    "ViewerUserFactory",
]


# ---------------------------------------------------------------------------
# Module-level helpers and constants
# ---------------------------------------------------------------------------
# The default test password is hashed once on first access using bcrypt
# cost=4 (matching :class:`app.config.TestingConfig` BCRYPT_COST). The
# hash is cached so subsequent factory calls do not re-hash. Bcrypt
# cost-4 is approximately 1ms per hash; cost-12 (production) would be
# ~250ms per hash. Caching the hash avoids the per-test cost of
# repeatedly computing it for every UserFactory instantiation.
#
# Note: The hash bytes returned by :func:`bcrypt.hashpw` are decoded to
# UTF-8 because :attr:`User.password_hash` is a SQLAlchemy
# :class:`String` column (not :class:`LargeBinary`). The bcrypt output
# is ASCII-only by construction, so UTF-8 decoding is lossless.

_DEFAULT_TEST_PASSWORD: str = "password123!"
_cached_default_password_hash: str | None = None


def _default_password_hash() -> str:
    """Return a cached bcrypt hash of the default test password.

    Lazily computes the hash on first access using bcrypt cost=4 (matching
    :class:`app.config.TestingConfig.BCRYPT_COST`) so import-time work
    is zero. Subsequent calls return the cached value without re-hashing.

    Returns:
        A UTF-8 string representation of the bcrypt hash. The bcrypt
        algorithm produces ASCII-only output, so encoding the bytes
        as UTF-8 is lossless and safe for storage in the SQLAlchemy
        :class:`String(255)` column on :attr:`User.password_hash`.
    """
    global _cached_default_password_hash  # noqa: PLW0603 -- intentional cache
    cached = _cached_default_password_hash
    if cached is None:
        # Local import keeps bcrypt out of the module-import path so
        # tests that do not exercise password authentication never pay
        # the bcrypt-import cost.
        import bcrypt  # noqa: PLC0415 -- intentional lazy import

        cached = bcrypt.hashpw(
            _DEFAULT_TEST_PASSWORD.encode("utf-8"),
            bcrypt.gensalt(rounds=4),
        ).decode("utf-8")
        _cached_default_password_hash = cached
    return cached


# ---------------------------------------------------------------------------
# OrganizationFactory
# ---------------------------------------------------------------------------


class OrganizationFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.Organization`.

    Generates a sequenced organization name to guarantee uniqueness within
    a single test run (the ``organizations`` table has no UNIQUE constraint
    on ``name`` in MVP, but using a sequence keeps test output deterministic
    and avoids accidental name collisions across factories).

    Attributes:
        id: A fresh UUID4 generated on each invocation via
            :func:`factory.LazyFunction`.
        name: A sequenced display name of the form
            ``"Test Organization {n}"`` where ``n`` is the per-factory
            integer counter that factory-boy increments on every call.
        created_at: A timezone-aware UTC :class:`datetime` captured at
            factory-call time. Overrides the model's ``server_default``
            so tests can freeze time deterministically with
            :mod:`freezegun` if needed.
    """

    class Meta:
        model = Organization
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest

    id = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"Test Organization {n}")
    created_at = factory.LazyFunction(lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# UserFactory and role-specific subclasses
# ---------------------------------------------------------------------------


class UserFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.User` (Contributor role by default).

    The default role is :data:`UserRole.CONTRIBUTOR` because the most
    common test scenario is a user authoring connection records. For
    other role variants use the dedicated subclasses:

        :class:`AdminUserFactory`        role=Admin
        :class:`ContributorUserFactory`  role=Contributor (explicit alias)
        :class:`ViewerUserFactory`       role=Viewer (Sales Rep)
        :class:`OAuthUserFactory`        password_hash=None (OAuth-only)

    Org-scoping: every user belongs to an Organization. The factory's
    ``org`` declaration is a :class:`factory.SubFactory` reference to
    :class:`OrganizationFactory`; ``org_id`` is derived from the
    just-created organization via :class:`factory.SelfAttribute`. Tests
    that need many users in the SAME organization should pass
    ``org=existing_org`` to subsequent :class:`UserFactory` calls.

    The ``Meta.exclude = ("org",)`` directive removes the helper ``org``
    attribute from the kwargs passed to :meth:`User.__init__`, because
    the User model exposes the related Organization via the
    ``organization`` relationship attribute (not ``org``). Without the
    exclusion, factory-boy would call ``User(org=<organization>, ...)``
    and SQLAlchemy would raise
    ``TypeError: 'org' is an invalid keyword argument for User``.

    Attributes:
        id: A fresh UUID4 generated on each invocation.
        org: The parent :class:`Organization` instance. Excluded from
            the User constructor kwargs via ``Meta.exclude``; used
            internally to compute ``org_id``. Tests can pass
            ``org=existing_org`` to bind the user to a specific
            organization.
        org_id: UUID extracted from ``org.id``. Always consistent with
            ``org`` because both derive from the same SubFactory call.
        email: A sequenced email address of the form
            ``"user{n}@example.com"`` to satisfy the
            ``UniqueConstraint("org_id", "email")`` even when many
            users are created in the same organization.
        display_name: A random Faker-generated person name.
        password_hash: A cached bcrypt cost-4 hash of the literal string
            ``"password123!"`` (see :data:`_DEFAULT_TEST_PASSWORD`).
            Tests that need a different password should pass
            ``password_hash=...`` explicitly.
        role: Defaults to :data:`UserRole.CONTRIBUTOR`. Overridden by
            the role-specific subclasses below.
        created_at: A timezone-aware UTC :class:`datetime` captured at
            factory-call time.
    """

    class Meta:
        model = User
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest
        # ``org`` is a factory-private helper that drives the SubFactory
        # creation of an Organization and the ``SelfAttribute("org.id")``
        # lookup for ``org_id``. The User model uses the relationship
        # name ``organization`` rather than ``org``; without this
        # exclusion, factory-boy would pass ``org=<Organization>`` to
        # the User constructor and SQLAlchemy would reject the kwarg
        # with ``TypeError: 'org' is an invalid keyword argument``.
        exclude = ("org",)

    id = factory.LazyFunction(uuid.uuid4)
    org = factory.SubFactory(OrganizationFactory)
    org_id = factory.SelfAttribute("org.id")
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    display_name = factory.Faker("name")
    password_hash = factory.LazyFunction(_default_password_hash)
    role = UserRole.CONTRIBUTOR
    created_at = factory.LazyFunction(lambda: datetime.now(UTC))


class AdminUserFactory(UserFactory):
    """:class:`UserFactory` variant with role=:data:`UserRole.ADMIN`.

    Used for tests that exercise admin-only API paths
    (``/api/admin/*``, hard delete, role mutation). The ``Meta`` is
    inherited from :class:`UserFactory` so the ``exclude = ("org",)``
    rule and the ``sqlalchemy_session_persistence = "commit"`` setting
    propagate automatically.
    """

    role = UserRole.ADMIN


class ContributorUserFactory(UserFactory):
    """Explicit alias for :class:`UserFactory` with role=Contributor.

    Provided for tests that prefer the explicit class name to make
    the role assignment visible at the call site (e.g.,
    ``ContributorUserFactory()`` vs ``UserFactory()``).
    """

    role = UserRole.CONTRIBUTOR


class ViewerUserFactory(UserFactory):
    """:class:`UserFactory` variant with role=:data:`UserRole.VIEWER`.

    The ``Viewer`` role corresponds to the "Sales Rep" user type per
    AAP Section 0.5.2 (Layer 1). Sales Reps can browse all records in
    the organization and mutate the ``outreach_status`` field on any
    record, but cannot edit other users' record fields and cannot
    access the admin panel.
    """

    role = UserRole.VIEWER


class OAuthUserFactory(UserFactory):
    """:class:`UserFactory` variant representing an OAuth-only user.

    Sets ``password_hash=None`` so the resulting User row has NULL in
    the ``password_hash`` column, exactly as the production OAuth
    upsert path produces. Used to verify:

        * Email/password login MUST fail for OAuth-only users (the
          auth service raises ``AuthError`` when ``password_hash`` is
          NULL per AAP Section 0.5.2 Layer 1).
        * ``/api/me``, RBAC, and other authenticated paths work the
          same for OAuth users as for password-authenticated users.
    """

    # mypy: factory-boy class attributes are processed by the FactoryMetaClass
    # at class-definition time and become declarations rather than ordinary
    # attributes. Overriding the parent class's ``LazyFunction`` declaration
    # with a literal ``None`` is the correct factory-boy idiom for "do not
    # populate this column"; the static type system cannot model this.
    password_hash = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# TagFactory
# ---------------------------------------------------------------------------


class TagFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.Tag`.

    Tag names are generated via :class:`factory.Sequence` so each call
    produces a unique ``name`` within the same organization, satisfying
    the ``UniqueConstraint("org_id", "name")`` declared on the
    :class:`app.models.Tag` table. The sequence-based pattern is
    preferred over Faker-based randomness because tag names typically
    appear in test assertions and deterministic naming aids
    debuggability.

    The ``Meta.exclude = ("org",)`` directive removes the helper ``org``
    attribute from the kwargs passed to :meth:`Tag.__init__`, because
    the Tag model exposes the related Organization via the
    ``organization`` relationship attribute (not ``org``). Without the
    exclusion, factory-boy would call ``Tag(org=<organization>, ...)``
    and SQLAlchemy would raise
    ``TypeError: 'org' is an invalid keyword argument for Tag``.

    Attributes:
        id: A fresh UUID4 generated on each invocation.
        org: The parent :class:`Organization` instance. Excluded from
            the Tag constructor kwargs via ``Meta.exclude``; used
            internally to compute ``org_id``.
        org_id: UUID extracted from ``org.id``.
        name: A sequenced tag display name of the form ``"tag-{n}"``
            so each new tag is unique within an organization.
        created_at: A timezone-aware UTC :class:`datetime` captured
            at factory-call time.
    """

    class Meta:
        model = Tag
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest
        exclude = ("org",)  # see UserFactory.Meta.exclude rationale

    id = factory.LazyFunction(uuid.uuid4)
    org = factory.SubFactory(OrganizationFactory)
    org_id = factory.SelfAttribute("org.id")
    name = factory.Sequence(lambda n: f"tag-{n}")
    created_at = factory.LazyFunction(lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# RecordFactory and SoftDeletedRecordFactory
# ---------------------------------------------------------------------------


class RecordFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.Record` (non-soft-deleted by default).

    Generates all nine F-001 business fields plus the architectural
    fields (``org_id``, ``owner_user_id``, ``owner_display_name``,
    ``normalized_linkedin_url``, ``deleted_at``, ``created_at``,
    ``updated_at``).

    Owner attribution (AAP F-006): ``owner_user_id`` and
    ``owner_display_name`` are sourced from the same
    :class:`UserFactory` instance via :class:`factory.SelfAttribute` so
    a record's owner identity is consistent across both columns. Tests
    that need a specific owner pass ``owner=existing_user``.

    Org-scoping: ``org_id`` matches ``owner.org_id`` by default so the
    record always sits in the same organization as its owner. Tests
    that need to bind a record to a specific org should pass
    ``owner=existing_user_in_that_org``; the record's ``org_id`` will
    follow the owner.

    LinkedIn URL normalization (AAP F-010):
    ``normalized_linkedin_url`` is derived from ``linkedin_url`` at
    construction time using
    :func:`app.utils.url.normalize_linkedin_url`, exactly mirroring the
    production code path. Tests that need a specific normalized form
    can pass ``normalized_linkedin_url=...`` explicitly.

    Soft-delete: ``deleted_at`` defaults to ``None``. Use
    :class:`SoftDeletedRecordFactory` for soft-deleted records so the
    test's intent is visible at the call site.

    Attributes:
        id: A fresh UUID4 generated on each invocation.
        owner: The parent :class:`User` instance. Set as both the
            relationship attribute and (transitively via
            ``owner_user_id``) the FK column. Tests can pass
            ``owner=existing_user`` to bind the record to a specific
            user.
        owner_user_id: UUID extracted from ``owner.id``. Always
            consistent with ``owner``.
        owner_display_name: Denormalized snapshot of
            ``owner.display_name`` at record-creation time. Mirrors
            the F-006 invariant that owner identity is captured
            permanently on the record.
        org_id: UUID extracted from ``owner.org_id`` so the record is
            org-scoped to the same organization as its owner.
        full_name: Required business field; a random Faker-generated
            person name.
        linkedin_url: Required business field; a sequenced LinkedIn
            profile URL of the form
            ``"https://www.linkedin.com/in/test-user-{n}"``. The
            sequenced form (rather than Faker's ``user_name``) keeps
            URL generation reproducible and avoids the corner case in
            which Faker emits a slug character that the LinkedIn URL
            validator rejects.
        normalized_linkedin_url: The output of
            :func:`app.utils.url.normalize_linkedin_url` applied to
            ``linkedin_url``. Mirrors the production server-side
            normalization step that powers F-010 duplicate detection.
        company: Required business field; a random Faker-generated
            company name.
        job_title: Required business field; a random Faker-generated
            job title.
        relationship_context: Required business field; a random
            three-sentence Faker-generated paragraph capturing how
            the contributor knows the connection.
        ai_notes: Defaults to ``None``. AI notes are typically
            populated by the F-002 service path; tests that need a
            non-null value should pass ``ai_notes="..."`` explicitly.
        involvement: A random :class:`InvolvementType` enum member
            chosen by :class:`factory.fuzzy.FuzzyChoice` from the
            three valid values (Warm Intro / Soft Reference /
            Target Only).
        outreach_status: Defaults to
            :data:`OutreachStatus.NOT_STARTED`, matching the database
            default per F-005.
        submission_date: A timezone-aware UTC :class:`datetime`
            captured at factory-call time.
        deleted_at: Defaults to ``None`` (active record). Subclasses
            override this to populate the soft-delete timestamp.
        created_at: Mirrors ``submission_date`` so all three
            timestamps agree by default. Tests that need divergent
            values pass them explicitly.
        updated_at: Mirrors ``submission_date`` for the same reason.
    """

    class Meta:
        model = Record
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest

    id = factory.LazyFunction(uuid.uuid4)
    owner = factory.SubFactory(UserFactory)
    owner_user_id = factory.SelfAttribute("owner.id")
    owner_display_name = factory.SelfAttribute("owner.display_name")
    org_id = factory.SelfAttribute("owner.org_id")

    # ------------------------------------------------------------------
    # Nine business fields (F-001)
    # ------------------------------------------------------------------
    full_name = factory.Faker("name")
    # Sequenced LinkedIn URL keeps each record's ``linkedin_url`` unique
    # within the org, satisfying the
    # ``uq_records_org_normalized_linkedin_url_active`` partial unique
    # index (F-010). The slug ``test-user-{n}`` is composed only of
    # URL-safe characters (ASCII alphanumerics and hyphen), passing the
    # ``_SLUG_SEGMENT_RE`` validation in :mod:`app.utils.url`.
    linkedin_url = factory.Sequence(lambda n: f"https://www.linkedin.com/in/test-user-{n}")
    # Derive the normalized URL using the production helper so tests
    # exercise the exact canonicalization that the API layer applies.
    # This mirrors the F-010 server-side normalization invariant.
    normalized_linkedin_url = factory.LazyAttribute(
        lambda obj: normalize_linkedin_url(obj.linkedin_url)
    )
    company = factory.Faker("company")
    job_title = factory.Faker("job")
    relationship_context = factory.Faker("paragraph", nb_sentences=3)
    # AI notes are optional; set to None by default. Tests that need a
    # specific value pass ``ai_notes="..."``.
    ai_notes = None
    # Random involvement to spread coverage across the three values.
    involvement = FuzzyChoice(list(InvolvementType))
    # Default per F-005: every record starts at "Not Started".
    outreach_status = OutreachStatus.NOT_STARTED
    submission_date = factory.LazyFunction(lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Operational columns
    # ------------------------------------------------------------------
    # Soft-delete sentinel; None means active record.
    deleted_at = None
    # Created/updated mirror submission_date by default so all three
    # timestamps agree on a freshly-created factory instance.
    created_at = factory.LazyAttribute(lambda obj: obj.submission_date)
    updated_at = factory.LazyAttribute(lambda obj: obj.submission_date)


class SoftDeletedRecordFactory(RecordFactory):
    """:class:`RecordFactory` variant with ``deleted_at`` populated.

    Used to test soft-delete-aware queries: the F-004 feed query
    excludes these rows by default (``WHERE deleted_at IS NULL``),
    while admin moderation views explicitly opt out of the filter to
    include them.

    The ``deleted_at`` value is a fresh UTC timestamp at factory-call
    time. Tests that need a specific deletion timestamp can pass
    ``deleted_at=...`` explicitly.
    """

    class Meta:
        # Re-declare the Meta options explicitly here rather than
        # inheriting via ``class Meta(RecordFactory.Meta)``. Factory-boy
        # strips the ``Meta`` class from the parent at class-creation
        # time (it lives on as ``RecordFactory._meta``, which is a
        # different shape), so direct inheritance via
        # ``class Meta(RecordFactory.Meta)`` raises
        # ``AttributeError: 'RecordFactory' has no attribute 'Meta'``.
        # Re-declaring the same options keeps the persistence and
        # session bindings obvious at the subclass definition site and
        # makes them visible to grep-based validation tooling.
        model = Record
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest

    # mypy: factory-boy class attributes are processed by the FactoryMetaClass
    # at class-definition time and become declarations rather than ordinary
    # attributes. Overriding the parent class's literal ``None`` with a
    # ``LazyFunction`` declaration is the correct factory-boy idiom for
    # "always populate this column"; the static type system cannot model this.
    deleted_at = factory.LazyFunction(lambda: datetime.now(UTC))  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# RecordTagFactory
# ---------------------------------------------------------------------------


class RecordTagFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.RecordTag` (record-tag association).

    The composite primary key is ``(record_id, tag_id)``. Both must
    reference existing rows; the factory creates a fresh
    :class:`Record` and :class:`Tag` via :class:`factory.SubFactory`
    when callers do not pass explicit instances. Tests that want to
    associate an existing record with an existing tag should pass
    ``record=existing_record, tag=existing_tag`` to override the
    SubFactory defaults.

    Org consistency: a Record and a Tag attached via :class:`RecordTag`
    must belong to the same organization for the F-008 filter queries
    to behave correctly. The
    :meth:`_ensure_tag_org_consistency` post-generation hook fixes up
    the auto-created :class:`Tag` instance when its ``org_id`` would
    otherwise differ from the Record's, so the resulting association
    row is always intra-org.

    Attributes:
        record: The parent :class:`Record` instance, created via
            :class:`RecordFactory` SubFactory by default.
        record_id: UUID extracted from ``record.id``.
        tag: The parent :class:`Tag` instance, created via
            :class:`TagFactory` SubFactory by default. Mutated by
            :meth:`_ensure_tag_org_consistency` to share the
            record's organization.
        tag_id: UUID extracted from ``tag.id``.
    """

    class Meta:
        model = RecordTag
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest

    record = factory.SubFactory(RecordFactory)
    record_id = factory.SelfAttribute("record.id")
    # Tag is created via SubFactory; the post-generation hook below
    # rewrites tag.org_id to match record.org_id when they would
    # otherwise diverge. This keeps the Record and Tag in the same
    # organization, satisfying the multi-tenant invariant (AAP
    # Section 0.7.1 invariant 3) without requiring callers to manually
    # synchronize the two SubFactory chains.
    tag = factory.SubFactory(TagFactory)
    tag_id = factory.SelfAttribute("tag.id")

    @factory.post_generation
    def _ensure_tag_org_consistency(
        self: RecordTag,
        create: bool,
        extracted: object,
        **kwargs: object,
    ) -> None:
        """Synchronize ``tag.org_id`` with ``record.org_id``.

        When the factory auto-creates both ``record`` and ``tag`` via
        their respective SubFactories, each call produces a fresh
        :class:`Organization`. The resulting Record and Tag belong to
        DIFFERENT organizations -- a state that the multi-tenant
        invariant forbids.

        This hook detects the divergence and rewrites ``tag.org_id``
        (and the back-populated ``tag.organization`` relationship if
        it is loaded) to match ``record.org_id`` so the association
        row is always intra-organization.

        The hook only runs when factory-boy's ``create`` flag is set
        (i.e., when the factory is invoked via ``RecordTagFactory()``
        rather than ``RecordTagFactory.build()``). The ``extracted``
        and ``kwargs`` parameters are unused but accepted to match the
        :func:`factory.post_generation` decorator's expected signature.

        Note: ``self`` here refers to the freshly-constructed
        :class:`RecordTag` association instance -- factory-boy passes
        the new object as the first positional argument to every
        post-generation hook. The naming convention satisfies ruff's
        N805 rule for instance methods.
        """
        # Arguments exist to match factory-boy's expected signature; the
        # decorator passes any post-generation kwargs through as
        # ``extracted`` and additional ``**kwargs``.
        del extracted, kwargs

        if not create:
            return
        # ``self.tag`` and ``self.record`` are guaranteed non-None here:
        # both are bound to SubFactory results (or caller-supplied
        # instances) before the post-generation hook runs. The model's
        # ``Mapped[Tag]`` / ``Mapped[Record]`` annotations explicitly
        # reflect this invariant. No further None check is needed.
        if self.tag.org_id != self.record.org_id:
            # Rewrite the tag's org_id to match the record's. The
            # ``organization`` relationship attribute is intentionally
            # NOT updated because it would trigger a lazy DB load; the
            # ``org_id`` FK is the authoritative source of org-scope
            # truth, and any subsequent access to ``self.tag.organization``
            # will lazy-load the correct Organization based on the
            # rewritten FK.
            self.tag.org_id = self.record.org_id


# ---------------------------------------------------------------------------
# AuditEventFactory
# ---------------------------------------------------------------------------


class AuditEventFactory(SQLAlchemyModelFactory):
    """Factory for :class:`app.models.AuditEvent`.

    AuditEvent rows are normally emitted as a SIDE EFFECT of state-
    changing service-layer operations (per the atomic state-change +
    audit pair invariant in AAP Section 0.7.1). This factory exists
    for the rare unit test that needs to construct audit rows
    directly, e.g., to validate the append-only privilege grants at
    the database layer or to seed history rows for the F-011 history
    feed.

    The default ``event_type`` is :data:`AuditEventType.CREATE`
    because that is the most common audit event in the system. Tests
    that need a specific event type should pass
    ``event_type=AuditEventType.STATUS_CHANGE`` (or another member)
    explicitly.

    Actor + target consistency: the ``target_record`` SubFactory uses
    :class:`factory.SelfAttribute` with the ``..actor`` parent path
    so the auto-created Record's owner is the SAME User as the
    AuditEvent's actor. This mirrors the realistic case in which the
    actor performing the operation owns the target record. Tests that
    want a different actor/target relationship should override
    ``target_record`` or ``actor`` explicitly.

    Note: the :class:`AuditEvent` model does NOT carry an ``org_id``
    column. Org-scope on audit events is derived transitively via the
    ``actor`` (User.org_id) and ``target_record`` (Record.org_id)
    relationships. This factory therefore does not declare ``org_id``
    even though the schema spec references it; declaring an ``org_id``
    that the model does not accept would raise
    ``TypeError: 'org_id' is an invalid keyword argument for AuditEvent``
    at construction time.

    Attributes:
        id: A fresh UUID4 generated on each invocation.
        actor: The :class:`User` who performed the action, created
            via :class:`UserFactory` SubFactory by default.
        actor_user_id: UUID extracted from ``actor.id``.
        target_record: The :class:`Record` the action targeted,
            created via :class:`RecordFactory` SubFactory by default
            with the same owner as the actor.
        target_record_id: UUID extracted from ``target_record.id``.
        event_type: Defaults to :data:`AuditEventType.CREATE`. Tests
            that need other event types pass them explicitly.
        event_timestamp: A timezone-aware UTC :class:`datetime`
            captured at factory-call time.
        before_payload: Defaults to ``None`` (no prior state for
            CREATE events).
        after_payload: A small JSONB-compatible dict marker so the
            after-state column is non-null for inspectability.
    """

    class Meta:
        model = AuditEvent
        sqlalchemy_session_persistence = "commit"
        sqlalchemy_session = None  # bound at fixture setup time by conftest

    id = factory.LazyFunction(uuid.uuid4)
    actor = factory.SubFactory(UserFactory)
    actor_user_id = factory.SelfAttribute("actor.id")
    # The target_record's owner is bound to the same User as the
    # AuditEvent's actor via the ``..actor`` SelfAttribute path. This
    # mirrors the realistic scenario in which the user performing the
    # operation owns the record under audit. The ``..`` prefix tells
    # factory-boy to look up ``actor`` on the parent factory
    # (AuditEventFactory itself) rather than on the SubFactory's own
    # stub.
    target_record = factory.SubFactory(
        RecordFactory,
        owner=factory.SelfAttribute("..actor"),
    )
    target_record_id = factory.SelfAttribute("target_record.id")
    event_type = AuditEventType.CREATE
    event_timestamp = factory.LazyFunction(lambda: datetime.now(UTC))
    # No prior state for CREATE events.
    before_payload = None
    # Marker payload so the column is inspectable in test assertions
    # without forcing every test to specify it. Tests that need a
    # specific payload pass ``after_payload={"...": "..."}`` explicitly.
    after_payload = factory.LazyFunction(lambda: {"created_via": "factory"})
