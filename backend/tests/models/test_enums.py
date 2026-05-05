"""Tests for the SQLAlchemy enum types in ``app.models.enums``.

The Python enum values must exactly match the PostgreSQL enum types
defined in ``backend/migrations/versions/0001_initial_schema.py`` and
the Zod schemas in the frontend. These tests pin the values so any
divergence is caught at test time.
"""

from __future__ import annotations

from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)

# ---------------------------------------------------------------------------
# UserRole
# ---------------------------------------------------------------------------


class TestUserRole:
    """UserRole enum: 3 roles for F-009 RBAC."""

    def test_admin_value(self) -> None:
        assert UserRole.ADMIN.value == "Admin"

    def test_contributor_value(self) -> None:
        assert UserRole.CONTRIBUTOR.value == "Contributor"

    def test_viewer_value(self) -> None:
        assert UserRole.VIEWER.value == "Viewer"

    def test_exactly_three_members(self) -> None:
        assert len(list(UserRole)) == 3

    def test_values_match_documented_set(self) -> None:
        values = {role.value for role in UserRole}
        assert values == {"Admin", "Contributor", "Viewer"}

    def test_is_string_subclass(self) -> None:
        """UserRole must be (str, Enum) so frozen dataclasses accept it."""
        assert isinstance(UserRole.ADMIN, str)


# ---------------------------------------------------------------------------
# InvolvementType
# ---------------------------------------------------------------------------


class TestInvolvementType:
    """InvolvementType enum: 3 values for F-003."""

    def test_warm_intro_value(self) -> None:
        assert InvolvementType.WARM_INTRO.value == "Warm Intro"

    def test_soft_reference_value(self) -> None:
        assert InvolvementType.SOFT_REFERENCE.value == "Soft Reference"

    def test_target_only_value(self) -> None:
        assert InvolvementType.TARGET_ONLY.value == "Target Only"

    def test_exactly_three_members(self) -> None:
        assert len(list(InvolvementType)) == 3

    def test_values_match_documented_set(self) -> None:
        values = {iv.value for iv in InvolvementType}
        assert values == {"Warm Intro", "Soft Reference", "Target Only"}


# ---------------------------------------------------------------------------
# OutreachStatus
# ---------------------------------------------------------------------------


class TestOutreachStatus:
    """OutreachStatus enum: 4 values for F-005."""

    def test_not_started_value(self) -> None:
        assert OutreachStatus.NOT_STARTED.value == "Not Started"

    def test_in_progress_value(self) -> None:
        assert OutreachStatus.IN_PROGRESS.value == "In Progress"

    def test_contacted_value(self) -> None:
        assert OutreachStatus.CONTACTED.value == "Contacted"

    def test_closed_value(self) -> None:
        assert OutreachStatus.CLOSED.value == "Closed"

    def test_exactly_four_members(self) -> None:
        assert len(list(OutreachStatus)) == 4

    def test_values_match_documented_set(self) -> None:
        values = {st.value for st in OutreachStatus}
        assert values == {
            "Not Started",
            "In Progress",
            "Contacted",
            "Closed",
        }


# ---------------------------------------------------------------------------
# AuditEventType
# ---------------------------------------------------------------------------


class TestAuditEventType:
    """AuditEventType enum: 8 values for F-013."""

    def test_create_value(self) -> None:
        assert AuditEventType.CREATE.value == "create"

    def test_status_change_value(self) -> None:
        assert AuditEventType.STATUS_CHANGE.value == "status_change"

    def test_edit_value(self) -> None:
        assert AuditEventType.EDIT.value == "edit"

    def test_soft_delete_value(self) -> None:
        assert AuditEventType.SOFT_DELETE.value == "soft_delete"

    def test_hard_delete_value(self) -> None:
        assert AuditEventType.HARD_DELETE.value == "hard_delete"

    def test_role_change_value(self) -> None:
        assert AuditEventType.ROLE_CHANGE.value == "role_change"

    def test_authentication_value(self) -> None:
        assert AuditEventType.AUTHENTICATION.value == "authentication"

    def test_admin_op_value(self) -> None:
        assert AuditEventType.ADMIN_OP.value == "admin_op"

    def test_exactly_eight_members(self) -> None:
        assert len(list(AuditEventType)) == 8

    def test_values_match_documented_set(self) -> None:
        values = {evt.value for evt in AuditEventType}
        assert values == {
            "create",
            "status_change",
            "edit",
            "soft_delete",
            "hard_delete",
            "role_change",
            "authentication",
            "admin_op",
        }
