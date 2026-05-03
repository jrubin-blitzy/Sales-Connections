"""Service-layer re-exports for the Sales-Connections backend.

Per AAP Section 0.2.3, this package consolidates the service modules
into a single import surface. The pattern mirrors
:mod:`app.models.__init__` and :mod:`app.schemas.__init__` so handlers,
middleware, and tests can write::

    from app.services import (
        emit_audit_event,
        find_duplicate,
        generate_outreach_notes,
        hash_password,
        verify_session_jwt,
    )

without knowing which submodule each function lives in.

Per AAP Section 0.5.3 ("service functions own transactions"), every
state-changing service function opens its own ``with session.begin():``
block and invokes :func:`emit_audit_event` inside that transaction so
the state change and the audit row commit (or roll back) atomically.
The re-exports here do not change that contract; they only make the
imports terser at the call site.

Per AAP Section 0.7.1 invariant 7 ("API-layer authorization is
authoritative"), this module deliberately re-exports authentication
helpers (``hash_password``, ``verify_password``, ``mint_session_jwt``,
``verify_session_jwt``) so the auth API blueprint and middleware can
reach them via a single import statement. The handlers themselves
remain thin per AAP Section 0.5.3.

This module deliberately has no module-level side effects beyond the
re-export statements. Importing :mod:`app.services` does not log,
does not make HTTP calls, does not touch the filesystem, and does
not connect to any database.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Admin aggregations (F-014)
# ---------------------------------------------------------------------------
# ``get_analytics_snapshot`` produces the three-panel analytics payload
# (most active contributors, leads by status, weekly activity sparkline)
# consumed by the admin dashboard. ``hard_delete_record`` permanently
# removes a record (admin-only). ``update_user_role`` mutates a user's
# role and emits the corresponding ``role_change`` audit event.
from app.services.admin import (
    LastAdminError,
    SelfDemotionError,
    get_analytics_snapshot,
    hard_delete_record,
    list_org_users,
    update_user_role,
)

# ---------------------------------------------------------------------------
# AI orchestration (F-002)
# ---------------------------------------------------------------------------
# ``generate_outreach_notes`` is the SOLE caller of the langchain +
# Anthropic SDK across the entire backend, preserving the
# provider-replaceability invariant per AAP Section 0.4.4.
# ``AIServiceUnavailableError`` is the AppError subclass surfaced on
# timeout / provider error / misconfiguration.
from app.services.ai_orchestration import (
    AIServiceUnavailableError,
    generate_outreach_notes,
)

# ---------------------------------------------------------------------------
# Audit emitter (F-013)
# ---------------------------------------------------------------------------
# ``emit_audit_event`` is the SOLE writer of the ``audit_events`` table
# per AAP Section 0.7.1 invariant 5 (append-only) and invariant 6
# (atomic state-change + audit pair). ``AuditEmissionError`` is the
# AppError subclass raised on caller misuse (no active transaction,
# malformed argument types, or INSERT failure).
from app.services.audit import (
    AuditEmissionError,
    emit_audit_event,
)

# ---------------------------------------------------------------------------
# Authentication services (F-012)
# ---------------------------------------------------------------------------
# ``hash_password`` / ``verify_password`` wrap bcrypt 4.x with the
# project's cost-12 production setting (cost-4 in TestingConfig).
# ``mint_session_jwt`` / ``verify_session_jwt`` produce and validate
# the HS256 8-hour session token delivered to the SPA via HttpOnly
# cookie. ``upsert_oauth_user`` is the OAuth-callback path's user
# row creator/updater. ``authenticate_password`` is the email/password
# login flow's verification entry point.
from app.services.auth import (
    AuthenticationError,
    authenticate_password,
    hash_password,
    mint_session_jwt,
    upsert_oauth_user,
    verify_password,
    verify_session_jwt,
)

# ---------------------------------------------------------------------------
# Connection record service (F-001, F-004, F-005, F-007, F-011)
# ---------------------------------------------------------------------------
# This module owns the entire lifecycle of ``records`` and
# ``record_tags``:
#
# * ``create_record`` is the sole writer of new ``records`` rows
#   (F-001), opening its own ``with session.begin():`` block and
#   emitting the corresponding ``CREATE`` audit event in the same
#   transaction per AAP Section 0.7.1 invariant 6 (atomic
#   state-change + audit pair).
# * ``get_record`` and ``list_records`` are the read paths backing
#   the F-004 feed and the F-011 detail view; both inject the
#   org-scope and soft-delete-scope predicates uniformly.
# * ``update_record`` (F-007 edit), ``update_status`` (F-005
#   outreach-status mutation), and ``soft_delete_record`` (F-007
#   soft delete) are the three state-changing mutation paths
#   beyond CREATE; each emits its own typed audit event in the
#   parent transaction.
# * ``get_record_history`` powers the F-011 edit-history feed by
#   surfacing audit events filtered to a single record id.
# * ``ConnectionFilters`` is the frozen dataclass carrying the
#   seven optional filter parameters consumed by the feed query.
# * ``DuplicateRecordError`` is the AppError subclass raised when
#   the unique partial index on ``normalized_linkedin_url`` fires
#   (mapped to HTTP 409).
from app.services.connections import (
    ConnectionFilters,
    DuplicateRecordError,
    create_record,
    get_record,
    get_record_history,
    list_records,
    soft_delete_record,
    update_record,
    update_status,
)

# ---------------------------------------------------------------------------
# Duplicate detection (F-010)
# ---------------------------------------------------------------------------
# ``find_duplicate`` queries the unique partial index
# ``uq_records_org_normalized_linkedin_url_active`` to detect a
# pre-existing record with the same normalized LinkedIn URL within the
# same org. Per AAP Section 0.7.6 this returns a non-blocking warning,
# never a hard reject.
from app.services.duplicate_detection import find_duplicate

__all__ = [
    "AIServiceUnavailableError",
    "AuditEmissionError",
    "AuthenticationError",
    "ConnectionFilters",
    "DuplicateRecordError",
    "LastAdminError",
    "SelfDemotionError",
    "authenticate_password",
    "create_record",
    "emit_audit_event",
    "find_duplicate",
    "generate_outreach_notes",
    "get_analytics_snapshot",
    "get_record",
    "get_record_history",
    "hard_delete_record",
    "hash_password",
    "list_org_users",
    "list_records",
    "mint_session_jwt",
    "soft_delete_record",
    "update_record",
    "update_status",
    "update_user_role",
    "upsert_oauth_user",
    "verify_password",
    "verify_session_jwt",
]
