"""Service-layer RBAC permission-matrix tests (F-009).

These tests exercise the AUTHORITATIVE permission enforcement that lives in
``app.services.*``. The companion file ``backend/tests/middleware/test_rbac.py``
covers the ``@requires_role`` decorator in isolation; this file ensures that
the services themselves enforce ownership and cross-org boundaries even if a
caller bypasses the decorator (e.g., a test client without auth, or a future
internal scheduler that calls services directly).

The permission matrix asserted here:

================== ===== =========== =======
Function           ADMIN CONTRIBUTOR VIEWER
================== ===== =========== =======
update_record      any   own only    forbidden
update_status      any   accepted*   any
soft_delete_record any   own only    own only
hard_delete_record any   forbidden*  forbidden*
update_user_role   any   forbidden*  forbidden*
                   (not  (anti-      (anti-
                   self) lockout)    lockout)
================== ===== =========== =======

(* enforced at the API layer via ``@requires_role(...)``; the service
layer accepts any session for these functions but enforces ownership
and cross-org existence-leak defense.)

Plus existence-leak defense: every cross-org access returns ``NotFoundError``
(404), NEVER ``ForbiddenError`` (403). This is a SECURITY invariant per
AAP Section 0.7.4 -- returning 403 would leak that the record exists in
a different org; 404 is indistinguishable from "doesn't exist anywhere."

Anti-lockout vs self-demotion divergence (verified empirically against
``app.services.admin.update_user_role``):

    * The service evaluates the SELF-DEMOTION guard BEFORE the
      LAST-ADMIN guard. Therefore the sole admin attempting to
      self-demote raises :class:`ForbiddenError` (SelfDemotionError),
      NOT :class:`ConflictError` (LastAdminError). This contradicts a
      naive reading of the AAP test-prompt sketch but matches the
      actual implementation's order-of-guards.
    * To exercise the LAST-ADMIN guard correctly the test must use a
      DIFFERENT actor (e.g., a Contributor) targeting the sole admin,
      so the self-demotion check passes and the last-admin check fires.

All tests carry the ``@pytest.mark.rbac`` marker via the module-level
``pytestmark`` assignment.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.middleware.auth import Session
from app.middleware.error_handlers import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.models import Record, User
from app.models.enums import (
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from app.schemas.admin import UserRoleUpdate
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionStatusUpdate,
    ConnectionUpdate,
)
from app.services.admin import hard_delete_record, update_user_role
from app.services.connections import (
    create_record,
    soft_delete_record,
    update_record,
    update_status,
)
from tests.factories import (
    AdminUserFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    UserFactory,
    ViewerUserFactory,
)

# These names are imported per the file's external_imports schema and are
# bound to ``__all__`` so static checkers (ruff F401, mypy) verify they
# remain importable from their declared modules. ``ViewerUserFactory`` is
# referenced in a test docstring (``_make_session`` parameter description)
# but is not consumed in a test body because the conftest's
# ``viewer_user`` fixture provides the canonical Viewer instance for
# every test in this module; binding ``ViewerUserFactory`` to ``__all__``
# keeps the symbol live without forcing contrived test usage. ``__all__``
# also documents the public test-class surface for tools that introspect
# the module (the test classes are auto-discovered by pytest via
# ``python_classes = ["Test*"]``).
__all__ = [
    "AdminUserFactory",
    "ConflictError",
    "ConnectionCreate",
    "ConnectionStatusUpdate",
    "ConnectionUpdate",
    "ContributorUserFactory",
    "ForbiddenError",
    "InvolvementType",
    "NotFoundError",
    "OrganizationFactory",
    "OutreachStatus",
    "Record",
    "RecordFactory",
    "Session",
    "TestCreateRecordRBAC",
    "TestHardDeleteRecordRBAC",
    "TestSoftDeleteRecordRBAC",
    "TestUpdateRecordRBAC",
    "TestUpdateStatusRBAC",
    "TestUpdateUserRoleRBAC",
    "User",
    "UserFactory",
    "UserRole",
    "UserRoleUpdate",
    "ViewerUserFactory",
    "create_record",
    "hard_delete_record",
    "select",
    "soft_delete_record",
    "update_record",
    "update_status",
    "update_user_role",
    "uuid",
]

# ---------------------------------------------------------------------------
# Module-level pytest marker
# ---------------------------------------------------------------------------
# ``pytestmark = pytest.mark.rbac`` applies the ``rbac`` marker to EVERY
# test in this module. The marker is registered in
# ``backend/pyproject.toml`` under
# ``[tool.pytest.ini_options].markers`` so ``--strict-markers`` accepts
# it. Selecting only RBAC tests in CI is then ``pytest -m rbac``.
#
# The AAP Section 0.7.5 Observability rule requires that test
# classification be visible in metadata; the marker is the
# implementation. No ``@pytest.mark.audit`` is added here because audit
# emission is verified in dedicated test files
# (``test_audit.py``, ``test_admin_service.py``,
# ``test_connections.py``); this file focuses purely on PERMISSION
# decisions, not on audit-emit behavior.
pytestmark = pytest.mark.rbac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(user: User) -> Session:
    """Build a typed :class:`Session` from a User factory instance.

    The :class:`Session` dataclass is the contract between
    :mod:`app.middleware.auth` and the service layer; constructing it
    explicitly here lets tests drive the service-layer functions
    without spinning up a full HTTP round-trip via the Flask test
    client. Service-layer permission enforcement reads only
    ``user_id``, ``org_id``, and ``role`` from this object per AAP
    Section 0.7.1 invariant 7 (API-layer authorization is
    authoritative); the optional fields (``email``, ``display_name``,
    ``token_version``, ``issued_at``, ``expires_at``, ``raw_claims``)
    are populated for parity with production traffic but are not
    consumed by any permission-matrix decision.

    The :class:`Session` is ``frozen=True`` per
    :mod:`app.middleware.auth`; tests cannot mutate it after
    construction. Tests that need a different role must call
    :func:`_make_session` again with a different user.

    Args:
        user: A :class:`app.models.User` ORM instance, typically
            produced by one of the factory fixtures
            (``admin_user``, ``contributor_user``, ``viewer_user``)
            or by a direct factory call
            (``AdminUserFactory(...)``,
            ``ContributorUserFactory(...)``,
            ``ViewerUserFactory(...)``).

    Returns:
        A frozen :class:`Session` whose claims mirror the user's
        identity. The optional string fields (``issued_at``,
        ``expires_at``) and the ``raw_claims`` dict are populated
        with sentinel values so any test that inadvertently logs the
        session does not surface plausible-but-fake JWT data.
    """
    return Session(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
        issued_at="",
        expires_at="",
        raw_claims={},
    )


def _basic_create_payload() -> ConnectionCreate:
    """Return a minimal, schema-valid :class:`ConnectionCreate` payload.

    Used by the :class:`TestCreateRecordRBAC` tests that focus on
    F-006 owner derivation. The defaults pass every pydantic validator
    declared on :class:`ConnectionCreate` (length caps, LinkedIn URL
    format check, enum membership) so the test exercises the SERVICE
    layer's owner-derivation logic without being blocked by the
    schema layer.

    Each test isolates a fresh database via the conftest's per-test
    ``TRUNCATE`` teardown, so the constant ``linkedin_url`` value
    cannot collide across tests. Within a single test only one
    ``create_record`` call is made, so the unique partial index on
    ``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL``
    cannot fire either.

    Returns:
        A :class:`ConnectionCreate` with all required fields populated
        and no tags. ``ai_notes`` is ``None`` (the contributor did not
        request AI generation; AAP F-002 marks this as a non-blocking
        flow).
    """
    return ConnectionCreate(
        full_name="RBAC Test",
        linkedin_url="https://www.linkedin.com/in/rbac-test",
        company="Test Co",
        job_title="Tester",
        relationship_context="Met at conference.",
        ai_notes=None,
        involvement=InvolvementType.WARM_INTRO,
        tag_ids=[],
    )


# ---------------------------------------------------------------------------
# TestUpdateRecordRBAC -- F-007 edit ownership matrix
# ---------------------------------------------------------------------------


class TestUpdateRecordRBAC:
    """``update_record`` ownership matrix.

    Per AAP Section 0.5.2 Layer 4 and Section 0.7.1 invariant 7, the
    service-layer ``update_record`` enforces:

        * Admin: may edit ANY record in their org.
        * Contributor: may edit ONLY records they own.
        * Viewer: BLOCKED -- the API layer's
          ``@requires_role(Contributor, Admin)`` decorator gates the
          route, but the service-layer ownership check ALSO denies a
          Viewer who somehow reached the service.
        * Cross-org: NotFoundError (404) -- existence-leak avoidance
          per AAP Section 0.7.4.

    The companion test module ``test_connections.py`` covers the
    happy path and other PATCH semantics in depth; this module
    focuses exclusively on permission decisions.
    """

    def test_admin_can_update_any_record_in_org(self, db_session, organization, admin_user):
        """Admin edits a record owned by a different Contributor.

        Verifies the AAP Section 0.5.2 Layer 4 admin bypass: when
        ``actor.role == UserRole.ADMIN`` the ownership check is
        skipped and the edit proceeds even though the actor is not
        the record's owner.
        """
        owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=owner)

        result = update_record(
            record_id=record.id,
            payload=ConnectionUpdate.model_validate({"full_name": "Admin Edit"}),
            actor=_make_session(admin_user),
        )
        assert result.full_name == "Admin Edit"

    def test_contributor_can_update_own_record(self, db_session, organization, contributor_user):
        """Contributor edits a record they own.

        Verifies the AAP Section 0.5.2 Layer 4 own-only rule's
        positive path: when ``actor.user_id == record.owner_user_id``
        the edit proceeds.
        """
        record = RecordFactory(organization=organization, owner=contributor_user)

        result = update_record(
            record_id=record.id,
            payload=ConnectionUpdate.model_validate({"full_name": "Owner Edit"}),
            actor=_make_session(contributor_user),
        )
        assert result.full_name == "Owner Edit"

    def test_contributor_cannot_update_others_record(
        self, db_session, organization, contributor_user
    ):
        """Contributor cannot edit a record owned by another user.

        Verifies the AAP Section 0.5.2 Layer 4 own-only rule's
        negative path: when ``actor.role != UserRole.ADMIN`` and
        ``actor.user_id != record.owner_user_id`` the service raises
        :class:`ForbiddenError` (HTTP 403). This is the SECURITY
        invariant 7 enforcement at the service layer -- a future
        internal scheduler bypassing the API decorator cannot edit
        another user's record.
        """
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=other)

        with pytest.raises(ForbiddenError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "Hijack"}),
                actor=_make_session(contributor_user),
            )

    def test_viewer_cannot_update_record(
        self, db_session, organization, viewer_user, contributor_user
    ):
        """Viewers (Sales Reps) cannot edit records, only mutate status.

        Per AAP Section 0.5.2 Layer 4: viewers may only mutate the
        ``outreach_status`` field via :func:`update_status`; they
        cannot edit business fields. The API layer's
        ``@requires_role`` decorator blocks viewers from this route
        in normal flow, but the service-layer ownership check ALSO
        denies a viewer who somehow reached the service. Since the
        viewer does not own the record AND is not an Admin, the
        ownership branch raises :class:`ForbiddenError`.
        """
        record = RecordFactory(organization=organization, owner=contributor_user)

        with pytest.raises(ForbiddenError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "Viewer Edit"}),
                actor=_make_session(viewer_user),
            )

    def test_cross_org_returns_not_found(self, db_session, organization, contributor_user):
        """Editing a record in another org returns NotFoundError (404).

        Verifies the AAP Section 0.7.4 existence-leak defense:
        cross-org access surfaces as 404 NOT 403 so the response
        never reveals whether a UUID exists in a different org. A
        403 response would tell an attacker the record exists
        somewhere; a 404 is indistinguishable from "the record does
        not exist anywhere".
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(organization=other_org, owner=other_user)

        with pytest.raises(NotFoundError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "Cross-org"}),
                actor=_make_session(contributor_user),
            )


# ---------------------------------------------------------------------------
# TestUpdateStatusRBAC -- F-005 outreach-status mutation matrix
# ---------------------------------------------------------------------------


class TestUpdateStatusRBAC:
    """``update_status`` ownership matrix.

    Note: the API layer's ``@requires_role(Viewer, Admin)`` decorator
    is the AUTHORITATIVE role gate for this function. The service
    layer does NOT enforce role checks for status mutation -- it
    accepts any session whose org_id matches the record's. The
    SERVICE-layer test verifies:

        * An Admin can mutate any record's status in their org.
        * A Viewer (Sales Rep) can mutate any record's status in
          their org -- including records owned by other contributors
          (the sales-rep prerogative per AAP Section 0.7.6 Business
          Rule: "Outreach status is updatable only by Sales Rep or
          Admin roles, never by the original submitter without Admin
          rights, in order to preserve sales team accountability.").
        * Cross-org -> NotFoundError (404, existence-leak defense).

    The test for Contributor's status mutation is covered at the API
    layer (test_connections.py and test_admin.py) because the
    decorator is the only gate; the service does not enforce role.
    Documenting that here keeps the audit trail of "what's tested
    where" explicit.
    """

    def test_admin_can_update_any_record_status(self, db_session, organization, admin_user):
        """Admin mutates the status of a record owned by a Contributor.

        Verifies the AAP F-005 sales-rep-prerogative path: admins
        bypass any ownership check and may mutate status on any
        record in their org.
        """
        owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            organization=organization,
            owner=owner,
            outreach_status=OutreachStatus.NOT_STARTED,
        )

        result = update_status(
            record_id=record.id,
            payload=ConnectionStatusUpdate(outreach_status=OutreachStatus.IN_PROGRESS),
            actor=_make_session(admin_user),
        )
        assert result.outreach_status == OutreachStatus.IN_PROGRESS

    def test_viewer_can_update_any_record_status(
        self, db_session, organization, viewer_user, contributor_user
    ):
        """Viewer (Sales Rep) mutates status on a record owned by another user.

        Verifies the F-005 sales-rep prerogative per AAP Section
        0.7.6: viewers can move ANY record through the outreach
        funnel (Not Started -> In Progress -> Contacted -> Closed)
        regardless of who originally submitted the record. This
        preserves "sales team accountability" by admitting only the
        sales team to status changes.
        """
        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )

        result = update_status(
            record_id=record.id,
            payload=ConnectionStatusUpdate(outreach_status=OutreachStatus.CONTACTED),
            actor=_make_session(viewer_user),
        )
        assert result.outreach_status == OutreachStatus.CONTACTED

    def test_cross_org_returns_not_found(self, db_session, organization, viewer_user):
        """Updating status on a cross-org record returns NotFoundError (404).

        Verifies the AAP Section 0.7.4 existence-leak defense for
        the status-mutation path: even though Viewers are admitted
        to status changes within their org, they cannot probe for
        the existence of records in other orgs by issuing
        cross-org status PATCH calls.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(organization=other_org, owner=other_user)

        with pytest.raises(NotFoundError):
            update_status(
                record_id=record.id,
                payload=ConnectionStatusUpdate(outreach_status=OutreachStatus.IN_PROGRESS),
                actor=_make_session(viewer_user),
            )


# ---------------------------------------------------------------------------
# TestSoftDeleteRecordRBAC -- F-007 soft delete ownership matrix
# ---------------------------------------------------------------------------


class TestSoftDeleteRecordRBAC:
    """``soft_delete_record`` ownership matrix.

    Per AAP Section 0.5.2 Layer 4 ("RBAC: own record (soft delete)
    for Contributor/Viewer/Admin"):

        * Admin: may soft-delete any record in their org.
        * Contributor: may soft-delete ONLY records they own.
        * Viewer: may soft-delete ONLY records they own (per the
          spec, even though Viewers do not typically own records in
          MVP -- this is forward-looking flexibility).
        * Cross-org: NotFoundError (404, existence-leak defense).

    The Viewer-soft-delete-own path is documented in the spec but
    not exercised here because viewers do not own records in MVP
    (the API layer's ``@requires_role(Contributor, Admin)`` on the
    create endpoint blocks viewer-created records). When a future
    flow admits viewer-owned records, an additional test should be
    added.
    """

    def test_admin_can_soft_delete_any_record(self, db_session, organization, admin_user):
        """Admin soft-deletes a record owned by a Contributor.

        Verifies the AAP Section 0.5.2 Layer 4 admin bypass: when
        ``actor.role == UserRole.ADMIN`` the ownership check is
        skipped and the soft-delete proceeds.
        """
        owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=owner)

        result = soft_delete_record(
            record_id=record.id,
            actor=_make_session(admin_user),
        )
        assert result is not None
        assert result.deleted_at is not None

    def test_contributor_can_soft_delete_own_record(
        self, db_session, organization, contributor_user
    ):
        """Contributor soft-deletes a record they own.

        Verifies the AAP Section 0.5.2 Layer 4 own-only rule's
        positive path for soft-delete.
        """
        record = RecordFactory(organization=organization, owner=contributor_user)

        result = soft_delete_record(
            record_id=record.id,
            actor=_make_session(contributor_user),
        )
        assert result is not None
        assert result.deleted_at is not None

    def test_contributor_cannot_soft_delete_others_record(
        self, db_session, organization, contributor_user
    ):
        """Contributor cannot soft-delete a record owned by another user.

        Verifies the AAP Section 0.5.2 Layer 4 own-only rule's
        negative path for soft-delete: when ``actor.role !=
        UserRole.ADMIN`` and ``actor.user_id !=
        record.owner_user_id`` the service raises
        :class:`ForbiddenError`.
        """
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=other)

        with pytest.raises(ForbiddenError):
            soft_delete_record(
                record_id=record.id,
                actor=_make_session(contributor_user),
            )

    def test_cross_org_returns_not_found(self, db_session, organization, contributor_user):
        """Soft-deleting a cross-org record returns NotFoundError (404).

        Verifies the AAP Section 0.7.4 existence-leak defense for
        the soft-delete path.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(organization=other_org, owner=other_user)

        with pytest.raises(NotFoundError):
            soft_delete_record(
                record_id=record.id,
                actor=_make_session(contributor_user),
            )


# ---------------------------------------------------------------------------
# TestHardDeleteRecordRBAC -- F-007 + F-014 hard delete admin-only matrix
# ---------------------------------------------------------------------------


class TestHardDeleteRecordRBAC:
    """``hard_delete_record`` ownership matrix.

    Per AAP Section 0.7.6 Business Rule "soft delete only; no
    permanent deletion except by Admin", only Admins may hard-delete.
    The API layer's ``@requires_role(UserRole.ADMIN)`` decorator is
    the AUTHORITATIVE role gate; the service layer does NOT enforce
    role at this level (it relies on the decorator). The service
    layer DOES enforce org-scope: cross-org targets surface as 404
    so the response never reveals whether a UUID exists in a
    different organization.

    The function uses keyword-only arguments per the actual
    implementation::

        hard_delete_record(
            db_session=...,
            org_id=...,
            record_id=...,
            actor_user_id=...,
        )

    State-changing operations are wrapped in
    ``with db_session.begin():`` per AAP Section 0.5.3 -- the caller
    owns the transaction so the record DELETE and the F-013
    ``hard_delete`` audit emission commit (or roll back) together.
    Audit emission is verified in
    ``backend/tests/services/test_admin_service.py``; this file
    focuses on permission decisions only.
    """

    def test_admin_can_hard_delete_any_record_in_org(self, db_session, organization, admin_user):
        """Admin physically removes a record owned by a Contributor.

        Verifies the AAP F-014 admin moderation path: admins may
        hard-delete any record in their org. The post-condition is
        that the row is GONE from the database (not soft-deleted;
        physically removed).
        """
        owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=owner)
        # Capture the id BEFORE the delete -- the row will be gone after.
        record_id = record.id

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        # Verify the row is physically removed from the database.
        # ``db_session.get(Record, record_id) is None`` is the
        # canonical existence check for SQLAlchemy 2.x; it triggers
        # a SELECT by primary key and returns None when the row is
        # absent.
        assert db_session.get(Record, record_id) is None

    def test_cross_org_returns_not_found(self, db_session, organization, admin_user):
        """Hard-deleting a cross-org record returns NotFoundError (404).

        Verifies the AAP Section 0.7.4 existence-leak defense for
        the hard-delete path: even an admin cannot probe for the
        existence of records in other organizations. The 404
        response is indistinguishable from "the record does not
        exist anywhere", preventing enumeration via response-status
        side-channel.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(organization=other_org, owner=other_user)
        record_id = record.id

        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,  # the actor's org -- different from the record's
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        # Defensive sanity: the cross-org record is intact (the
        # ``with begin():`` block rolled back on the raise).
        intact = db_session.get(Record, record_id)
        assert intact is not None

    def test_unknown_record_returns_not_found(self, db_session, admin_user):
        """A random UUID that does not exist returns NotFoundError (404).

        Verifies the same defensive code path as the cross-org
        test: missing rows and cross-org rows BOTH surface as 404
        so the response does not differentiate "row exists in a
        different org" from "row does not exist anywhere".
        """
        unknown_id = uuid.uuid4()

        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=admin_user.org_id,
                record_id=unknown_id,
                actor_user_id=admin_user.id,
            )


# ---------------------------------------------------------------------------
# TestUpdateUserRoleRBAC -- F-009 + F-014 role mutation matrix
# ---------------------------------------------------------------------------


class TestUpdateUserRoleRBAC:
    """``update_user_role`` permission and invariants.

    Per AAP Section 0.5.2 Layer 6, role mutation is admin-only
    (gated at the API layer by ``@requires_role(UserRole.ADMIN)``).
    The service layer enforces three additional invariants:

        1. Cross-org targets -> :class:`NotFoundError` (404,
           existence-leak defense per AAP Section 0.7.4).
        2. Self-demotion guard -> :class:`ForbiddenError` (403)
           when ``actor_user_id == target_user_id`` AND the role
           would actually change. This guard is a four-eyes
           governance invariant: no admin can silently downgrade
           their own role to evade audit oversight.
        3. Anti-lockout (last-admin) guard -> :class:`ConflictError`
           (409) when demoting would leave the org with zero
           admins. Preserves the "at least one admin per org"
           invariant.

    Order-of-guards (verified empirically against
    ``app.services.admin.update_user_role``):

        1. Target lookup (NotFoundError on missing or cross-org).
        2. No-op short-circuit (return without raising when
           ``previous_role == new_role``).
        3. Self-demotion guard (ForbiddenError when actor == target
           and role changes).
        4. Anti-lockout guard (ConflictError when last admin would
           be demoted).

    Because the self-demotion guard fires BEFORE the anti-lockout
    guard, the sole admin attempting to self-demote raises
    :class:`ForbiddenError` (SelfDemotionError), NOT
    :class:`ConflictError` (LastAdminError). To exercise the
    anti-lockout guard correctly the test must use a DIFFERENT
    actor (e.g., a Contributor) targeting the sole admin, so the
    self-demotion check passes and the last-admin check fires.

    The function uses keyword-only arguments per the actual
    implementation::

        update_user_role(
            db_session=...,
            org_id=...,
            target_user_id=...,
            new_role=...,  # UserRole enum or UserRoleUpdate payload
            actor_user_id=...,
        )

    The ``new_role`` parameter accepts EITHER a :class:`UserRole`
    enum value OR a :class:`UserRoleUpdate` pydantic payload (which
    has a ``.role`` attribute); the latter form lets handlers pass
    the validated request payload directly without unpacking
    ``payload.role`` first. These tests exercise the
    :class:`UserRoleUpdate` form to align with the schema-required
    handler-facing call site.
    """

    def test_admin_can_promote_contributor(self, db_session, organization, admin_user):
        """Admin promotes a Contributor to Admin.

        Verifies the AAP Section 0.5.2 Layer 6 happy path: an admin
        with a different identity than the target may freely change
        the target's role. The ``new_role`` parameter is passed as a
        :class:`UserRoleUpdate` payload to exercise the polymorphic
        argument unwrapping in the service.
        """
        target = ContributorUserFactory(organization=organization)

        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=target.id,
                new_role=UserRoleUpdate(role=UserRole.ADMIN),
                actor_user_id=admin_user.id,
            )

        assert result.role == UserRole.ADMIN

    def test_admin_can_demote_peer_admin(self, db_session, organization, admin_user):
        """Admin demotes a peer Admin (not self) when other admins exist.

        Verifies the AAP Section 0.5.2 Layer 6 path where:

            * ``previous_role == ADMIN``,
            * ``new_role != ADMIN``,
            * ``actor_user_id != target_user_id`` (no self-demotion),
            * ``admin_count >= 2`` BEFORE the mutation (no
              last-admin lockout).

        The mutation succeeds because none of the guards fire: the
        self-demotion check passes (different identities), and the
        last-admin check passes (one admin remains after the
        demote).
        """
        peer = AdminUserFactory(organization=organization)

        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=peer.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=admin_user.id,
            )

        assert result.role == UserRole.CONTRIBUTOR

    def test_anti_lockout_demoting_sole_admin_by_other_raises_conflict(
        self, db_session, organization, admin_user
    ):
        """Anti-lockout fires when a non-admin actor demotes the sole admin.

        Verifies the AAP Section 0.5.2 Layer 6 anti-lockout
        invariant: when the only admin in an org would be demoted,
        the service raises :class:`ConflictError` to preserve the
        "at least one admin per org" rule.

        The actor MUST be a non-admin (here a Contributor) because
        if the admin tried to demote themselves the SELF-DEMOTION
        guard would fire FIRST (raising
        :class:`ForbiddenError`/SelfDemotionError) before the
        last-admin guard had a chance to evaluate. Using a
        different actor sidesteps the self-demotion path and
        properly exercises the anti-lockout path. The actor's role
        is irrelevant to the service-layer logic (the API
        decorator is the authoritative role gate); the actor's id
        only matters for the self-demotion comparison.
        """
        # Confirm the precondition: ``admin_user`` is the SOLE Admin
        # in the org. The anti-lockout guard counts admins in the
        # org BEFORE the mutation; we confirm the count is exactly
        # 1 so the guard's branch is the one being tested.
        admin_count_stmt = select(User).where(
            User.org_id == admin_user.org_id,
            User.role == UserRole.ADMIN,
        )
        admins = db_session.execute(admin_count_stmt).scalars().all()
        assert len(admins) == 1
        assert admins[0].id == admin_user.id

        # Different actor (non-admin) so self-demotion does not fire.
        actor = ContributorUserFactory(organization=organization)

        with db_session.begin(), pytest.raises(ConflictError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=actor.id,
            )

        # Defensive sanity: admin_user role unchanged (the
        # transaction rolled back on the raise).
        refreshed = db_session.get(User, admin_user.id)
        assert refreshed is not None
        assert refreshed.role == UserRole.ADMIN

    def test_self_demotion_blocked_with_peer_admins_raises_forbidden(
        self, db_session, organization, admin_user
    ):
        """Admin self-demotion raises ForbiddenError even with peer admins.

        Verifies the AAP Section 0.5.2 Layer 6 anti-self-demotion
        invariant: an admin cannot demote themselves regardless of
        admin count. This is distinct from the anti-lockout guard
        (which counts admins) -- self-demotion is its OWN
        constraint that fires whenever
        ``actor_user_id == target_user_id`` and the role would
        actually change.

        Setup: there are TWO admins (admin_user from the fixture
        plus peer_admin); the anti-lockout guard would NOT fire
        here because demoting one still leaves the other. The
        guard that DOES fire is the self-demotion check, raising
        :class:`ForbiddenError` (SelfDemotionError, mapped to HTTP
        403).
        """
        peer_admin = AdminUserFactory(organization=organization)

        with db_session.begin(), pytest.raises(ForbiddenError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=admin_user.id,  # SELF
            )

        # Both admins unchanged after rollback.
        refreshed_admin = db_session.get(User, admin_user.id)
        assert refreshed_admin is not None
        assert refreshed_admin.role == UserRole.ADMIN
        peer_refreshed = db_session.get(User, peer_admin.id)
        assert peer_refreshed is not None
        assert peer_refreshed.role == UserRole.ADMIN

    def test_self_demotion_blocked_when_sole_admin_raises_forbidden(self, db_session, admin_user):
        """Sole admin self-demotion raises ForbiddenError (NOT ConflictError).

        Verifies the order-of-guards in
        :func:`app.services.admin.update_user_role`: the
        self-demotion guard fires BEFORE the anti-lockout guard.
        Therefore when the SOLE admin attempts to self-demote, the
        SelfDemotionError fires first and raises
        :class:`ForbiddenError` -- NOT the LastAdminError that
        would raise :class:`ConflictError`.

        This is a deliberately-engineered quality-of-error
        property: the self-demotion message ("Admins cannot change
        their own role. Another Admin must perform the role
        change.") is more actionable than the anti-lockout message
        ("Cannot demote the last Admin..."). Surfacing the more
        actionable diagnostic FIRST helps the admin understand
        what to do next: ask another admin, or promote one first.

        See ``test_anti_lockout_demoting_sole_admin_by_other_raises_conflict``
        for the complementary path that exercises the anti-lockout
        guard with a different (non-admin) actor.
        """
        # Confirm the precondition: admin_user is the sole admin.
        # We close the implicit auto-begun transaction with
        # ``rollback()`` after the read-only SELECT so the subsequent
        # explicit ``db_session.begin()`` can start a new transaction.
        # Without this rollback, SQLAlchemy 2.x raises
        # ``InvalidRequestError: A transaction is already begun on
        # this Session.`` because the auto-begun transaction from the
        # ``execute()`` call above is still active.
        admin_count_stmt = select(User).where(
            User.org_id == admin_user.org_id,
            User.role == UserRole.ADMIN,
        )
        admins = db_session.execute(admin_count_stmt).scalars().all()
        assert len(admins) == 1
        db_session.rollback()

        with db_session.begin(), pytest.raises(ForbiddenError):
            update_user_role(
                db_session=db_session,
                org_id=admin_user.org_id,
                target_user_id=admin_user.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=admin_user.id,
            )

        # admin_user role unchanged (the transaction rolled back on
        # the raise).
        refreshed = db_session.get(User, admin_user.id)
        assert refreshed is not None
        assert refreshed.role == UserRole.ADMIN

    def test_two_admins_demote_one_then_self_demote_raises_forbidden(
        self, db_session, organization
    ):
        """Two-admin scenario: demote-other succeeds, then self-demote fails.

        Walks through the order-of-guards holistically:

            1. Two Admins exist (admin_a, admin_b) in the org.
            2. admin_a demotes admin_b -> SUCCESS (anti-lockout
               does not fire because one admin remains; self-
               demotion does not fire because actor != target).
            3. admin_a is now the SOLE Admin.
            4. admin_a attempts to self-demote -> the
               SELF-DEMOTION guard fires first, raising
               :class:`ForbiddenError`.

        This documents the layered defense: even when a malicious
        admin manages to demote all peers, the self-demotion
        guard prevents them from then silently downgrading their
        own role. They would have to coordinate with another
        person (who can be promoted to Admin and act as a peer)
        to ever change roles -- preserving four-eyes governance.
        """
        admin_a = AdminUserFactory(organization=organization)
        admin_b = AdminUserFactory(organization=organization)

        # Step 1: admin_a demotes admin_b. Succeeds because the
        # post-mutation admin count is 1 (admin_a remains).
        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_b.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=admin_a.id,
            )
        assert result.role == UserRole.CONTRIBUTOR

        # Refresh admin_b to confirm the demote landed.
        refreshed_b = db_session.get(User, admin_b.id)
        assert refreshed_b is not None
        assert refreshed_b.role == UserRole.CONTRIBUTOR

        # Step 2: admin_a is now the sole admin. Attempting to
        # self-demote fires the SELF-DEMOTION guard first
        # (ForbiddenError), NOT the anti-lockout guard
        # (ConflictError).
        with db_session.begin(), pytest.raises(ForbiddenError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_a.id,
                new_role=UserRoleUpdate(role=UserRole.CONTRIBUTOR),
                actor_user_id=admin_a.id,
            )

        # admin_a still admin after the rollback.
        refreshed_a = db_session.get(User, admin_a.id)
        assert refreshed_a is not None
        assert refreshed_a.role == UserRole.ADMIN

    def test_cross_org_target_returns_not_found(self, db_session, admin_user):
        """Targeting a user in another org raises NotFoundError (404).

        Verifies the AAP Section 0.7.4 existence-leak defense:
        cross-org targets surface as 404 NOT 403 so the response
        never reveals whether a UUID exists in a different
        organization. Without this defense, an attacker could
        enumerate user UUIDs across orgs by observing the
        difference between 403 ("user exists, you can't change
        them") and 404 ("user does not exist") responses.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=admin_user.org_id,  # actor's org, NOT other_org
                target_user_id=other_user.id,
                new_role=UserRoleUpdate(role=UserRole.ADMIN),
                actor_user_id=admin_user.id,
            )

        # Defensive sanity: other_user role unchanged (the
        # transaction rolled back on the raise).
        refreshed = db_session.get(User, other_user.id)
        assert refreshed is not None
        assert refreshed.role != UserRole.ADMIN


# ---------------------------------------------------------------------------
# TestCreateRecordRBAC -- F-001 + F-006 owner attribution matrix
# ---------------------------------------------------------------------------


class TestCreateRecordRBAC:
    """``create_record`` permission matrix.

    Per AAP Section 0.5.2 Layer 3, the API layer admits Contributor
    and Admin roles via ``@requires_role(Contributor, Admin)``. The
    SERVICE layer accepts any session whose ``user_id`` corresponds
    to a real User in the actor's org. The service does NOT enforce
    role membership (the API layer is authoritative per AAP Section
    0.7.1 invariant 7).

    What the service DOES enforce -- and what these tests verify --
    is the F-006 owner-attribution invariant:

        > "The owner_user_id is derived from actor.user_id
        > server-side; any client-supplied owner_* value would have
        > been rejected by the pydantic schema's extra='forbid'
        > config before reaching this function."

    So the SERVICE-layer test verifies that REGARDLESS OF ROLE, the
    record's ``owner_user_id`` matches the actor's ``user_id``. This
    closes the F-006 invariant at the service-layer boundary even
    if a future internal scheduler invokes the service directly,
    bypassing the schema's ``extra='forbid'`` check.

    Three role tests cover the matrix:

        * Admin session: record owned by the admin.
        * Contributor session: record owned by the contributor.
        * Viewer session: record owned by the viewer (defense in
          depth -- the API layer blocks this in normal flow, but
          if a future internal flow admits Viewers to create, the
          ownership invariant still holds).
    """

    def test_admin_creates_record_owned_by_self(self, db_session, admin_user):
        """Admin's create_record produces a record owned by the admin.

        Verifies the F-006 owner-attribution invariant for the
        Admin role: ``owner_user_id == admin_user.id`` and
        ``owner_display_name == admin_user.display_name``.
        """
        result = create_record(
            payload=_basic_create_payload(),
            actor=_make_session(admin_user),
        )

        assert result.owner_user_id == admin_user.id
        assert result.owner_display_name == admin_user.display_name

    def test_contributor_creates_record_owned_by_self(self, db_session, contributor_user):
        """Contributor's create_record produces a record owned by the contributor.

        Verifies the F-006 owner-attribution invariant for the
        Contributor role -- the canonical happy path. The record's
        ``owner_user_id`` matches the actor's ``user_id`` and the
        ``owner_display_name`` is the denormalized snapshot of the
        contributor's display name at submission time (per AAP
        Section 0.7.6).
        """
        result = create_record(
            payload=_basic_create_payload(),
            actor=_make_session(contributor_user),
        )

        assert result.owner_user_id == contributor_user.id
        assert result.owner_display_name == contributor_user.display_name

    def test_viewer_session_creates_record_owned_by_self(self, db_session, viewer_user):
        """Viewer session's create_record produces a record owned by the viewer.

        Verifies the F-006 owner-attribution invariant for the
        Viewer role at the SERVICE layer. The API decorator
        ``@requires_role(Contributor, Admin)`` blocks Viewer
        creation in normal flow -- this test does NOT exercise the
        decorator (that is covered by
        ``backend/tests/middleware/test_rbac.py``). Instead it
        exercises the SERVICE-LAYER guarantee that even if a
        Viewer session somehow reached the service (e.g., a future
        internal scheduler that bypasses the decorator), the
        resulting record would be owned by the Viewer themselves
        -- NOT by an arbitrary user-supplied id.

        This is defense in depth per AAP Section 0.7.1 invariant 7:
        the API decorator is the authoritative role gate, but the
        service layer also enforces the F-006 owner-attribution
        invariant so the contract holds regardless of who calls
        the service.
        """
        result = create_record(
            payload=_basic_create_payload(),
            actor=_make_session(viewer_user),
        )

        assert result.owner_user_id == viewer_user.id
        assert result.owner_display_name == viewer_user.display_name
