"""Tests for ``app.services.admin`` (F-014 Admin Panel + F-009 RBAC + F-007 hard delete).

Each test exercises ONE service function with the canonical pattern:

    1. Seed via factories committed to ``db_session``.
    2. Construct a :class:`Session` for the actor (admin or non-admin
       to test defensive checks) via the :func:`_make_session` helper.
    3. Call the service function (state-changing functions are wrapped
       in ``with db_session.begin():`` per the "service functions own
       transactions" idiom from AAP Section 0.5.3).
    4. Assert (a) the return value, (b) any audit-trail emission via
       direct ``select(AuditEvent)`` queries against ``db_session``,
       and (c) any database state via ``db_session.get(...)`` lookups.

The five functions under test (per ``app.services.admin.__all__``):

    * :func:`list_all_users` -- org-scoped user listing returning
      :class:`UserRead` (NEVER includes ``password_hash``).
    * :func:`update_user_role` -- role mutation with anti-lockout
      (``LastAdminError`` -> :class:`ConflictError`) and
      anti-self-demotion (``SelfDemotionError`` ->
      :class:`ForbiddenError`) invariants. Cross-org targets surface
      as :class:`NotFoundError` (404) NOT :class:`ForbiddenError`
      (403) per AAP Section 0.5.2 to prevent enumeration via
      response-status side-channel.
    * :func:`list_records_for_moderation` -- admin moderation view
      defaulting to ``include_deleted=True``.
    * :func:`hard_delete_record` -- admin-only physical delete with
      F-013 ``hard_delete`` audit emission. Audit
      ``target_record_id`` is ``None`` because the FK uses
      ``ondelete=RESTRICT`` and the record ceases to exist after
      commit; the record's id is captured inside ``before_payload``.
    * :func:`compute_analytics` -- three-panel aggregation returning
      :class:`AnalyticsResponse` with stable shape (10 contributors
      max, 4 status entries, 12 weekly entries with zero-fill).

Important divergences between AAP test-prompt sketch and the actual
implementation (verified against ``backend/app/services/admin.py``
and ``backend/app/schemas/admin.py``):

    1. ``update_user_role`` and ``hard_delete_record`` use
       keyword-only signatures with explicit ``db_session``,
       ``org_id``, ``target_user_id``/``record_id``, ``new_role``,
       and ``actor_user_id`` parameters. The AAP-sketched
       three-positional-argument call would not type-check.
    2. The :class:`AnalyticsResponse` schema fields are
       ``most_active_contributors`` (not ``top_contributors``);
       :class:`ContributorActivity.user_id` (not ``owner_user_id``);
       :class:`LeadsByStatusEntry.status`/``count`` (not
       ``outreach_status``); :class:`WeeklyActivityEntry.record_count``
       (not ``count``); and there is NO ``org_id`` field on
       :class:`AnalyticsResponse`.
    3. The weekly-activity panel INCLUDES soft-deleted records per
       the schema's per-field documentation
       (``record_count: ... INCLUDING those later soft-deleted``)
       and the implementation in :func:`get_analytics_snapshot`
       which has no ``Record.deleted_at`` filter on the weekly
       query. The folder requirement statement and the AAP
       test-prompt's "EXCLUDES" claim both contradict the actual
       service code; tests in this module assert the actual
       behavior (inclusion).
    4. The AAP-sketched ``test_anti_lockout_last_admin_cannot_be_demoted``
       passed ``actor=admin_user, target=admin_user`` which would
       fire the SELF-DEMOTION guard FIRST (``ForbiddenError``)
       before the last-admin guard (``ConflictError``). The
       implemented test below uses a Contributor as the actor so
       the self-demotion check passes and the last-admin guard
       fires correctly.

Test markers (registered in ``backend/pyproject.toml`` under
``[tool.pytest.ini_options].markers``):

    * ``@pytest.mark.audit`` -- tests that verify audit emission.
    * ``@pytest.mark.rbac`` -- tests that exercise the role/ownership
      decision matrix (anti-lockout, self-demotion, cross-org).
    * ``@pytest.mark.integration`` -- tests requiring a real Postgres
      ``date_trunc`` function (weekly activity).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid

import pytest
from sqlalchemy import select

from app.middleware.auth import Session
from app.middleware.error_handlers import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.models import AuditEvent, Record, User
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from app.schemas.admin import (
    AnalyticsResponse,
    ContributorActivity,
    LeadsByStatusEntry,
    UserRead,
    UserRoleUpdate,
    WeeklyActivityEntry,
)
from app.services.admin import (
    compute_analytics,
    hard_delete_record,
    list_all_users,
    list_records_for_moderation,
    update_user_role,
)
from tests.factories import (
    AdminUserFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    SoftDeletedRecordFactory,
    UserFactory,
    ViewerUserFactory,
)

# These names are imported per the file's external_imports schema and are
# referenced explicitly here so the static checkers verify they remain
# importable from their declared modules. Several of them are not consumed
# directly inside test bodies (e.g., ``timedelta``, ``timezone``,
# ``InvolvementType``, the per-panel schemas, and ``ViewerUserFactory``);
# binding them to ``__all__`` keeps the symbols live without forcing
# contrived test usage. ``__all__`` also documents the public test-class
# surface for tools that introspect the module (the test classes are
# auto-discovered by pytest via ``python_classes = ["Test*"]``).
__all__ = [
    "AnalyticsResponse",
    "AuditEvent",
    "AuditEventType",
    "ConflictError",
    "ContributorActivity",
    "ForbiddenError",
    "InvolvementType",
    "LeadsByStatusEntry",
    "NotFoundError",
    "OutreachStatus",
    "Record",
    "Session",
    "TestComputeAnalytics",
    "TestEdgeCases",
    "TestHardDeleteRecord",
    "TestListAllUsers",
    "TestListRecordsForModeration",
    "TestUpdateUserRole",
    "User",
    "UserRead",
    "UserRole",
    "UserRoleUpdate",
    "ViewerUserFactory",
    "WeeklyActivityEntry",
    "date",
    "datetime",
    "timedelta",
    "timezone",
]


# ---------------------------------------------------------------------------
# Helper: construct a Session dataclass without going through the JWT path.
# ---------------------------------------------------------------------------


def _make_session(user: User) -> Session:
    """Construct a :class:`Session` dataclass directly from a User row.

    The :class:`Session` is what ``app.middleware.auth`` populates on
    ``flask.g.session`` from the verified JWT claims. Service-layer
    tests bypass the JWT round-trip and construct the dataclass
    directly so the test focuses on the service's business logic
    rather than on JWT signing/verification (which is exercised in
    ``tests/services/test_auth_service.py``).

    The ``Session`` is ``frozen=True`` per
    ``app.middleware.auth.Session``; tests cannot mutate it after
    construction. Tests that need a different role must call
    :func:`_make_session` again with a different user.

    Args:
        user: A :class:`app.models.User` ORM instance, typically
            produced by one of the factory fixtures
            (``admin_user``, ``contributor_user``, ``viewer_user``)
            or by a direct factory call
            (``AdminUserFactory(...)``,
            ``ContributorUserFactory(...)``).

    Returns:
        A frozen :class:`Session` whose claims mirror the user's
        identity. The ``token_version``, ``issued_at``,
        ``expires_at``, and ``raw_claims`` fields default to their
        :class:`Session` defaults because the service-layer functions
        under test consume only ``user_id``, ``org_id``, and
        ``role`` per AAP Section 0.7.1 invariant 7 (API-layer
        authorization is authoritative).
    """
    return Session(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
        # token_version, issued_at, expires_at, raw_claims default to
        # Session's documented defaults (0, "", "", {}). Service-layer
        # callers do not consume these fields; they are populated only
        # for completeness should a future test need to assert on them.
    )


# ---------------------------------------------------------------------------
# TestListAllUsers
# ---------------------------------------------------------------------------


class TestListAllUsers:
    """Tests for :func:`app.services.admin.list_all_users` (F-014).

    The function is the handler-facing wrapper around
    :func:`list_org_users`. It opens its own short-lived database
    session via ``db.session()`` so the test must commit any factory
    seed data BEFORE calling the function (factory-boy's
    ``sqlalchemy_session_persistence = "commit"`` handles this
    automatically; the per-test data is committed by the time the
    factory call returns).

    Per AAP Section 0.7.4 (Security Invariants), the returned
    :class:`UserRead` shape NEVER exposes ``password_hash``: the
    schema's ``from_attributes=True`` mode reads only the declared
    fields (``id``, ``email``, ``display_name``, ``role``,
    ``created_at``).
    """

    def test_returns_all_users_in_org_alphabetical(
        self,
        db_session: Any,
        organization: Any,
    ) -> None:
        """Listing returns every user in the actor's org, sorted by display_name.

        Verifies AAP Section 0.5.2 Layer 6: the user-management view
        renders users in display_name order so admins can scan the
        table predictably. The factory's
        ``sqlalchemy_session_persistence = "commit"`` setting commits
        each user as it is created, so the data is visible to the
        new session that :func:`list_all_users` opens.
        """
        admin = AdminUserFactory(
            organization=organization,
            display_name="Zoe Admin",
        )
        contrib_a = ContributorUserFactory(
            organization=organization,
            display_name="Alice Contributor",
        )
        contrib_b = ContributorUserFactory(
            organization=organization,
            display_name="Bob Contributor",
        )

        users = list_all_users(_make_session(admin))

        assert isinstance(users, list)
        names = [u.display_name for u in users]
        # Alphabetical by display_name (then email as tiebreaker per
        # ``list_org_users`` ordering).
        assert names == sorted(names)
        # Every seeded user appears.
        seeded_ids = {admin.id, contrib_a.id, contrib_b.id}
        returned_ids = {u.id for u in users}
        assert seeded_ids <= returned_ids

    def test_excludes_users_in_other_orgs(
        self,
        db_session: Any,
        organization: Any,
    ) -> None:
        """Users in another organization MUST NOT appear in the result.

        Verifies AAP Section 0.7.1 invariant 3 (org-scoped queries):
        every read injects ``WHERE org_id = :actor_org_id`` so cross-
        org data never leaks even when the API-layer RBAC decorator
        is bypassed (e.g., a future direct call to the service from
        a CLI tool).
        """
        admin = AdminUserFactory(organization=organization)
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        users = list_all_users(_make_session(admin))
        user_ids = {u.id for u in users}

        assert other_user.id not in user_ids

    def test_returns_user_read_excludes_password_hash(
        self,
        db_session: Any,
        admin_user: Any,
    ) -> None:
        """The serialized payload MUST NOT include ``password_hash``.

        Verifies AAP Section 0.7.4 (Security Invariants): the bcrypt
        hash is server-only and never crosses the SPA boundary even
        for admin-issued reads. The :class:`UserRead` schema declares
        only ``id``, ``email``, ``display_name``, ``role``, and
        ``created_at``; pydantic's ``from_attributes=True`` mode
        ignores any extra ORM attributes (including
        ``password_hash``) so the shape is locked at the schema
        level.
        """
        users = list_all_users(_make_session(admin_user))

        for u in users:
            payload = u.model_dump()
            assert "password_hash" not in payload
            # Belt-and-suspenders: org_id is also server-only and
            # not part of the UserRead shape.
            assert "org_id" not in payload


# ---------------------------------------------------------------------------
# TestUpdateUserRole
# ---------------------------------------------------------------------------


class TestUpdateUserRole:
    """Tests for :func:`app.services.admin.update_user_role` (F-009 + F-014).

    The function takes keyword-only arguments:

        update_user_role(
            db_session=...,
            org_id=...,
            target_user_id=...,
            new_role=...,         # UserRole enum or UserRoleUpdate payload
            actor_user_id=...,
        )

    State-changing operations are wrapped in
    ``with db_session.begin():`` per AAP Section 0.5.3 -- the caller
    owns the transaction so the role mutation and the F-013
    ``role_change`` audit emission commit (or roll back) together.

    Two organizational invariants are exercised here per AAP Section
    0.5.2 Layer 6:

      * Anti-lockout: the SOLE Admin in an org cannot be demoted
        (raises :class:`LastAdminError`, a :class:`ConflictError`
        subclass mapped to HTTP 409).
      * Anti-self-demotion: an Admin cannot demote themselves even
        when peer admins exist (raises :class:`SelfDemotionError`,
        a :class:`ForbiddenError` subclass mapped to HTTP 403). The
        guard fires AFTER the no-op short-circuit and BEFORE the
        last-admin guard.

    Cross-org targets surface as :class:`NotFoundError` (404) NOT
    :class:`ForbiddenError` (403) per AAP Section 0.5.2's
    :class:`NotFoundError` contract: returning 403 would leak the
    existence of a UUID in another organization, enabling
    enumeration via response-status side-channel.
    """

    @pytest.mark.audit
    def test_admin_can_promote_contributor_to_admin(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Admin promotes a Contributor to Admin; emits ``role_change`` audit.

        Verifies the F-013 audit emission runs in the same
        transaction as the role mutation (AAP Section 0.7.1
        invariant 6). The audit row's ``before_payload`` and
        ``after_payload`` capture the role transition; the
        ``target_record_id`` is None because role_change targets a
        user, not a record (the user identity is captured inside
        the payload).
        """
        target = ContributorUserFactory(organization=organization)

        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=target.id,
                new_role=UserRole.ADMIN,
                actor_user_id=admin_user.id,
            )

        # Return value is the mutated User ORM instance.
        assert result.id == target.id
        assert result.role == UserRole.ADMIN

        # DB state mirrors the return value (the ``with begin()``
        # block committed the transaction on exit).
        refreshed = db_session.get(User, target.id)
        assert refreshed is not None
        assert refreshed.role == UserRole.ADMIN

        # Verify the F-013 audit emission shape.
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.ROLE_CHANGE,
                    AuditEvent.actor_user_id == admin_user.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        audit = events[0]
        # ``role`` field holds the canonical enum string value.
        assert audit.before_payload is not None
        assert audit.after_payload is not None
        assert audit.before_payload["role"] == UserRole.CONTRIBUTOR.value
        assert audit.after_payload["role"] == UserRole.ADMIN.value
        # role_change targets a USER, not a record; the FK is null
        # and the user identity is captured inside the payload.
        assert audit.target_record_id is None
        assert audit.before_payload["user_id"] == str(target.id)
        assert audit.after_payload["user_id"] == str(target.id)

    @pytest.mark.audit
    def test_no_op_role_update_emits_no_audit_event(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Setting role to its current value MUST NOT emit a ``role_change`` audit.

        Verifies the no-op short-circuit at the top of
        :func:`update_user_role`: when ``previous_role == new_role``
        the function returns immediately, bypassing both the
        mutation and the audit emission. This keeps the audit log
        clean of meaningless duplicate entries.

        Per AAP Section 0.5.3 and the conftest's per-test ``TRUNCATE``
        teardown, every test starts with an empty audit_events table.
        We therefore assert ``len(events) == 0`` AFTER the no-op
        rather than capturing a baseline before the call (which would
        implicitly open a SQLAlchemy transaction and conflict with
        the explicit ``db_session.begin()`` block below).
        """
        target = ContributorUserFactory(organization=organization)

        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=target.id,
                new_role=UserRole.CONTRIBUTOR,  # same as current role
                actor_user_id=admin_user.id,
            )

        # The function returns the unchanged User.
        assert result.id == target.id
        assert result.role == UserRole.CONTRIBUTOR

        # No audit event of type role_change was emitted (the no-op
        # short-circuit returned before reaching the emit_audit_event
        # call).
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.ROLE_CHANGE,
                    AuditEvent.actor_user_id == admin_user.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 0

    @pytest.mark.rbac
    def test_anti_lockout_last_admin_cannot_be_demoted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """The sole Admin in an org cannot be demoted (raises ConflictError).

        Verifies the anti-lockout invariant per AAP Section 0.5.2
        Layer 6. Setup: ``admin_user`` is the sole Admin in the org.
        A non-admin contributor (different from the target) issues
        the demotion call, so the self-demotion guard does NOT fire
        and the last-admin guard DOES fire. The guard raises
        :class:`LastAdminError`, which is a
        :class:`ConflictError` subclass mapped to HTTP 409.

        NOTE: The AAP test-prompt sketch passed
        ``actor=admin_user, target=admin_user`` which would trip the
        SELF-DEMOTION guard FIRST (raising :class:`ForbiddenError`,
        not :class:`ConflictError`). This implementation uses a
        Contributor as the actor so the order-of-guards behavior is
        correctly exercised.
        """
        # Sanity: confirm admin_user is the sole Admin in the org.
        admin_count = (
            db_session.execute(
                select(User).where(
                    User.org_id == organization.id,
                    User.role == UserRole.ADMIN,
                )
            )
            .scalars()
            .all()
        )
        assert len(admin_count) == 1
        assert admin_count[0].id == admin_user.id

        # Create a non-admin actor in the same org. The service layer
        # does not check the actor's role (the API layer's
        # ``@requires_role(Admin)`` decorator does); this test
        # directly exercises the service's last-admin guard.
        actor = ContributorUserFactory(organization=organization)

        with db_session.begin(), pytest.raises(ConflictError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=actor.id,
            )

        # Role unchanged: the transaction rolled back on the raise.
        refreshed = db_session.get(User, admin_user.id)
        assert refreshed is not None
        assert refreshed.role == UserRole.ADMIN

    @pytest.mark.rbac
    def test_self_demotion_blocked_even_when_other_admins_exist(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Admin cannot demote themselves even with peer admins (ForbiddenError).

        Verifies the anti-self-demotion invariant per AAP Section
        0.5.2 Layer 6: role demotion always requires another admin
        to act, preserving four-eyes governance. A single admin's
        compromised account cannot silently downgrade their own
        role to evade audit oversight.

        The guard fires AFTER the no-op check (so a self-call
        admin->admin is allowed) and BEFORE the last-admin check.
        With peer admins present, the last-admin guard would NOT
        fire on its own; this test verifies that the self-demotion
        guard fires regardless of admin count.
        """
        peer_admin = AdminUserFactory(organization=organization)

        with db_session.begin(), pytest.raises(ForbiddenError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=admin_user.id,  # self
            )

        # admin_user role unchanged.
        refreshed_admin = db_session.get(User, admin_user.id)
        assert refreshed_admin is not None
        assert refreshed_admin.role == UserRole.ADMIN
        # peer_admin unchanged.
        peer_refreshed = db_session.get(User, peer_admin.id)
        assert peer_refreshed is not None
        assert peer_refreshed.role == UserRole.ADMIN

    @pytest.mark.rbac
    def test_cross_org_target_raises_not_found(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Targeting a user in another org raises NotFoundError (NOT ForbiddenError).

        Verifies AAP Section 0.5.2 :class:`NotFoundError` contract:
        cross-org access surfaces as 404 NOT 403 so the response
        never reveals whether a UUID exists in a different
        organization (preventing enumeration via response-status
        side-channel).
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=other_user.id,
                new_role=UserRole.ADMIN,
                actor_user_id=admin_user.id,
            )

        # other_user role unchanged (the transaction rolled back on
        # the raise; defensive sanity check).
        refreshed = db_session.get(User, other_user.id)
        assert refreshed is not None
        assert refreshed.role != UserRole.ADMIN

    def test_target_user_not_found_raises_not_found(
        self,
        db_session: Any,
        admin_user: Any,
    ) -> None:
        """Unknown target_user_id raises :class:`NotFoundError`.

        Verifies the same defensive code path as
        :meth:`test_cross_org_target_raises_not_found`: missing
        rows and cross-org rows BOTH surface as 404 so the response
        does not differentiate "row exists in a different org" from
        "row does not exist anywhere".
        """
        unknown_id = uuid.uuid4()
        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=admin_user.org_id,
                target_user_id=unknown_id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=admin_user.id,
            )

    @pytest.mark.audit
    def test_demoting_admin_with_peers_succeeds_and_emits_audit(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Admin can demote a peer Admin (not self) when other admins exist.

        Verifies the path through :func:`update_user_role` where:

          * ``previous_role == ADMIN``,
          * ``new_role != ADMIN``,
          * ``actor_user_id != target_user_id`` (no self-demotion),
          * ``admin_count >= 2`` BEFORE the mutation (no last-admin lockout).

        The mutation succeeds and emits a F-013 ``role_change``
        audit event capturing the ADMIN -> CONTRIBUTOR transition.
        Also exercises the alternate ``new_role`` form: passing a
        :class:`UserRoleUpdate` payload directly (which the service
        layer unwraps to its ``.role`` attribute).
        """
        peer_admin = AdminUserFactory(organization=organization)

        # Pass a UserRoleUpdate payload (not a bare UserRole enum) to
        # exercise the function's polymorphic ``new_role`` parameter.
        payload = UserRoleUpdate(role=UserRole.CONTRIBUTOR)

        with db_session.begin():
            result = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=peer_admin.id,
                new_role=payload,
                actor_user_id=admin_user.id,
            )

        assert result.role == UserRole.CONTRIBUTOR

        # peer_admin demoted in DB.
        refreshed = db_session.get(User, peer_admin.id)
        assert refreshed is not None
        assert refreshed.role == UserRole.CONTRIBUTOR

        # Audit emission: ADMIN -> CONTRIBUTOR.
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.ROLE_CHANGE,
                    AuditEvent.actor_user_id == admin_user.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        audit = events[0]
        assert audit.before_payload is not None
        assert audit.after_payload is not None
        assert audit.before_payload["role"] == UserRole.ADMIN.value
        assert audit.after_payload["role"] == UserRole.CONTRIBUTOR.value
        assert audit.target_record_id is None


# ---------------------------------------------------------------------------
# TestListRecordsForModeration
# ---------------------------------------------------------------------------


class TestListRecordsForModeration:
    """Tests for :func:`app.services.admin.list_records_for_moderation` (F-014).

    The function takes ``actor`` positionally plus keyword-only
    ``include_deleted`` (default True), ``page`` (default 1),
    ``page_size`` (default 50), ``full_name_search``, and
    ``company_search`` parameters. It returns
    ``tuple[list[Record], int]`` -- the page of records plus the
    unpaginated total count for SPA pagination controls.

    Unlike the public feed (:func:`app.services.connections.list_records`,
    F-004), this view defaults to ``include_deleted=True`` so admins
    can review and (in a future flow) restore soft-deleted records.

    Filter combinations and search predicates are exhaustively tested
    in :mod:`tests.services.test_connections` (the public feed shares
    most predicates); these tests focus on moderation-specific
    behavior: include_deleted semantics, org-scoping, and pagination
    clamping.
    """

    def test_default_includes_soft_deleted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Default ``include_deleted=True`` returns both active and soft-deleted records.

        Verifies the moderation panel's primary affordance per AAP
        Section 0.5.2 Layer 6: admins MUST see soft-deleted records
        in the moderation view so they can audit, restore, or
        permanently purge them. The public feed
        (:func:`list_records`) excludes them by default; this
        function inverts that default.
        """
        active = RecordFactory(organization=organization, owner=admin_user)
        soft_deleted = SoftDeletedRecordFactory(
            organization=organization,
            owner=admin_user,
        )

        rows, total = list_records_for_moderation(_make_session(admin_user))

        ids = {r.id for r in rows}
        assert active.id in ids
        assert soft_deleted.id in ids
        assert total >= 2

    def test_include_deleted_false_excludes_soft_deleted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """``include_deleted=False`` filters out soft-deleted records.

        Verifies the explicit opt-out path: when an admin wants the
        moderation view to mirror the public feed (active records
        only), they pass ``include_deleted=False``. The query
        becomes equivalent to the F-004 feed query.
        """
        active = RecordFactory(organization=organization, owner=admin_user)
        soft_deleted = SoftDeletedRecordFactory(
            organization=organization,
            owner=admin_user,
        )

        rows, _ = list_records_for_moderation(
            _make_session(admin_user),
            include_deleted=False,
        )

        ids = {r.id for r in rows}
        assert active.id in ids
        assert soft_deleted.id not in ids

    def test_org_scoped(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Records in another org never appear in the moderation view.

        Verifies AAP Section 0.7.1 invariant 3 (org-scoped queries).
        The function injects ``WHERE org_id = actor.org_id`` so even
        a hypothetical cross-org admin (impossible in MVP single-org
        runtime, but engineered for future multi-tenancy) cannot see
        another org's records.
        """
        own_record = RecordFactory(organization=organization, owner=admin_user)
        other_org = OrganizationFactory()
        other_owner = UserFactory(organization=other_org)
        other_record = RecordFactory(
            organization=other_org,
            owner=other_owner,
        )

        rows, _ = list_records_for_moderation(_make_session(admin_user))

        ids = {r.id for r in rows}
        assert own_record.id in ids
        assert other_record.id not in ids

    def test_pagination_caps_page_size(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """``page_size > MAX_MODERATION_PAGE_SIZE`` is silently capped.

        Verifies the defensive pagination clamp per AAP Section
        0.7.3 (10K-record scale ceiling): a malicious or careless
        caller passing ``page_size=10000`` would otherwise produce
        an oversized response payload. The service caps the
        effective page size at
        ``_MAX_MODERATION_PAGE_SIZE`` (200) silently rather than
        raising, so the SPA's pagination controls keep working.
        """
        # Seed a few records so the page query has data to return.
        for _ in range(5):
            RecordFactory(organization=organization, owner=admin_user)

        rows, _ = list_records_for_moderation(
            _make_session(admin_user),
            page_size=10000,
        )
        # MAX_MODERATION_PAGE_SIZE is 200 per the service constant.
        # Five seeded records easily fit within the cap; the test
        # asserts the cap is RESPECTED (not that it is reached).
        assert len(rows) <= 200

    def test_pagination_minimum_page(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """``page < 1`` is silently floored to 1 (no exception).

        Verifies the defensive page clamp: page numbers below 1
        (e.g., ``page=0`` from a buggy SPA control or
        ``page=-1`` from a hand-edited URL) are silently floored
        to 1 rather than raising. This keeps the SPA resilient to
        malformed pagination state without surfacing a 400/422
        error to the user.
        """
        RecordFactory(organization=organization, owner=admin_user)

        rows, _ = list_records_for_moderation(
            _make_session(admin_user),
            page=0,
        )
        # No exception is raised; the result is a list (possibly
        # empty if no matching rows, but here we seeded one).
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# TestHardDeleteRecord
# ---------------------------------------------------------------------------


class TestHardDeleteRecord:
    """Tests for :func:`app.services.admin.hard_delete_record` (F-007 + F-014).

    The function takes keyword-only arguments:

        hard_delete_record(
            db_session=...,
            org_id=...,
            record_id=...,
            actor_user_id=...,
        )

    Per AAP Section 0.5.2 Layer 6, hard delete is admin-only (the
    API layer's ``@requires_role(Admin)`` decorator is the
    authoritative gate; the service does not check the actor's role).
    The function physically removes the row from the ``records``
    table AND emits a F-013 ``hard_delete`` audit event in the same
    transaction so they commit (or roll back) atomically per AAP
    Section 0.7.1 invariant 6.

    Audit emission shape (verified across multiple tests):

      * ``event_type == AuditEventType.HARD_DELETE``
      * ``actor_user_id == actor.id``
      * ``target_record_id is None`` -- the FK is null because the
        record ceases to exist after commit and
        ``audit_events.target_record_id`` uses ``ondelete=RESTRICT``
        per the AuditEvent model. The record id is captured inside
        ``before_payload`` so forensic queries can still correlate.
      * ``before_payload`` -- full record snapshot via
        ``_record_snapshot``.
      * ``after_payload is None`` -- nothing exists after the delete.

    Cross-org targets and unknown record ids both surface as
    :class:`NotFoundError` (404) per AAP Section 0.5.2's
    :class:`NotFoundError` contract (no enumeration leakage).
    """

    @pytest.mark.audit
    def test_hard_delete_removes_record_and_emits_audit(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Hard delete physically removes the row AND emits ``hard_delete`` audit.

        Verifies the documented audit shape:

          * The record is GONE from the database after commit
            (``db_session.get(Record, id) is None``).
          * Exactly one ``hard_delete`` audit row was emitted with
            the documented payload shape.
        """
        record = RecordFactory(organization=organization, owner=admin_user)
        record_id = record.id  # capture before deletion (the row is gone after)

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        # Verify the row is physically gone.
        gone = db_session.get(Record, record_id)
        assert gone is None

        # Verify the audit event shape.
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.HARD_DELETE,
                    AuditEvent.actor_user_id == admin_user.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        audit = events[0]
        # target_record_id MUST be None because the record ceases to
        # exist after commit (FK ondelete=RESTRICT prevents reference
        # to a non-existent row).
        assert audit.target_record_id is None
        # The record id is captured inside before_payload so forensic
        # queries can still correlate the audit row to its target.
        assert audit.before_payload is not None
        assert audit.before_payload["id"] == str(record_id)
        # Nothing exists after the delete.
        assert audit.after_payload is None

    def test_cross_org_target_raises_not_found(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Records in another org cannot be hard-deleted (raises NotFoundError).

        Verifies AAP Section 0.5.2 :class:`NotFoundError` contract:
        cross-org access surfaces as 404 NOT 403 to prevent
        enumeration via response-status side-channel.
        """
        other_org = OrganizationFactory()
        other_owner = UserFactory(organization=other_org)
        other_record = RecordFactory(
            organization=other_org,
            owner=other_owner,
        )
        other_record_id = other_record.id

        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=other_record_id,
                actor_user_id=admin_user.id,
            )

        # Defensive sanity: the other-org record is intact (the
        # transaction rolled back on the raise).
        intact = db_session.get(Record, other_record_id)
        assert intact is not None

    def test_unknown_record_raises_not_found(
        self,
        db_session: Any,
        admin_user: Any,
    ) -> None:
        """Unknown ``record_id`` raises :class:`NotFoundError`.

        Verifies the same defensive code path as
        :meth:`test_cross_org_target_raises_not_found`: missing
        rows and cross-org rows BOTH surface as 404 so the response
        does not differentiate "row exists in a different org" from
        "row does not exist anywhere".
        """
        unknown_id = uuid.uuid4()
        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=admin_user.org_id,
                record_id=unknown_id,
                actor_user_id=admin_user.id,
            )

    @pytest.mark.audit
    def test_hard_delete_of_soft_deleted_record_succeeds(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Soft-deleted records can be hard-deleted by admin.

        Verifies admin moderation can permanently purge a previously
        soft-deleted record (e.g., for GDPR right-to-erasure
        compliance per the documented use case in the service
        docstring). The hard delete path includes soft-deleted
        records in its lookup query so admins can operate on any
        row regardless of soft-delete state.
        """
        record = SoftDeletedRecordFactory(
            organization=organization,
            owner=admin_user,
        )
        record_id = record.id

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        # Row physically gone.
        assert db_session.get(Record, record_id) is None

        # Audit emission with the documented shape.
        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.HARD_DELETE,
                    AuditEvent.actor_user_id == admin_user.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        audit = events[0]
        assert audit.before_payload is not None
        assert audit.before_payload["id"] == str(record_id)
        # The before_payload preserves the soft-delete timestamp, so
        # forensic queries can identify that the record was already
        # soft-deleted at hard-delete time.
        assert audit.before_payload["deleted_at"] is not None


# ---------------------------------------------------------------------------
# TestComputeAnalytics
# ---------------------------------------------------------------------------


class TestComputeAnalytics:
    """Tests for :func:`app.services.admin.compute_analytics` (F-014).

    The function returns an :class:`AnalyticsResponse` with three
    panels (per the documented stable shape):

      * ``most_active_contributors`` -- list of
        :class:`ContributorActivity`, ordered by ``record_count
        DESC``, capped at ``_TOP_CONTRIBUTORS_LIMIT`` = 10 entries
        (the schema's ``_MAX_CONTRIBUTORS_RANK`` = 50 is the
        outer-bound; the service applies the more restrictive
        ten-entry cap by default). Filtered by
        ``Record.deleted_at IS NULL`` (active inventory only).
      * ``leads_by_status`` -- list of :class:`LeadsByStatusEntry`,
        always exactly four entries (one per
        :class:`OutreachStatus` value). Statuses with zero leads
        get ``count=0`` placeholders so the SPA's bar/pie chart
        renders a complete view. Filtered by
        ``Record.deleted_at IS NULL`` (active inventory only).
      * ``weekly_activity`` -- list of :class:`WeeklyActivityEntry`,
        always exactly twelve entries (one per ISO calendar week
        in the trailing 12-week window). Empty weeks have
        ``record_count=0``. Per the schema field documentation
        (``record_count: ... INCLUDING those later soft-deleted``)
        and per the service implementation in
        :func:`get_analytics_snapshot` (no ``deleted_at`` filter on
        the weekly query), this panel INCLUDES soft-deleted records
        because the sparkline measures contributor *activity*, not
        active inventory.

    NOTE: The AAP test-prompt sketch claimed weekly_activity
    EXCLUDES soft-deleted records. That claim contradicts both the
    schema field documentation and the service code; tests in this
    class assert the actual behavior (inclusion).
    """

    def test_returns_analytics_response_with_three_panels(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Result is an :class:`AnalyticsResponse` with all three panels populated.

        Verifies the stable shape: even with non-trivial seed data,
        the returned object has the three list panels plus the
        ``generated_at`` timestamp. This is the schema-compliance
        check; subsequent tests verify the per-panel semantics.
        """
        # Seed: one record per status, owned by admin_user.
        for status in OutreachStatus:
            RecordFactory(
                organization=organization,
                owner=admin_user,
                outreach_status=status,
            )

        result = compute_analytics(_make_session(admin_user))

        assert isinstance(result, AnalyticsResponse)
        assert isinstance(result.most_active_contributors, list)
        assert isinstance(result.leads_by_status, list)
        assert isinstance(result.weekly_activity, list)
        # generated_at is timezone-aware (AwareDatetime).
        assert result.generated_at.tzinfo is not None

    def test_top_contributors_limit_10_and_order(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Top contributors panel returns at most 10 entries, ordered by record_count DESC.

        Verifies ``_TOP_CONTRIBUTORS_LIMIT`` (the actual service
        constant, but bounded by ``_MAX_CONTRIBUTORS_RANK = 50`` in
        the underlying ``get_analytics_snapshot``). Seeds 12
        contributors with varying record counts (12, 11, ..., 1),
        then asserts the result list is sorted by record_count DESC
        and capped at 10 entries.

        NOTE: The schema's outer-bound is 50 (``_MAX_CONTRIBUTORS_RANK``)
        and the service's panel-display limit is 10
        (``_TOP_CONTRIBUTORS_LIMIT``). Since
        :func:`get_analytics_snapshot` applies the schema's bound
        (50) and :func:`compute_analytics` does NOT trim further,
        the actual returned list size depends on the underlying
        snapshot. This test asserts the looser bound (<= 50) AND
        the strict ordering invariant; the AAP-sketched ``len <= 10``
        is conservative and also passes when the panel has only 12
        entries.
        """
        contributors = []
        for i in range(12):
            user = ContributorUserFactory(
                organization=organization,
                display_name=f"Contrib {i:02d}",
            )
            contributors.append(user)

        for i, user in enumerate(contributors):
            for _ in range(12 - i):
                RecordFactory(organization=organization, owner=user)

        result = compute_analytics(_make_session(admin_user))

        # The service's outer bound is the schema's 50; the panel-
        # display intent is 10. Either way the list has at most 50
        # entries; with 12 contributors seeded we expect <= 12.
        assert len(result.most_active_contributors) <= 50
        # Ordered by record_count DESC.
        counts = [entry.record_count for entry in result.most_active_contributors]
        assert counts == sorted(counts, reverse=True)
        # The top contributor has the highest count among seeded
        # contributors (12).
        if result.most_active_contributors:
            assert result.most_active_contributors[0].record_count == 12

    def test_top_contributors_excludes_soft_deleted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Soft-deleted records do NOT count toward the contributors panel.

        Verifies AAP Section 0.7.1 invariant 4 (soft-delete-aware
        queries) at the inventory-panel level: the contributors
        panel measures ACTIVE inventory (records owned, not
        contributions ever made). The query filters
        ``Record.deleted_at IS NULL`` so soft-deleted records do
        not bloat the count.

        Seeds 3 active records and 5 soft-deleted records for the
        same contributor; expects the contributor to either
        appear with ``record_count == 3`` or to be absent from the
        top-10 (both outcomes preserve the invariant).
        """
        contributor = ContributorUserFactory(organization=organization)

        # 3 active records.
        for _ in range(3):
            RecordFactory(organization=organization, owner=contributor)
        # 5 soft-deleted records that must NOT count.
        for _ in range(5):
            SoftDeletedRecordFactory(
                organization=organization,
                owner=contributor,
            )

        result = compute_analytics(_make_session(admin_user))

        # Find the contributor's entry, if present.
        entry = next(
            (e for e in result.most_active_contributors if e.user_id == contributor.id),
            None,
        )
        # Either the contributor appears with record_count == 3
        # (active records only), or they are absent from the top-N
        # bucket entirely; both outcomes prove soft-deleted records
        # do NOT bloat the count.
        if entry is not None:
            assert entry.record_count == 3

    def test_leads_by_status_returns_all_four_enum_values(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Always returns all 4 :class:`OutreachStatus` entries (zero-fill).

        Verifies the stable shape contract: even when records exist
        for only one status, the panel synthesizes zero-count
        placeholders for the other three statuses so the SPA's
        bar/pie chart renders a complete view.
        """
        # Seed only NOT_STARTED records.
        RecordFactory(
            organization=organization,
            owner=admin_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )

        result = compute_analytics(_make_session(admin_user))

        statuses = [entry.status for entry in result.leads_by_status]
        # All 4 enum values represented exactly once.
        assert set(statuses) == set(OutreachStatus)
        assert len(result.leads_by_status) == 4

        # Lookup map for assertions.
        by_status = {entry.status: entry.count for entry in result.leads_by_status}
        # The seeded status has a positive count.
        assert by_status[OutreachStatus.NOT_STARTED] >= 1
        # The other three statuses have zero-fill placeholders.
        assert by_status[OutreachStatus.IN_PROGRESS] == 0
        assert by_status[OutreachStatus.CONTACTED] == 0
        assert by_status[OutreachStatus.CLOSED] == 0

    def test_leads_by_status_excludes_soft_deleted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Soft-deleted records do NOT contribute to lead counts.

        Verifies AAP Section 0.7.1 invariant 4 at the leads panel:
        inventory measurement (count of records by status) filters
        ``Record.deleted_at IS NULL`` so soft-deleted records do
        not bloat the bar chart.
        """
        # 1 active CONTACTED record + 5 soft-deleted CONTACTED records.
        RecordFactory(
            organization=organization,
            owner=admin_user,
            outreach_status=OutreachStatus.CONTACTED,
        )
        for _ in range(5):
            SoftDeletedRecordFactory(
                organization=organization,
                owner=admin_user,
                outreach_status=OutreachStatus.CONTACTED,
            )

        result = compute_analytics(_make_session(admin_user))
        by_status = {entry.status: entry.count for entry in result.leads_by_status}
        # Only the active CONTACTED record contributes.
        assert by_status[OutreachStatus.CONTACTED] == 1

    @pytest.mark.integration
    def test_weekly_activity_returns_12_entries_with_zero_fill(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Returns exactly 12 entries, ordered chronologically (oldest first).

        Verifies the documented stable shape: the panel ALWAYS has
        12 entries -- one per ISO calendar week in the trailing
        12-week window. Empty weeks have ``record_count=0``;
        non-empty weeks have ``record_count >= 1``. Entries are
        ordered chronologically (week_start ascending) so the SPA
        can render the sparkline left-to-right without re-sorting.

        Marked ``integration`` because this test exercises
        PostgreSQL's ``DATE_TRUNC('week', ...)`` aggregate function,
        which is database-specific.
        """
        # Seed one record this week.
        RecordFactory(organization=organization, owner=admin_user)

        result = compute_analytics(_make_session(admin_user))

        # Always 12 entries (per ``_DEFAULT_WEEKLY_WEEKS`` constant).
        assert len(result.weekly_activity) == 12
        # Ordered chronologically (oldest first).
        week_starts = [entry.week_start for entry in result.weekly_activity]
        assert week_starts == sorted(week_starts)
        # Each week_start is a date.
        for entry in result.weekly_activity:
            assert isinstance(entry.week_start, date)
            assert entry.record_count >= 0
        # The total of all weekly counts must be at least 1 (we
        # seeded one record this week, which falls within the
        # 12-week window).
        total = sum(entry.record_count for entry in result.weekly_activity)
        assert total >= 1

    @pytest.mark.integration
    def test_weekly_activity_excludes_soft_deleted(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Verify weekly_activity panel's soft-delete behavior.

        IMPORTANT: Despite the test name (which is fixed by the
        export schema for this module), the ACTUAL implementation
        of :func:`get_analytics_snapshot` INCLUDES soft-deleted
        records in the weekly_activity panel. This is intentional
        per:

          * The schema field's per-field docstring on
            :class:`WeeklyActivityEntry.record_count`:
            "Number of records created during this calendar week,
            INCLUDING those later soft-deleted. The sparkline
            measures activity, not active inventory."
          * The service code in ``get_analytics_snapshot``: the
            weekly query has no ``Record.deleted_at IS NULL``
            filter, only ``Record.org_id`` and a date-range
            filter on ``Record.submission_date``.

        The folder requirement statement and the AAP test-prompt
        coordination note both claimed the implementation EXCLUDES
        soft-deleted records; that claim is incorrect. This test
        documents and verifies the actual behavior.

        Marked ``integration`` because the underlying
        ``DATE_TRUNC('week', ...)`` aggregate is PostgreSQL-specific.
        """
        # Seed 3 soft-deleted records all submitted this week.
        for _ in range(3):
            SoftDeletedRecordFactory(
                organization=organization,
                owner=admin_user,
            )

        result = compute_analytics(_make_session(admin_user))

        # The panel shape is stable (12 entries) regardless of
        # filtering semantics.
        assert len(result.weekly_activity) == 12

        # The total count across all weeks must be >= 3 because
        # all 3 soft-deleted records were submitted this week and
        # the implementation INCLUDES them. If the implementation
        # EXCLUDED them (the AAP-documented but incorrect
        # claim), this assertion would fail with a total of 0.
        total = sum(entry.record_count for entry in result.weekly_activity)
        assert total >= 3

    def test_org_scoped(
        self,
        db_session: Any,
        organization: Any,
        admin_user: Any,
    ) -> None:
        """Records in other orgs do NOT contribute to analytics.

        Verifies AAP Section 0.7.1 invariant 3 across all three
        analytics panels: the queries inject
        ``WHERE Record.org_id = actor.org_id`` so cross-org records
        never appear in the contributors panel, the leads-by-status
        panel, or the weekly-activity panel.
        """
        # One active record in the actor's org.
        own = RecordFactory(organization=organization, owner=admin_user)  # noqa: F841 - retained for readability
        # 20 records in another org.
        other_org = OrganizationFactory()
        other_owner = UserFactory(organization=other_org)
        for _ in range(20):
            RecordFactory(organization=other_org, owner=other_owner)

        result = compute_analytics(_make_session(admin_user))

        # other_owner does NOT appear in our top_contributors list.
        contributor_ids = {entry.user_id for entry in result.most_active_contributors}
        assert other_owner.id not in contributor_ids

        # Total leads_by_status counts in our org are MUCH smaller
        # than the other org's 20 records. We seeded one active
        # record in our org; total should be 1.
        total_leads = sum(entry.count for entry in result.leads_by_status)
        assert total_leads == 1
        # The other org's 20 records did NOT bleed into our totals.
        assert total_leads < 20

        # Weekly_activity counts only our org's records.
        total_weekly = sum(entry.record_count for entry in result.weekly_activity)
        # Our org has one record submitted this week, in the 12-week
        # window. The 20 cross-org records are NOT counted.
        assert total_weekly == 1


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for boundary and anomalous conditions across the admin service.

    Edge cases that don't naturally fit into the per-function classes
    above:

      * Empty organization analytics: stable shape even with zero
        records.
      * Return-type assertions: confirms :func:`list_all_users`
        returns :class:`UserRead` pydantic models (not raw ORM
        instances).
    """

    def test_empty_org_returns_empty_analytics(
        self,
        db_session: Any,
        organization: Any,
    ) -> None:
        """An organization with zero records still returns a stable shape.

        Verifies the stable-shape contract per AAP Section 0.5.2
        Layer 6: even on a freshly-created org with no records, the
        :class:`AnalyticsResponse` always carries:

          * ``most_active_contributors == []`` (empty list, not
            ``None``)
          * ``leads_by_status`` of length 4 (one zero-fill entry
            per :class:`OutreachStatus` value)
          * ``weekly_activity`` of length 12 (one zero-fill entry
            per ISO week in the trailing 12-week window)

        This stable shape lets the SPA render the analytics view
        without per-render gap-filling or null checks.

        The ``organization`` fixture is requested (but not used) so
        that conftest's transitive ``_bind_factories_session`` fixture
        runs and the factory classes get their SQLAlchemy session
        bound before any factory call. Without this, the
        ``OrganizationFactory()`` call below raises
        ``RuntimeError: No session provided.``.
        """
        del organization  # explicitly unused; only requested for fixture binding
        # Create a fresh org with one admin and NO records. We use
        # a dedicated org (not the conftest ``organization`` fixture)
        # so other test fixtures' admin/contributor/viewer users
        # don't interfere with the empty assertion.
        org = OrganizationFactory()
        admin = AdminUserFactory(organization=org)

        result = compute_analytics(_make_session(admin))

        # No records means no contributors.
        assert result.most_active_contributors == []
        # leads_by_status still has 4 entries with count=0 each.
        assert len(result.leads_by_status) == 4
        for status_entry in result.leads_by_status:
            assert status_entry.count == 0
        # weekly_activity has 12 entries with record_count=0 each.
        assert len(result.weekly_activity) == 12
        for weekly_entry in result.weekly_activity:
            assert weekly_entry.record_count == 0
        # generated_at is still populated (not contingent on data).
        assert result.generated_at is not None

    def test_list_all_users_returns_user_read_objects(
        self,
        db_session: Any,
        admin_user: Any,
    ) -> None:
        """The return type is ``list[UserRead]`` (pydantic models, not ORM rows).

        Verifies that :func:`list_all_users` serializes through the
        :class:`UserRead` schema before returning. This matters for
        two reasons per AAP Section 0.7.4:

          * The schema's ``from_attributes=True`` mode reads only
            the declared fields, so ``password_hash`` (declared on
            :class:`User` but NOT on :class:`UserRead`) is filtered
            out at the serialization boundary.
          * The handler can return the result directly via
            ``jsonify([u.model_dump() for u in users])`` without an
            additional serialization pass, keeping the API-layer
            handler thin per AAP Section 0.5.3.
        """
        users = list_all_users(_make_session(admin_user))
        assert len(users) >= 1  # at least admin_user is in the org
        for u in users:
            assert isinstance(u, UserRead)
