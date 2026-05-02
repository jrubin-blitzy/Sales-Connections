"""Pydantic 2.x authentication schemas (F-012).

This module defines the request/response schemas for the authentication
surface of the Sales-Connections REST API:

- ``LoginRequest``        Payload for ``POST /auth/login`` (email +
                          password fallback flow).
- ``LoginResponse``       Response body for the login endpoints. The
                          session cookie is set via ``Set-Cookie``,
                          NOT carried in the response body. The body
                          carries the authenticated ``UserRead`` so
                          the SPA can hydrate its auth context.
- ``OAuthCallbackQuery``  Validates the query string of
                          ``GET /auth/google/callback``: ``code``,
                          ``state``, and optional ``error`` /
                          ``error_description`` fields per RFC 6749
                          Section 4.1.2 / 4.1.2.1.
- ``SessionRead``         Lightweight session payload returned by
                          ``GET /api/me`` so the SPA's AuthProvider
                          can re-hydrate after page refresh without
                          another login.

Per AAP Section 0.7.1 invariant 8, every payload reaching a Flask
handler is re-validated by these schemas regardless of any client-side
Zod validation.

Per AAP Section 0.7.4 (Security Invariants), passwords use
``pydantic.SecretStr`` so accidental ``repr()`` or string interpolation
prints ``**********`` instead of the plaintext password. SecretStr
values are extracted via ``.get_secret_value()`` only at the bcrypt
hashing boundary.

Per AAP Section 0.4.5 (Surface 3 - Backend <-> Google OAuth):
``OAuthCallbackQuery`` validates Google's authorization-code callback.
Google's success callback carries ``code`` + ``state`` (RFC 6749
Sec 4.1.2); a failure callback carries ``error`` + ``state`` plus an
optional ``error_description`` (RFC 6749 Sec 4.1.2.1). The
``check_code_or_error`` model validator enforces "exactly one of
``code`` / ``error``" so malformed callbacks surface as HTTP 422
rather than silently passing through to the handler.

Per AAP Section 0.5.2 (Layer 1 - F-012 Authentication), this module
is consumed by ``backend/app/api/auth.py`` for inbound payload
validation and by ``backend/app/services/auth.py`` for service-layer
boundary contracts.

Per AAP Section 0.5.3, schemas mirror Zod schemas in
``frontend/src/schemas/auth.ts`` field-for-field; drift between the
two layers is a defect.

Note on imports: pydantic v2 evaluates field type hints at
class-definition time (via ``get_type_hints``) to wire up its
validators, so ``EmailStr``, ``SecretStr``, and ``UserRead`` MUST be
present at runtime even though they appear primarily in type
annotations. The ``# noqa: TC001/TC002`` suppressions on the import
lines document this constraint and prevent ruff's flake8-type-checking
strict mode from moving the imports into a ``TYPE_CHECKING`` block
(which would break the schemas at first use).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (  # noqa: TC002  (pydantic resolves these at class-construction time)
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    model_validator,
)

from app.schemas.admin import (
    UserRead,  # noqa: TC001  (used in pydantic type annotations at runtime)
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Email field length cap. Matches RFC 5321 maximum and the
# corresponding cap on ``UserRead.email`` / ``User.email`` so the
# schema rejects oversized inputs BEFORE the database would otherwise
# raise a DataError.
_EMAIL_MAX_CHARS: int = 320

# Password field length cap. Bcrypt's effective input length is 72
# bytes anyway; capping at 128 chars protects against DoS-style
# very-long-password submissions and matches the cap mirrored in
# ``frontend/src/schemas/auth.ts``.
_PASSWORD_MAX_CHARS: int = 128
_PASSWORD_MIN_CHARS: int = 1

# Length caps for OAuth callback query parameters. Google's actual
# authorization codes are typically <300 chars; 2048 leaves comfortable
# headroom while still bounding the per-request memory footprint
# against a malicious actor crafting a multi-megabyte query string.
_OAUTH_CODE_MAX_CHARS: int = 2048
_OAUTH_STATE_MAX_CHARS: int = 2048
_OAUTH_ERROR_CODE_MAX_CHARS: int = 255
_OAUTH_ERROR_DESCRIPTION_MAX_CHARS: int = 2048
_OAUTH_SCOPE_MAX_CHARS: int = 4096
_OAUTH_AUTHUSER_MAX_CHARS: int = 64
_OAUTH_HD_MAX_CHARS: int = 255
_OAUTH_PROMPT_MAX_CHARS: int = 64

# Per the assigned folder Conventions: every inbound schema rejects
# unexpected keys (defends against role-escalation or callback-tampering
# attempts per AAP Section 0.7.4) and strips whitespace so a
# whitespace-only payload becomes ``""`` post-strip and is rejected by
# ``min_length=1`` rather than passing through as a meaningless value.
_STRICT_CONFIG: ConfigDict = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True,
)

# Outbound config enables ORM-mode serialization. Pydantic's
# ``from_attributes=True`` mode reads ONLY the fields declared on the
# schema; extra ORM attributes (e.g. internal ``org_id``,
# ``password_hash``) are silently ignored. This is defense in depth
# against accidental leakage of server-only fields. Used by the
# response schemas ``LoginResponse`` and ``SessionRead`` which compose
# ``UserRead`` directly from a SQLAlchemy ``User`` ORM instance.
_OUTBOUND_CONFIG: ConfigDict = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "LoginRequest",
    "LoginResponse",
    "OAuthCallbackQuery",
    "SessionRead",
]


# ---------------------------------------------------------------------------
# Inbound: LoginRequest
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Payload for ``POST /auth/login`` (email + password fallback flow).

    The email/password fallback exists alongside the primary Google OAuth
    flow per AAP Section 0.1.2:

    > "Stack mandates from the user's brief, taken verbatim:
    >    Auth: simple email-based auth or SSO (Google OAuth)"

    Fields:
        email     RFC 5322 email address. Validated by pydantic's
                  ``EmailStr`` (uses email-validator under the hood) so
                  typo'd or syntactically invalid emails are rejected
                  at the schema layer with HTTP 422.
        password  Plaintext password supplied by the user, wrapped in
                  ``SecretStr`` so it is never accidentally logged via
                  ``repr()`` or string interpolation. The handler
                  extracts the value via ``.get_secret_value()`` only
                  at the bcrypt hashing boundary.

    Field-length defenses:
        email     Max 320 chars (RFC 5321 maximum).
        password  Max 128 chars. Bcrypt's effective input length is
                  72 bytes anyway; capping at 128 chars protects
                  against DoS-style very-long-password submissions.
                  ``min_length=1`` prevents empty-password submissions
                  from reaching bcrypt; the handler can return 401
                  immediately on validation failure.

    Anti-tampering rationale:
        ``extra='forbid'`` (via ``_STRICT_CONFIG``) rejects any
        client-supplied extra fields. An attacker who got a CSRF token
        and a partially-leaked endpoint cannot post extra fields like
        ``role='Admin'`` to escalate privilege. The schema rejects with
        HTTP 422 before any handler logic runs - defense in depth on
        top of the handler's authoritative session-cookie identity.
    """

    model_config = _STRICT_CONFIG

    email: Annotated[
        EmailStr,
        Field(
            max_length=_EMAIL_MAX_CHARS,
            description=(
                "RFC 5322 email address validated by pydantic EmailStr. "
                "Rejected with HTTP 422 if syntactically invalid."
            ),
        ),
    ]
    password: Annotated[
        SecretStr,
        Field(
            min_length=_PASSWORD_MIN_CHARS,
            max_length=_PASSWORD_MAX_CHARS,
            description=(
                "Plaintext password wrapped in SecretStr so repr() and "
                "str() print '**********' instead of the actual value. "
                "The handler extracts the value via "
                ".get_secret_value() only at the bcrypt hashing "
                "boundary."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Outbound: LoginResponse
# ---------------------------------------------------------------------------


class LoginResponse(BaseModel):
    """Response body for the login endpoints.

    The session JWT cookie is delivered out-of-band via the
    ``Set-Cookie`` header (HttpOnly, Secure, SameSite=Lax, Path=/), NOT
    in this response body. The body carries the authenticated user's
    ``UserRead`` shape so the SPA's ``AuthProvider`` can immediately
    hydrate its auth state without an extra ``GET /api/me`` round-trip.

    Per AAP Section 0.4.3, the SPA fetch wrapper at
    ``frontend/src/api/client.ts`` includes ``credentials: 'include'``
    so the cookie is sent on subsequent requests.

    CRITICAL SECURITY: This schema does NOT include the session JWT in
    the response body. Including it would expose the token to
    JavaScript and defeat the HttpOnly cookie protection (XSS would
    then steal the token). The token lives ONLY in the
    ``Set-Cookie`` header per AAP Section 0.7.4 (Security Invariants).

    Fields:
        user  The authenticated user as ``UserRead`` (id, email,
              display_name, role, created_at). Sourced from a
              SQLAlchemy ``User`` ORM instance via
              ``UserRead.model_validate(user_orm)``. The
              ``password_hash`` and ``org_id`` columns are NEVER
              serialized because ``UserRead`` does not declare them.
    """

    model_config = _OUTBOUND_CONFIG

    user: UserRead


# ---------------------------------------------------------------------------
# Inbound: OAuthCallbackQuery
# ---------------------------------------------------------------------------


class OAuthCallbackQuery(BaseModel):
    """Validates the query string of ``GET /auth/google/callback``.

    Google's OAuth 2.0 callback may carry either:

    - Success: ``?code=<auth_code>&state=<state>`` per RFC 6749 Sec 4.1.2.
    - Failure: ``?error=<code>&state=<state>&error_description=<msg>``
      per RFC 6749 Sec 4.1.2.1.

    The handler dispatches on which field is present:

    - If ``code`` is set, exchange it for an ID token and complete login.
    - If ``error`` is set, render a friendly error page and emit an
      ``audit_events.event_type = authentication`` row capturing the
      failure.

    Exactly one of ``code`` or ``error`` MUST be set; the model
    validator ``check_code_or_error`` enforces this invariant.

    Anti-tampering rationale:
        Google's success callback often carries informational extras
        (``scope``, ``authuser``, ``hd``, ``prompt``); these are
        declared explicitly so they pass validation while
        ``extra='forbid'`` (via ``_STRICT_CONFIG``) still rejects
        truly unknown query parameters. Unexpected keys could indicate
        callback tampering or a future Google addition worth
        investigating; surfacing them as HTTP 422 makes both cases
        visible rather than silently ignored.

    Field-length defenses:
        Each value is capped to bound per-request memory and to make a
        malicious very-long-query-string submission cheap to reject.
        Google's actual codes are typically <300 chars; the 2048 cap
        leaves comfortable headroom.

    Fields:
        code               OAuth 2.0 authorization code (success path).
                           Mutually exclusive with ``error``.
        state              CSRF / replay-protection token issued by
                           ``GET /auth/google/start``. Always required.
        error              OAuth 2.0 error code (failure path), e.g.
                           ``access_denied``, ``invalid_request``.
                           Mutually exclusive with ``code``.
        error_description  Human-readable failure description from
                           Google. Optional even on the failure path.
        scope              Space-delimited list of granted scopes.
                           Informational; Google may include on success.
        authuser           Google account index (``0``, ``1``, ...).
                           Informational.
        hd                 Hosted-domain hint (e.g. ``example.com``)
                           when the user signs in with a Workspace
                           account. Informational.
        prompt             The ``prompt`` value Google honored
                           (``consent``, ``select_account``, ``none``).
                           Informational.
    """

    model_config = _STRICT_CONFIG

    code: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=_OAUTH_CODE_MAX_CHARS,
            description=(
                "OAuth 2.0 authorization code (success path). Mutually "
                "exclusive with ``error`` per RFC 6749 Sec 4.1.2."
            ),
        ),
    ]
    state: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_OAUTH_STATE_MAX_CHARS,
            description=(
                "CSRF / replay-protection token issued by "
                "``GET /auth/google/start`` and persisted in a "
                "short-lived signed cookie. Always required."
            ),
        ),
    ]
    error: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=_OAUTH_ERROR_CODE_MAX_CHARS,
            description=(
                "OAuth 2.0 error code (failure path) per RFC 6749 "
                "Sec 4.1.2.1, e.g. ``access_denied``. Mutually "
                "exclusive with ``code``."
            ),
        ),
    ]
    error_description: Annotated[
        str | None,
        Field(
            default=None,
            max_length=_OAUTH_ERROR_DESCRIPTION_MAX_CHARS,
            description=(
                "Human-readable failure description from Google. Optional even on the failure path."
            ),
        ),
    ]
    scope: Annotated[
        str | None,
        Field(
            default=None,
            max_length=_OAUTH_SCOPE_MAX_CHARS,
            description=(
                "Space-delimited list of scopes Google granted. "
                "Informational; the handler does not parse this value."
            ),
        ),
    ]
    authuser: Annotated[
        str | None,
        Field(
            default=None,
            max_length=_OAUTH_AUTHUSER_MAX_CHARS,
            description=(
                "Google account index when the user has multiple "
                "Google accounts signed in (``0``, ``1``, ...). "
                "Informational."
            ),
        ),
    ]
    hd: Annotated[
        str | None,
        Field(
            default=None,
            max_length=_OAUTH_HD_MAX_CHARS,
            description=(
                "Hosted-domain hint (e.g. ``example.com``) when the "
                "user signs in with a Google Workspace account. "
                "Informational."
            ),
        ),
    ]
    prompt: Annotated[
        str | None,
        Field(
            default=None,
            max_length=_OAUTH_PROMPT_MAX_CHARS,
            description=(
                "The ``prompt`` value Google honored (``consent``, "
                "``select_account``, ``none``). Informational."
            ),
        ),
    ]

    @model_validator(mode="after")
    def check_code_or_error(self) -> OAuthCallbackQuery:
        """Enforce exactly one of ``code`` or ``error`` is set.

        Per RFC 6749 Sec 4.1.2 (success) and Sec 4.1.2.1 (failure), a
        Google OAuth callback MUST carry either ``code`` (success) or
        ``error`` (failure) - never both, never neither. A request
        carrying neither is malformed (likely tampering or a network
        glitch); a request carrying both is contradictory. Both cases
        surface as HTTP 422 via ``ValidationError``.

        Returns:
            The validated ``OAuthCallbackQuery`` instance unchanged.

        Raises:
            ValueError: If neither ``code`` nor ``error`` is set, or if
                both are set simultaneously. Pydantic converts this to
                ``ValidationError`` and the Flask error handler converts
                that to HTTP 422.
        """
        if self.code is None and self.error is None:
            raise ValueError("OAuth callback must include either 'code' or 'error'")
        if self.code is not None and self.error is not None:
            raise ValueError("OAuth callback must not include both 'code' and 'error'")
        return self


# ---------------------------------------------------------------------------
# Outbound: SessionRead
# ---------------------------------------------------------------------------


class SessionRead(BaseModel):
    """Lightweight session payload returned by ``GET /api/me``.

    Used by the SPA's ``AuthProvider`` (per AAP Section 0.5.2 Layer 1)
    to hydrate auth state on initial page load. If the session cookie
    is missing or invalid, the endpoint returns 401 and the
    ``AuthProvider`` redirects to ``/login``.

    The payload includes the user's full ``UserRead`` shape so the
    SPA can render the user's display name and gate UI elements by
    role (``RoleGate`` component) without a second round-trip.

    Fields:
        user           The authenticated user as ``UserRead``.
        authenticated  Always ``True`` for a successfully decoded
                       session. The endpoint NEVER returns this schema
                       for unauthenticated requests; it returns 401
                       instead. The field is included so the SPA's
                       TypeScript types match the Zod schema (mirror
                       parity per AAP Section 0.5.3).

    Per AAP Section 0.7.4 (Security Invariants), the embedded
    ``UserRead`` intentionally excludes ``password_hash`` and
    ``org_id``; the bcrypt hash never crosses the API boundary.
    """

    model_config = _OUTBOUND_CONFIG

    user: UserRead
    authenticated: bool = True
