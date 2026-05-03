"""Sales-Connections business-logic (service) layer.

The service layer is the single place in the codebase that:

1. Opens and owns database transactions.
2. Calls external services (Anthropic Claude via Langchain, Google
   OAuth via Authlib).
3. Emits audit events for every state-changing operation.
4. Enforces business invariants beyond schema validation (org-scoping,
   soft-delete semantics, owner attribution, RBAC consequences).

Modules
-------
- ``audit``: :func:`emit_audit_event` is the sole writer of the
  ``audit_events`` table. State-mutating services call it inside their
  parent transaction so the state change and the audit row commit (or
  roll back) atomically per AAP Section 0.7.1 invariant 6.
- ``auth``: password hashing (bcrypt 4.x, cost-12 in production),
  PyJWT mint/verify of the HS256 8-hour session token, and Google
  OAuth user upsert. Powers F-012 across both the email/password
  fallback and the OAuth authorization-code flow.
- ``connections``: record CRUD, listing, soft-deletion, status
  mutation, and edit-history retrieval. Backs F-001, F-004, F-005,
  F-007, and F-011.
- ``ai_orchestration``: Langchain-wrapped Anthropic Claude client
  (F-002). The ONLY module in the entire codebase that imports the
  Anthropic SDK or any Langchain provider class. This preserves the
  provider-replaceability invariant per AAP Section 0.7.7 ("No direct
  Anthropic SDK usage outside ``services/ai_orchestration.py``").
- ``duplicate_detection``: LinkedIn URL duplicate lookup against the
  partial unique index ``(org_id, normalized_linkedin_url) WHERE
  deleted_at IS NULL``. Backs F-010.
- ``admin``: administrative aggregations, role mutations, hard
  delete, and the three analytics panels exposed by F-014. All
  operations are restricted to the Admin role at the API layer per
  AAP Section 0.7.1 invariant 7.

Conventions
-----------
- Service functions accept the authenticated session context (the
  ``Session`` dataclass produced by :mod:`app.middleware.auth`) as an
  explicit parameter, never via a thread-local. This keeps the service
  layer pure for testing and avoids hidden coupling.
- Service functions raise typed exceptions (subclasses of
  ``app.middleware.error_handlers.AppError`` such as
  :class:`app.middleware.error_handlers.NotFoundError`,
  :class:`app.middleware.error_handlers.ForbiddenError`,
  :class:`app.middleware.error_handlers.ConflictError`); the registered
  Flask error handler maps these to JSON envelopes per AAP Section
  0.4.3.
- State-changing service functions are atomic: the state change AND
  the audit-event INSERT happen in a single database transaction.
  Failure of either rolls back both, satisfying AAP Section 0.7.1
  invariant 6.
- Service modules do NOT import another service module's private
  internals (underscore-prefixed helpers). Cross-service collaboration
  only happens through the public functions re-exported here.

Importing app.services has zero side effects beyond running the
re-export statements: no database connection is opened, no HTTP call
is made, no environment variable is read, and no file is touched.
External-resource side effects only occur inside the consumer
functions themselves.

Re-export ordering note
-----------------------
The per-module re-export blocks below are arranged in alphabetical
order (admin, ai_orchestration, audit, auth, connections,
duplicate_detection) to satisfy the project's ruff isort
configuration (``force-sort-within-sections = true``) and to make
diffs predictable. The conceptual build-layer ordering used by
AAP Section 0.5.1 (foundation -> auth -> RBAC + audit -> create
paths -> read paths -> classification -> composition) is documented
in the ``Modules`` section above, not encoded in import order.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Admin operations (F-014)
# ---------------------------------------------------------------------------
# ``list_all_users`` enumerates all users in the actor's organization for
# the Admin user-management surface. ``update_user_role`` performs the
# role mutation and emits the corresponding ``role_change`` audit event.
# ``list_records_for_moderation`` powers the Admin record-moderation
# tab including (optionally) soft-deleted records. ``hard_delete_record``
# permanently removes a record (Admin-only) and emits the
# ``hard_delete`` audit event. ``compute_analytics`` produces the
# three-panel analytics payload (most active contributors, leads by
# status, weekly activity sparkline).
from app.services.admin import (
    compute_analytics,
    hard_delete_record,
    list_all_users,
    list_records_for_moderation,
    update_user_role,
)

# ---------------------------------------------------------------------------
# AI orchestration (F-002) - sole importer of the Anthropic SDK
# ---------------------------------------------------------------------------
# ``generate_outreach_notes`` is the only public entry point of the AI
# orchestration module and the only function in the entire backend that
# (transitively) imports the Anthropic SDK or Langchain provider classes.
# This preserves the provider-replaceability invariant per AAP Section
# 0.7.7 so the upstream model vendor can be swapped without changing
# feature handlers.
from app.services.ai_orchestration import generate_outreach_notes

# ---------------------------------------------------------------------------
# Audit emitter (foundational; all state-mutating services depend on this)
# ---------------------------------------------------------------------------
# ``emit_audit_event`` is the SOLE writer of the ``audit_events`` table
# per AAP Section 0.7.1 invariant 5 (append-only) and invariant 6
# (atomic state-change + audit pair). The PostgreSQL-level GRANT INSERT
# / REVOKE UPDATE, DELETE clauses in the initial migration enforce the
# same invariant at the database privilege layer.
from app.services.audit import emit_audit_event

# ---------------------------------------------------------------------------
# Authentication primitives (F-012)
# ---------------------------------------------------------------------------
# ``hash_password`` / ``verify_password`` wrap bcrypt 4.x with the
# project's cost-12 production setting (cost-4 in TestingConfig for fast
# tests). ``mint_session_jwt`` / ``verify_session_jwt`` produce and
# validate the HS256 8-hour session token delivered to the SPA via an
# HttpOnly + Secure + SameSite=Lax cookie. ``upsert_oauth_user`` is the
# Google OAuth callback path's user row creator/updater.
from app.services.auth import (
    hash_password,
    mint_session_jwt,
    upsert_oauth_user,
    verify_password,
    verify_session_jwt,
)

# ---------------------------------------------------------------------------
# Connection record CRUD (F-001, F-004, F-005, F-007, F-011)
# ---------------------------------------------------------------------------
# This module owns the entire lifecycle of ``records``:
#
# - ``create_record``: F-001 form-driven create. Opens its own
#   ``session.begin()`` block and emits the corresponding ``create``
#   audit event in the same transaction.
# - ``get_record`` / ``list_records``: read paths backing the F-011
#   detail view and the F-004 feed; both inject the org-scope and
#   soft-delete-scope predicates uniformly.
# - ``update_record`` (F-007 edit), ``update_status`` (F-005 outreach
#   status mutation, RBAC-gated to Sales Rep / Admin), and
#   ``soft_delete_record`` (F-007 soft delete) are the three
#   state-changing mutation paths beyond CREATE; each emits its own
#   typed audit event in the parent transaction.
# - ``get_record_history``: powers the F-011 edit-history feed by
#   surfacing audit events filtered to a single record id.
from app.services.connections import (
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
# ``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL`` to
# detect a pre-existing record with the same normalized LinkedIn URL
# within the same org. Per AAP Section 0.7.6 this returns a
# non-blocking warning, never a hard reject.
from app.services.duplicate_detection import find_duplicate

# ---------------------------------------------------------------------------
# Public re-export surface
# ---------------------------------------------------------------------------
# ``__all__`` defines the package's public API. Names are listed in
# alphabetical order so wildcard imports (``from app.services import *``)
# and tooling (mypy, ruff) recognize the canonical surface. Helper
# utilities such as ``_build_prompt``, ``_assert_in_transaction``,
# ``_apply_org_scope``, etc., are intentionally PRIVATE inside their
# respective modules and are deliberately NOT re-exported here.
__all__ = [
    "compute_analytics",
    "create_record",
    "emit_audit_event",
    "find_duplicate",
    "generate_outreach_notes",
    "get_record",
    "get_record_history",
    "hard_delete_record",
    "hash_password",
    "list_all_users",
    "list_records",
    "list_records_for_moderation",
    "mint_session_jwt",
    "soft_delete_record",
    "update_record",
    "update_status",
    "update_user_role",
    "upsert_oauth_user",
    "verify_password",
    "verify_session_jwt",
]
