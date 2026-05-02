"""Pydantic 2.x request and response schema re-exports.

This package contains the AUTHORITATIVE server-side validation schemas
for the Sales-Connections REST API. Per AAP Section 0.7.1 (Architectural
Invariant 8), every payload that reaches a Flask handler is re-validated
by a pydantic schema regardless of any client-side Zod validation.

Schemas are organized by domain in submodules:

- ``connection``       Connection record CRUD, status, duplicate-check,
                       history, pagination, tag schemas (F-001, F-004,
                       F-005, F-007, F-008, F-010, F-011).
- ``note_generation``  AI note generation request/response (F-002).
- ``admin``            Admin panel: user listing, role mutation,
                       analytics aggregation (F-009, F-014).
- ``auth``             Authentication: login/logout, OAuth callback
                       query (F-012).

Public re-exports allow handlers, services, and tests to write::

    from app.schemas import (
        ConnectionCreate,
        ConnectionRead,
        UserRead,
        LoginRequest,
    )

without knowing which submodule each schema lives in. This is the
same pattern used by ``app.models.__init__`` for SQLAlchemy models.

Per AAP Section 0.5.3, schemas mirror Zod schemas in
``frontend/src/schemas/`` field-for-field; drift between the two
layers is a defect.
"""

from __future__ import annotations

from app.schemas.admin import (
    AnalyticsResponse,
    ContributorActivity,
    LeadsByStatusEntry,
    UserRead,
    UserRoleUpdate,
    WeeklyActivityEntry,
)
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    OAuthCallbackQuery,
    SessionRead,
)
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionDuplicateCheckResponse,
    ConnectionHistoryEntry,
    ConnectionRead,
    ConnectionStatusUpdate,
    ConnectionUpdate,
    PaginatedConnections,
    TagCreate,
    TagRead,
)
from app.schemas.note_generation import (
    NoteGenerationRequest,
    NoteGenerationResponse,
)

__all__ = [
    "AnalyticsResponse",
    "ConnectionCreate",
    "ConnectionDuplicateCheckResponse",
    "ConnectionHistoryEntry",
    "ConnectionRead",
    "ConnectionStatusUpdate",
    "ConnectionUpdate",
    "ContributorActivity",
    "LeadsByStatusEntry",
    "LoginRequest",
    "LoginResponse",
    "NoteGenerationRequest",
    "NoteGenerationResponse",
    "OAuthCallbackQuery",
    "PaginatedConnections",
    "SessionRead",
    "TagCreate",
    "TagRead",
    "UserRead",
    "UserRoleUpdate",
    "WeeklyActivityEntry",
]
