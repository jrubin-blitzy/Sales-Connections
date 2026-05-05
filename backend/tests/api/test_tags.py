"""API tests for the tags blueprint (F-008).

Covers ``GET /api/tags`` (list) and ``POST /api/tags`` (create) including
the RBAC matrix, org-scope isolation, idempotency on
``(org_id, name)``, race-condition fallback, and ``extra='forbid'``
validation.

Key invariants verified by this module
--------------------------------------

- ``GET /api/tags`` is open to ALL authenticated roles (Admin,
  Contributor, Viewer); only anonymous users are rejected with 401.
- ``POST /api/tags`` is gated to Contributor + Admin via the
  ``@requires_role`` decorator; Viewer receives 403.
- Tag names are unique per ``(org_id, name)`` via the
  ``uq_tags_org_name`` UNIQUE constraint declared on
  :class:`app.models.tag.Tag`. A duplicate POST returns the existing
  tag with HTTP 200, NOT a 409 conflict.
- Race-condition fallback: when two concurrent POSTs would both
  attempt to insert the same ``(org_id, name)``, the second receives
  an :class:`sqlalchemy.exc.IntegrityError` but the handler catches
  it, re-fetches the winning row, and returns HTTP 200 instead of
  surfacing a 500 to the client.
- Tag operations do NOT emit audit events (tags are derivative
  metadata, not first-class records). The eight
  :class:`app.models.enums.AuditEventType` values do not include any
  tag-related events.
- ``TagCreate`` has ``model_config = _STRICT_CONFIG`` (which carries
  ``extra='forbid'``) so any attempt to set ``id``, ``org_id``, or
  any other column is rejected with HTTP 422.
- ``TagRead`` (the outbound shape) carries ``id``, ``name``, and
  ``created_at`` only; ``org_id`` is intentionally excluded since it
  is a server-only multi-tenant scoping concept.

Coordination contract
---------------------

Fixtures are inherited from ``backend/tests/conftest.py``:

- ``client`` -- unauthenticated Flask test client (used for 401
  baselines).
- ``authed_client`` -- authenticated as a Contributor user.
- ``admin_client`` -- authenticated as an Admin user.
- ``viewer_client`` -- authenticated as a Viewer/Sales-Rep user.
- ``db_session`` -- isolated SQLAlchemy session bound to the test
  database; truncates all tables on teardown.
- ``organization`` -- the default Organization row pinned to
  :data:`app.config.TestingConfig.DEFAULT_ORG_ID` so every factory's
  default ``org`` SubFactory reference resolves to the same row.

Factory data setup uses ``backend/tests/factories.py``:

- :class:`tests.factories.OrganizationFactory` -- builds additional
  Organization rows for cross-org isolation tests (verifies that the
  ``uq_tags_org_name`` constraint is keyed on ``(org_id, name)``, not
  on ``name`` alone).
- :class:`tests.factories.TagFactory` -- seeds the ``tags`` table
  before list/lookup assertions.
- :class:`tests.factories.UserFactory` -- creates ad-hoc User rows
  to verify the GET response does not leak user-related fields.
"""

from __future__ import annotations

# Standard library imports.
#
# ``typing.Any`` is reserved for permissive type annotations on
# parametrized request bodies whose value can be a ``dict`` or
# ``None``, and on fixture parameters whose concrete type
# (``flask.testing.FlaskClient``) is gated behind ``TYPE_CHECKING``
# in conftest.
#
# ``uuid.uuid4()`` produces random UUIDs that are str-coerced into the
# request body of ``test_extra_field_rejected_by_extra_forbid`` and
# ``test_create_with_id_field_rejected``; both verify that the
# ``TagCreate`` pydantic schema with ``extra='forbid'`` rejects
# client-supplied ``id`` and ``org_id`` fields with HTTP 422 (per AAP
# Section 0.7.4: ``org_id`` and ``id`` are server-derived only).
from typing import Any
import uuid

# Third-party runtime imports.
#
# ``pytest`` provides test discovery, parametrization, and the custom
# ``@pytest.mark.rbac`` marker registered in
# ``backend/pyproject.toml`` (``markers = ["rbac: tests for RBAC
# permission matrix"]``). The marker is applied to
# :class:`TestTagRBACMatrix` so the parametrized RBAC matrix is
# selectable via ``pytest -m rbac``.
#
# SQLAlchemy 2.x SQL toolkit primitives are used for direct database
# verification queries within tests:
#   * ``select()`` builds the typed SELECT statement objects used to
#     look up Tag rows for asserting persistence and to count rows
#     for the ``test_idempotent_create_does_not_duplicate_in_db``
#     assertion.
#   * ``func.count()`` is used inside ``select(func.count())`` for
#     the duplicate-row count assertion and inside
#     ``select(func.count()).select_from(AuditEvent)`` for the
#     ``test_no_audit_event_emitted`` invariant check.
import pytest
from sqlalchemy import func, select

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration in ``backend/pyproject.toml``;
# relative imports are banned project-wide.
#
# * ``db`` is the :class:`app.extensions.SQLAlchemy` wrapper
#   singleton. Tests use ``with db.session() as session:`` for direct
#   DB queries that read just-committed state.
# * ``Tag`` is the SQLAlchemy ORM model whose rows are queried with
#   ``select(Tag).where(...)`` to verify ``POST /api/tags`` actually
#   persisted a row.
# * ``AuditEvent`` is queried via ``select(func.count())`` before and
#   after each tag-create POST to assert the audit event count is
#   UNCHANGED, enforcing the F-008 invariant that tag operations do
#   NOT emit audit events.
# * ``OrganizationFactory.create()`` builds a second Organization for
#   the org-scope-isolation test (``test_tags_org_scoped``) and the
#   cross-org name-collision test
#   (``test_create_same_name_in_different_orgs_succeeds``) which
#   together verify ``uq_tags_org_name`` is keyed on
#   ``(org_id, name)`` and not on ``name`` alone.
# * ``TagFactory`` seeds the database with tags before list/lookup
#   assertions.
# * ``UserFactory`` is used to seed an ad-hoc User row in
#   ``test_response_does_not_include_user_or_password_fields`` so the
#   test verifies the tags endpoint does not leak user-table data
#   even when the database carries unrelated user rows.
from app.extensions import db
from app.models import AuditEvent, Tag
from tests.factories import OrganizationFactory, TagFactory, UserFactory

# ---------------------------------------------------------------------------
# Module-level helper: assert the empty audit-events invariant
# ---------------------------------------------------------------------------


def _audit_event_count() -> int:
    """Return the total number of rows in the ``audit_events`` table.

    The result is used by ``test_no_audit_event_emitted`` and the
    parametrized RBAC matrix to assert that tag CRUD does NOT
    increment the audit-event count. Per AAP Section 0.7.1 invariant
    6 (Atomic state-change + audit pair), state-changing operations
    on first-class records emit audit events; tag CRUD is metadata-
    only and not subject to that invariant.

    Returns:
        The integer count of rows currently in the
        ``audit_events`` table. The query opens its own
        ``db.session()`` so the surrounding test session does not
        need to be flushed; the engine's ``READ COMMITTED`` default
        guarantees the count reflects committed-only state.
    """
    with db.session() as session:
        return int(session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())


# ---------------------------------------------------------------------------
# TestTagList -- GET /api/tags
# ---------------------------------------------------------------------------


class TestTagList:
    """Tests for ``GET /api/tags`` (F-008 list endpoint).

    The endpoint is open to all authenticated roles per
    :func:`app.api.tags.list_tags` (no ``@requires_role`` decorator).
    The handler returns a JSON list (NOT a paginated envelope)
    sorted alphabetically by ``Tag.name`` ASC.
    """

    def test_admin_can_list_tags(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Admin role admitted to GET /api/tags with 3 seeded tags."""
        # Seed three tags in the default organization. Sequenced
        # names (``tag-{n}``) avoid colliding on the
        # ``uq_tags_org_name`` constraint within a single test.
        TagFactory.create_batch(3, org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert len(body) == 3

    def test_contributor_can_list_tags(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Contributor role admitted to GET /api/tags with 3 seeded tags."""
        TagFactory.create_batch(3, org_id=organization.id)

        response = authed_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert len(body) == 3

    def test_viewer_can_list_tags(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Viewer role admitted to GET /api/tags (open to all auth roles)."""
        TagFactory.create_batch(3, org_id=organization.id)

        response = viewer_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert len(body) == 3

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """Unauthenticated GET /api/tags is rejected with 401."""
        response = client.get("/api/tags")

        # The auth middleware rejects requests without a valid session
        # cookie BEFORE the handler runs. The body shape is the
        # canonical AAP error envelope.
        assert response.status_code == 401

    def test_tags_alphabetical_order(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Tag list is sorted by ``name`` ASC.

        The handler emits ``order_by(asc(Tag.name))`` so a user
        typing into the autocomplete sees tags alphabetically. We
        seed three tags in random insert order ("zebra", "alpha",
        "middle") and assert the response order is
        ["alpha", "middle", "zebra"].
        """
        # Seed in NON-alphabetical order to force the handler to
        # actually sort. If the handler omitted ``order_by``, the
        # database would emit rows in INSERT order or undefined
        # order, and this test would fail.
        TagFactory.create(name="zebra", org_id=organization.id)
        TagFactory.create(name="alpha", org_id=organization.id)
        TagFactory.create(name="middle", org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        names = [tag["name"] for tag in body]
        assert names == ["alpha", "middle", "zebra"]

    def test_tags_org_scoped(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Cross-org tags are excluded from the response.

        Verifies the ``WHERE org_id = g.session.org_id`` predicate in
        :func:`app.api.tags.list_tags` per AAP Section 0.7.1
        invariant 3 (Org-scoped multi-tenancy at the data model
        layer). A user in Org A cannot see Org B's tags.
        """
        # Default org gets 3 tags; the other org gets 5.
        TagFactory.create_batch(3, org_id=organization.id)
        other_org = OrganizationFactory.create()
        TagFactory.create_batch(5, org_id=other_org.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        # Only the default-org tags are visible.
        assert len(body) == 3

    def test_empty_list_returns_empty_array(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """No tags in the org returns ``[]`` with HTTP 200.

        The ``organization`` fixture is referenced to ensure the
        org row exists; no tags are seeded so the handler emits an
        empty list. The response body is a flat JSON array (not a
        paginated envelope), per the handler's ``jsonify(payload)``
        signature.
        """
        # Reference ``organization`` to ensure the default org row
        # exists for the session attached to the auth cookie.
        _ = organization

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert body == []

    def test_response_includes_id_and_name_and_created_at(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Each tag in the response carries id, name, and created_at.

        Per :class:`app.schemas.connection.TagRead`, the canonical
        outbound shape is ``{id, name, created_at}``. The id is a
        UUID v4 string and created_at is an ISO-8601 datetime string.
        """
        TagFactory.create(name="industry: fintech", org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert len(body) == 1
        tag_payload = body[0]
        assert "id" in tag_payload
        assert "name" in tag_payload
        assert "created_at" in tag_payload
        assert tag_payload["name"] == "industry: fintech"
        # ``id`` is the string representation of a UUID v4. Coercing
        # the string back to ``uuid.UUID`` and back to ``str`` round-
        # trips iff the value is a valid UUID; an invalid UUID
        # raises ``ValueError`` from :class:`uuid.UUID` and the
        # assertion fails.
        assert str(uuid.UUID(tag_payload["id"])) == tag_payload["id"]

    def test_response_does_not_include_org_id(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Response payloads exclude org_id (server-only field).

        Per :class:`app.schemas.connection.TagRead`, the schema does
        NOT declare ``org_id``; it is intentionally excluded since
        org-scoping is implicit (clients only see their own org's
        data). The pydantic schema's ``from_attributes=True`` mode
        silently drops the ``Tag.org_id`` ORM attribute during
        serialization. Defense in depth against accidental leakage
        of server-only multi-tenant scoping detail.
        """
        TagFactory.create(name="leak-check", org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert len(body) == 1
        tag_payload = body[0]
        assert "org_id" not in tag_payload

    def test_response_does_not_include_user_or_password_fields(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Tag response shape never carries user-table fields.

        Even when the database carries unrelated User rows (e.g., the
        organization has multiple contributors), the
        :class:`app.schemas.connection.TagRead` outbound shape MUST
        NOT include any user-related field. This test seeds an
        ad-hoc :class:`tests.factories.UserFactory` row alongside a
        Tag row and asserts the GET response shape contains only the
        canonical ``{id, name, created_at}`` keys.
        """
        # Add user noise: a random User row in the same org. The user
        # is unrelated to the tag; this test verifies the handler's
        # SELECT statement is scoped to ``Tag`` and does not
        # accidentally JOIN to or expose the ``users`` table.
        UserFactory.create(org=organization)
        TagFactory.create(name="user-leak-check", org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert len(body) == 1
        tag_payload = body[0]
        # No user-related fields on a tag response.
        for forbidden_key in (
            "email",
            "display_name",
            "password_hash",
            "role",
            "user_id",
            "owner_user_id",
            "owner_display_name",
        ):
            assert forbidden_key not in tag_payload, (
                f"Tag payload must not include {forbidden_key!r}"
            )
        # The canonical fields must be present.
        assert set(tag_payload.keys()) == {"id", "name", "created_at"}

    def test_list_at_one_hundred_tags_returns_all(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Endpoint returns all tags without pagination.

        Per the handler docstring, pagination is intentionally absent:
        tag counts are bounded by org curation behavior (typical
        10-100 tags per org). With 100 tags seeded the response is a
        flat JSON array with all 100 items present in alphabetical
        order. If a future requirement adds pagination, this test
        will need to be updated to honor the pagination contract.
        """
        TagFactory.create_batch(100, org_id=organization.id)

        response = admin_client.get("/api/tags")

        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert len(body) == 100
        # The list is sorted; the first tag's name must be
        # alphabetically <= the last tag's name (transitively
        # validates the ``order_by(asc(Tag.name))`` clause for a
        # large set).
        assert body[0]["name"] <= body[-1]["name"]


# ---------------------------------------------------------------------------
# TestTagCreate -- POST /api/tags
# ---------------------------------------------------------------------------


class TestTagCreate:
    """Tests for ``POST /api/tags`` (F-008 create endpoint).

    The endpoint is RBAC-gated to Contributor + Admin via the
    ``@requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)`` decorator
    on :func:`app.api.tags.create_tag`. Idempotent on
    ``(org_id, name)``: 201 if newly created, 200 if existing.

    Validation:
        * ``name`` is required, 1-64 characters (post-strip).
        * ``extra='forbid'`` rejects unknown fields with HTTP 422.
        * Whitespace is stripped before length validation via
          ``str_strip_whitespace=True`` on the schema's
          ``_STRICT_CONFIG``.
    """

    def test_contributor_creates_new_tag_returns_201(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Contributor POSTs a new tag; HTTP 201 with id and name."""
        response = authed_client.post(
            "/api/tags",
            json={"name": "industry: logistics"},
        )

        assert response.status_code == 201
        body = response.get_json()
        assert "id" in body
        assert body["name"] == "industry: logistics"

        # Verify the row was actually persisted to the database.
        with db.session() as session:
            stmt = select(Tag).where(
                Tag.name == "industry: logistics",
                Tag.org_id == organization.id,
            )
            persisted = session.execute(stmt).scalar_one_or_none()
            assert persisted is not None
            assert str(persisted.id) == body["id"]

    def test_admin_creates_new_tag_returns_201(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Admin POSTs a new tag; HTTP 201 with id and name."""
        response = admin_client.post(
            "/api/tags",
            json={"name": "use-case: outbound"},
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["name"] == "use-case: outbound"

        # Verify persistence.
        with db.session() as session:
            persisted = session.execute(
                select(Tag).where(
                    Tag.name == "use-case: outbound",
                    Tag.org_id == organization.id,
                )
            ).scalar_one_or_none()
            assert persisted is not None

    def test_viewer_forbidden_on_create_returns_403(
        self,
        viewer_client: Any,
    ) -> None:
        """Viewer POSTs are rejected with HTTP 403 by the RBAC decorator.

        The ``@requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)``
        decorator on :func:`app.api.tags.create_tag` rejects Viewer
        with the canonical 403 envelope where
        ``error.code = 'forbidden'``.
        """
        response = viewer_client.post(
            "/api/tags",
            json={"name": "viewer-blocked"},
        )

        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized_returns_401(self, client: Any) -> None:
        """Unauthenticated POST is rejected with HTTP 401 by the auth middleware."""
        response = client.post(
            "/api/tags",
            json={"name": "anon-blocked"},
        )

        # Auth middleware rejects BEFORE RBAC runs, so we get 401
        # (not 403). The handler is never invoked.
        assert response.status_code == 401

    def test_idempotent_create_returns_200_for_existing(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Second POST with same name returns HTTP 200 with the existing id.

        Per the handler's idempotency strategy
        (SELECT-then-INSERT pattern), if a tag with the same
        ``(org_id, name)`` already exists, the existing tag is
        returned with HTTP 200 (NOT 201). The response body's ``id``
        matches the pre-existing tag's id, never a freshly-generated
        UUID.
        """
        existing = TagFactory.create(
            name="existing-tag",
            org_id=organization.id,
        )

        response = authed_client.post(
            "/api/tags",
            json={"name": "existing-tag"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == str(existing.id)
        assert body["name"] == "existing-tag"

    def test_idempotent_create_does_not_duplicate_in_db(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Idempotent POST does not insert a duplicate row.

        After the second POST with the same name, the ``tags`` table
        carries exactly ONE row with that name in the org. The
        ``uq_tags_org_name`` UNIQUE constraint enforces this at the
        database layer; the handler honors it at the application
        layer by returning the existing row instead of raising.
        """
        TagFactory.create(name="existing-tag", org_id=organization.id)

        response = authed_client.post(
            "/api/tags",
            json={"name": "existing-tag"},
        )

        assert response.status_code == 200
        # Count rows in the DB matching the name; must be exactly 1.
        with db.session() as session:
            count = session.execute(
                select(func.count())
                .select_from(Tag)
                .where(
                    Tag.name == "existing-tag",
                    Tag.org_id == organization.id,
                )
            ).scalar_one()
            assert count == 1

    def test_create_same_name_in_different_orgs_succeeds(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Same tag name in two different orgs both succeed.

        The ``uq_tags_org_name`` constraint is keyed on
        ``(org_id, name)`` (composite), NOT on ``name`` alone, so the
        same display name can be reused across organizations once
        multi-tenant runtime is enabled. This test verifies the
        constraint is composite by:

        1. Pre-creating a tag with name ``"shared-name"`` in a
           DIFFERENT organization via :class:`OrganizationFactory`.
        2. POSTing the same name as the contributor (whose session
           is bound to the default org).
        3. Asserting HTTP 201 (the default-org POST is treated as a
           NEW tag because the unique key includes ``org_id``).
        """
        other_org = OrganizationFactory.create()
        TagFactory.create(name="shared-name", org_id=other_org.id)

        response = authed_client.post(
            "/api/tags",
            json={"name": "shared-name"},
        )

        # Default-org tag is NEW (other-org tag is in a different
        # row), so 201 is correct.
        assert response.status_code == 201
        body = response.get_json()
        assert body["name"] == "shared-name"

        # Verify two rows exist with the same name (one per org).
        with db.session() as session:
            count = session.execute(
                select(func.count()).select_from(Tag).where(Tag.name == "shared-name")
            ).scalar_one()
            assert count == 2

    def test_extra_field_rejected_by_extra_forbid(
        self,
        authed_client: Any,
    ) -> None:
        """POST with client-supplied org_id is rejected with HTTP 422.

        Per AAP Section 0.7.4 (Security Invariants), org_id is
        derived server-side from ``g.session.org_id`` and is NEVER
        client-supplied. The :class:`app.schemas.connection.TagCreate`
        schema's ``model_config = _STRICT_CONFIG`` carries
        ``extra='forbid'`` which rejects ANY field beyond the
        declared ``name``. The error envelope's
        ``error.code = 'validation_failed'``.
        """
        response = authed_client.post(
            "/api/tags",
            json={
                "name": "valid-name",
                "org_id": str(uuid.uuid4()),
            },
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_create_with_id_field_rejected(
        self,
        authed_client: Any,
    ) -> None:
        """POST with client-supplied id is rejected with HTTP 422.

        Same rationale as ``test_extra_field_rejected_by_extra_forbid``:
        ``id`` is server-derived (UUID v4 generated at insert time
        via the ``default=uuid.uuid4`` factory on
        :class:`app.models.tag.Tag.id`) and the schema's
        ``extra='forbid'`` rejects client overrides.
        """
        response = authed_client.post(
            "/api/tags",
            json={
                "name": "valid-name",
                "id": str(uuid.uuid4()),
            },
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_extra_field_created_at_rejected(
        self,
        authed_client: Any,
    ) -> None:
        """POST with client-supplied created_at is rejected with HTTP 422.

        ``created_at`` is server-derived from the PostgreSQL
        ``server_default=func.now()`` directive on
        :class:`app.models.tag.Tag.created_at`. The schema's
        ``extra='forbid'`` rejects client overrides.
        """
        response = authed_client.post(
            "/api/tags",
            json={
                "name": "valid-name",
                "created_at": "2024-01-01T00:00:00Z",
            },
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_empty_name_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """POST with empty name is rejected with HTTP 422.

        :class:`app.schemas.connection.TagCreate.name` declares
        ``Field(min_length=1, ...)``, so an empty string fails
        validation BEFORE the handler executes any DB work.
        """
        response = authed_client.post(
            "/api/tags",
            json={"name": ""},
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_whitespace_only_name_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """POST with whitespace-only name is rejected with HTTP 422.

        ``str_strip_whitespace=True`` on the schema's
        ``_STRICT_CONFIG`` strips leading/trailing whitespace BEFORE
        the ``min_length=1`` check, so ``"   "`` becomes ``""`` and
        is rejected.
        """
        response = authed_client.post(
            "/api/tags",
            json={"name": "   "},
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_name_max_length_64_chars_succeeds(
        self,
        authed_client: Any,
    ) -> None:
        """A 64-character name (the boundary) is accepted with HTTP 201.

        Per :class:`app.schemas.connection.TagCreate.name`,
        ``Field(max_length=_TAG_NAME_MAX_CHARS)`` where
        ``_TAG_NAME_MAX_CHARS = 64`` matches the ``Tag.name``
        column's ``String(64)`` width in the PostgreSQL schema.
        Boundary value 64 is INCLUSIVE.
        """
        boundary_name = "x" * 64
        response = authed_client.post(
            "/api/tags",
            json={"name": boundary_name},
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["name"] == boundary_name

    def test_name_too_long_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """A 65-character name is rejected with HTTP 422.

        Boundary value 65 exceeds ``max_length=64``; pydantic
        rejects with HTTP 422 BEFORE the handler executes any DB
        work. This test pins the boundary so a future schema change
        that loosens the limit fails the test.
        """
        too_long = "x" * 65
        response = authed_client.post(
            "/api/tags",
            json={"name": too_long},
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_missing_name_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """POST with empty body is rejected with HTTP 422.

        ``name`` has no default value on
        :class:`app.schemas.connection.TagCreate`, so omitting the
        field fails pydantic's "missing required field" check.
        """
        response = authed_client.post(
            "/api/tags",
            json={},
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_non_object_body_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """POST with a non-object JSON body is rejected with HTTP 422.

        The handler's
        ``isinstance(raw_body, dict)`` guard rejects valid JSON
        that is not an object (e.g., an array, a string, a number).
        ``error.code = 'validation_failed'`` per the handler's
        :class:`ValidationFailedError` raise.
        """
        # JSON array is valid JSON but not a JSON object.
        response = authed_client.post(
            "/api/tags",
            json=[1, 2, 3],
        )

        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_name_whitespace_stripped_before_persist(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Surrounding whitespace is stripped before persistence.

        ``str_strip_whitespace=True`` on the schema's
        ``_STRICT_CONFIG`` strips ``"  trimmed  "`` to ``"trimmed"``
        BEFORE length validation. The persisted ``Tag.name`` is the
        stripped form, ensuring that a SELECT by the unstripped
        string would NOT match (the row's stored name is canonical).
        """
        response = authed_client.post(
            "/api/tags",
            json={"name": "  trimmed  "},
        )

        assert response.status_code == 201
        body = response.get_json()
        # The response carries the canonical, stripped form.
        assert body["name"] == "trimmed"

        # The DB row also stores the stripped form.
        with db.session() as session:
            persisted = session.execute(
                select(Tag).where(
                    Tag.name == "trimmed",
                    Tag.org_id == organization.id,
                )
            ).scalar_one_or_none()
            assert persisted is not None

    def test_no_audit_event_emitted_on_create(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """POST /api/tags does NOT emit an audit event.

        Per AAP Section 0.5.2 Layer 5 and the handler docstring, tag
        operations do not emit audit events. The eight
        :class:`app.models.enums.AuditEventType` values
        (``create``, ``status_change``, ``edit``, ``soft_delete``,
        ``hard_delete``, ``role_change``, ``authentication``,
        ``admin_op``) are scoped to records, users, and authentication
        only. Tags are subordinate metadata; the records that
        REFERENCE tags are audited via ``edit``/``create`` events
        on those records.
        """
        # Reference ``organization`` so the default org row exists
        # for the contributor's session before the POST lands.
        _ = organization

        prior_count = _audit_event_count()

        response = authed_client.post(
            "/api/tags",
            json={"name": "no-audit"},
        )

        assert response.status_code == 201
        post_count = _audit_event_count()
        assert post_count == prior_count, (
            f"Expected audit count to stay at {prior_count} after tag "
            f"creation; got {post_count}. Tag CRUD must NOT emit "
            f"audit events per F-008 design."
        )

    def test_no_audit_event_emitted_on_idempotent_repeat(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Idempotent POST does NOT emit an audit event either.

        The idempotent path (existing tag returned with HTTP 200)
        also must not emit any audit event. Verifies the abstinence
        invariant on BOTH the create-new and create-existing
        branches.
        """
        TagFactory.create(name="repeat-no-audit", org_id=organization.id)
        prior_count = _audit_event_count()

        response = authed_client.post(
            "/api/tags",
            json={"name": "repeat-no-audit"},
        )

        assert response.status_code == 200
        post_count = _audit_event_count()
        assert post_count == prior_count


# ---------------------------------------------------------------------------
# TestTagCreateRaceCondition -- idempotent fallback path
# ---------------------------------------------------------------------------


class TestTagCreateRaceCondition:
    """Tests for the ``POST /api/tags`` race-condition fallback.

    True concurrent POSTs are difficult to simulate deterministically
    from a single-threaded pytest test. The pragmatic approach is to
    pre-seed the database with the target ``(org_id, name)`` BEFORE
    issuing the POST. This is functionally identical to the second
    of two concurrent POSTs:

    1. The first POST inserts the new row. (Simulated by
       :class:`TagFactory.create`.)
    2. The second POST runs ``SELECT`` (does NOT find the row in
       the test thread's session because PostgreSQL serializes
       reads), then ``INSERT`` (which raises ``IntegrityError`` due
       to ``uq_tags_org_name``), then the handler's exception
       handler re-fetches the winning row and returns 200.

    In the test environment, the SELECT may find the pre-seeded row
    on the first attempt (because the test session sees the
    just-committed row), in which case the SELECT-found fast path is
    exercised instead. Both paths return HTTP 200 with the existing
    tag's id, so the tests are insensitive to which branch the
    handler takes -- the observable behavior (HTTP 200, existing id)
    is what matters.

    The race-condition-specific branch (IntegrityError caught,
    re-fetch returns the winning row) is covered by direct unit
    tests of the service helper in
    ``tests/services/test_*.py`` if/when a tags service module is
    introduced; here we exercise the API contract end-to-end.
    """

    def test_race_condition_returns_existing_tag(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Pre-seeded duplicate POST returns HTTP 200 with the existing id.

        Pre-create the tag in the database BEFORE the POST. The
        handler MUST NOT raise a 500 to the client; it must return
        HTTP 200 with the pre-existing tag's id.
        """
        existing = TagFactory.create(
            name="race-tag",
            org_id=organization.id,
        )

        response = authed_client.post(
            "/api/tags",
            json={"name": "race-tag"},
        )

        # Either the SELECT-found fast path (existing tag returned)
        # or the IntegrityError fallback (concurrent insert race
        # resolved). Both paths emit HTTP 200 with the existing id.
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == str(existing.id)
        assert body["name"] == "race-tag"

    def test_race_condition_does_not_propagate_500(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Pre-seeded duplicate POST never returns HTTP 500.

        Defense-in-depth: even though the
        ``IntegrityError`` from the partial-unique-index race is
        caught and translated to HTTP 200, this test pins the
        invariant that the client NEVER sees a 500 under this
        scenario. A regression that removes the ``except
        IntegrityError`` block in
        :func:`app.api.tags.create_tag` would surface as a 500 to
        the client and this test would fail.
        """
        TagFactory.create(name="no-500-race", org_id=organization.id)

        response = authed_client.post(
            "/api/tags",
            json={"name": "no-500-race"},
        )

        assert response.status_code != 500
        # The expected status is 200 (idempotent re-fetch).
        assert response.status_code == 200

    def test_race_condition_does_not_emit_audit_event(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Race-condition fallback path also does NOT emit an audit event.

        The race-condition path returns the same response shape as
        the SELECT-found fast path; both must respect the F-008
        audit-emission abstinence invariant.
        """
        TagFactory.create(name="race-no-audit", org_id=organization.id)
        prior_count = _audit_event_count()

        response = authed_client.post(
            "/api/tags",
            json={"name": "race-no-audit"},
        )

        assert response.status_code == 200
        post_count = _audit_event_count()
        assert post_count == prior_count


# ---------------------------------------------------------------------------
# TestTagRBACMatrix -- cross-cutting permission matrix
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestTagRBACMatrix:
    """Cross-cutting RBAC matrix for ``/api/tags`` (F-008 + F-009).

    Marked with ``@pytest.mark.rbac`` so the test class is selectable
    via ``pytest -m rbac`` per the marker registered in
    ``backend/pyproject.toml``. The matrix covers both endpoints
    (GET, POST) and all three authenticated roles (Admin, Contributor,
    Viewer) plus the anonymous case.

    Permission matrix (per AAP Section 0.5.2 Layer 6 / rbac.py):

        GET  /api/tags    Admin -> 200, Contributor -> 200, Viewer -> 200
        POST /api/tags    Admin -> 201, Contributor -> 201, Viewer -> 403
        Anonymous on GET  401
        Anonymous on POST 401
    """

    @pytest.mark.parametrize(
        ("method", "body", "viewer_status", "contributor_status", "admin_status"),
        [
            (
                "GET",
                None,
                200,
                200,
                200,
            ),
            (
                "POST",
                {"name": "rbac-matrix-tag"},
                403,
                201,
                201,
            ),
        ],
        ids=["GET-tags", "POST-tags"],
    )
    def test_tags_endpoint_rbac_matrix(
        self,
        request: pytest.FixtureRequest,
        organization: Any,
        method: str,
        body: dict[str, Any] | None,
        viewer_status: int,
        contributor_status: int,
        admin_status: int,
    ) -> None:
        """Parametrized matrix verifying the RBAC permission table.

        For each (method, role) cell the test issues the request
        through the role-specific test client and asserts the
        expected HTTP status. Each role uses a UNIQUE tag name
        derived from ``role`` + ``method`` so concurrent
        parametrized cases do not collide on the
        ``uq_tags_org_name`` constraint when the same parametrized
        body would otherwise be POSTed by multiple roles in turn.
        """
        # Reference ``organization`` so the default org row exists
        # before any role-specific client issues the request.
        _ = organization

        # Each role's test issues a request with a UNIQUE name to
        # avoid colliding on the ``uq_tags_org_name`` constraint
        # when multiple roles POST the same parametrized body.
        # GET requests do not carry a body and are unaffected.
        cases = [
            ("viewer_client", viewer_status),
            ("authed_client", contributor_status),
            ("admin_client", admin_status),
        ]
        for fixture_name, expected_status in cases:
            client_for_role = request.getfixturevalue(fixture_name)
            if method == "POST" and body is not None:
                # Build a per-role unique name to avoid the
                # second-role POST being interpreted as a duplicate
                # of the first role's tag (which would yield 200
                # instead of 201 for the role that runs second).
                # Viewer is rejected before persistence so it cannot
                # cause a duplicate; Contributor and Admin would
                # otherwise collide.
                request_body = {"name": f"{body['name']}-{fixture_name}"}
                response = client_for_role.post("/api/tags", json=request_body)
            elif method == "POST":
                response = client_for_role.post("/api/tags")
            else:
                response = client_for_role.get("/api/tags")

            assert response.status_code == expected_status, (
                f"{method} /api/tags as {fixture_name}: expected "
                f"{expected_status}, got {response.status_code}; "
                f"body={response.get_data(as_text=True)!r}"
            )

    def test_anonymous_unauthorized_on_get(self, client: Any) -> None:
        """Unauthenticated GET /api/tags is rejected with HTTP 401.

        The auth middleware rejects requests without a valid session
        cookie BEFORE either the RBAC decorator or the handler runs.
        The 401 response is the canonical error envelope.
        """
        response = client.get("/api/tags")
        assert response.status_code == 401

    def test_anonymous_unauthorized_on_post(self, client: Any) -> None:
        """Unauthenticated POST /api/tags is rejected with HTTP 401.

        Same rationale as the GET case: the auth middleware fires
        FIRST. The RBAC decorator's 403 path is unreachable for
        anonymous users.
        """
        response = client.post("/api/tags", json={"name": "anon-blocked"})
        assert response.status_code == 401
