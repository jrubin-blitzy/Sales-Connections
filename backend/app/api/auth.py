"""F-012 Authentication API blueprint.

Five endpoints span the authentication surface:

- ``POST /auth/login`` -- Email/password login. Validates credentials
  via :func:`app.services.auth.authenticate_email_password`, mints a
  server-validated PyJWT session token, and sets it as an
  HttpOnly + Secure + SameSite=Lax cookie. The response body contains
  the user info only (NO JWT in body) per AAP Section 0.7.4.

- ``POST /auth/logout`` -- Clears the session cookie and emits an
  ``authentication`` audit event when an authenticated session is
  present on ``g.session``. The endpoint is intentionally idempotent:
  unauthenticated requests still receive a 200 response with a
  cookie-clearing ``Set-Cookie`` header so a user with an expired
  cookie can complete logout without re-authenticating.

- ``GET /auth/google/start`` -- Initiates the OAuth 2.0
  authorization-code + PKCE flow against Google. Authlib's Flask
  integration generates the ``state`` and ``code_verifier`` and
  persists them in the framework's session storage; the browser is
  redirected (HTTP 302) to Google's authorization URL.

- ``GET /auth/google/callback`` -- Validates state, exchanges the
  authorization code for an ID token, upserts the matching User row
  via :func:`app.services.auth.upsert_oauth_user`, emits an
  ``authentication`` audit event via
  :func:`app.services.auth.record_login_audit` (atomic with the
  upsert), mints a session JWT, sets the cookie, and redirects the
  browser to ``/feed`` (or the validated ``next`` path). OAuth
  access/refresh tokens are NEVER persisted nor exposed to the SPA
  per AAP Section 0.7.4.

- ``GET /api/me`` -- Returns the current session info as
  :class:`SessionRead`. Used by the SPA's ``AuthProvider`` on mount
  to hydrate session state. A 401 from this endpoint signals "not
  logged in"; the AuthProvider interprets that as the unauthenticated
  state without crashing. The handler performs a single-row
  primary-key lookup against ``users`` to refresh the role so role
  mutations take effect on the next ``/api/me`` call without
  requiring the user to log out and log back in.

Two Flask blueprints are exported because the application factory in
:mod:`app.api.__init__` mounts the four ``/auth/*`` routes under the
``/auth`` prefix while the session-introspection route is mounted
under ``/api``:

* ``auth_bp`` mounted at ``/auth`` -- ``login``, ``logout``,
  ``google_start``, ``google_callback``.
* ``me_bp`` mounted at ``/api`` -- ``get_me``.

Per AAP Section 0.5.3 thin-handler convention, every endpoint here
delegates to :mod:`app.services.auth` for credential validation, JWT
minting, audit emission, and OAuth user upsert. This file is concerned
only with HTTP wiring (request parsing, cookie setting, response
shaping). All business logic lives in the service layer.

Per AAP Section 0.7.4 (Security Invariants):

* Session cookies use HttpOnly, Secure (in production), SameSite=Lax,
  and Path=/, with the JWT carried out-of-band -- the cookie value
  never appears in any response body.
* OAuth tokens (access token, refresh token) NEVER cross the SPA
  boundary. Only the locally minted session JWT does.
* Anti-enumeration: the email/password endpoint returns a generic
  401 ``"Invalid credentials."`` message regardless of whether the
  email exists, the password is wrong, or the user is OAuth-only.
* Audit emission: every successful login (password or OAuth) and
  every logout where ``g.session`` is populated emits a F-013
  ``audit_events`` row with ``event_type=AUTHENTICATION`` inside the
  same transaction as the upsert/emission.

This module deliberately has no module-level side effects beyond the
construction of the two blueprint objects. Importing :mod:`app.api.auth`
does NOT register routes on a Flask app; route registration is
mediated by :func:`app.api.register_blueprints`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    request,
    url_for,
)
from pydantic import ValidationError
from sqlalchemy import select

from app.extensions import db, oauth
from app.middleware.error_handlers import AuthError, ValidationFailedError
from app.models import User
from app.schemas import (
    LoginRequest,
    LoginResponse,
    SessionRead,
    UserRead,
)
from app.services.auth import (
    authenticate_email_password,
    mint_session_jwt,
    record_login_audit,
    record_logout_audit,
    upsert_oauth_user,
)

if TYPE_CHECKING:
    from flask.wrappers import Response


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# Stdlib logger -- the structlog processor chain configured in
# :mod:`app.observability.logging` automatically attaches the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id`` to
# every log line emitted within a request context.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# Two blueprints are exported because :mod:`app.api.__init__` mounts
# the four core auth endpoints under ``/auth`` while the
# session-introspection probe lives under ``/api``:
#
# * ``auth_bp`` mounted at ``/auth``
# * ``me_bp``   mounted at ``/api``
#
# Splitting the blueprints lets a single Flask blueprint own a
# coherent URL prefix while keeping all session-related handler logic
# co-located in this one module.

auth_bp: Blueprint = Blueprint("auth", __name__)
me_bp: Blueprint = Blueprint("me", __name__)


__all__ = ["auth_bp", "me_bp"]


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def _session_cookie_kwargs() -> dict[str, Any]:
    """Return the ``set_cookie`` keyword arguments for the session cookie.

    Reads cookie configuration from the active Flask app config so
    each environment (DevelopmentConfig / TestingConfig /
    ProductionConfig) can override the security flags. Production
    sets ``Secure=True`` so the cookie travels only over HTTPS;
    development sets ``Secure=False`` so the cookie works over plain
    HTTP on localhost.

    Per AAP Section 0.7.4 (Security Invariants), the production cookie
    is always:

    * ``HttpOnly`` -- no JavaScript access (defends XSS token theft).
    * ``Secure``   -- HTTPS only.
    * ``SameSite=Lax`` -- CSRF defense for top-level navigations.
    * ``Path=/``   -- sent on every request to the API.

    ``max_age`` matches the JWT TTL so the cookie expires at the same
    time the token does; this prevents the SPA from sending an expired
    token back on a subsequent request and getting a confusing 401
    instead of a clean redirect to /login.
    """
    cfg = current_app.config
    return {
        "key": cfg.get("SESSION_COOKIE_NAME", "session"),
        "httponly": cfg.get("SESSION_COOKIE_HTTPONLY", True),
        "secure": cfg.get("SESSION_COOKIE_SECURE", True),
        "samesite": cfg.get("SESSION_COOKIE_SAMESITE", "Lax"),
        "path": "/",
        "max_age": cfg.get("JWT_TTL_SECONDS", 8 * 60 * 60),
    }


def _set_session_cookie(response: Response, jwt_token: str) -> None:
    """Attach the session JWT as an HttpOnly cookie on ``response``.

    Centralizes the cookie-attribute derivation so login and OAuth
    callback handlers stay consistent.
    """
    response.set_cookie(value=jwt_token, **_session_cookie_kwargs())


def _clear_session_cookie(response: Response) -> None:
    """Clear the session cookie on ``response``.

    Uses ``set_cookie(value="", max_age=0)`` for maximum browser
    compatibility -- the cookie is overwritten with empty bytes AND
    its max_age is set to 0 so browsers that ignore one mechanism
    still honor the other.
    """
    cookie_kwargs = _session_cookie_kwargs()
    cookie_kwargs["max_age"] = 0
    response.set_cookie(value="", **cookie_kwargs)


# ---------------------------------------------------------------------------
# URL safety helper
# ---------------------------------------------------------------------------


def _safe_next_path(candidate: str | None) -> str:
    """Validate a ``next`` parameter to prevent open-redirect attacks.

    Only same-origin path-only redirects are permitted. Any candidate
    that contains ``"://"``, starts with ``"//"``, or is missing a
    leading ``"/"`` falls back to the default ``"/feed"`` route.

    This is the standard defense against open-redirect attacks that
    leverage post-authentication redirects to phish credentials on a
    look-alike domain. Even though the redirect is server-issued
    after the user is authenticated, an attacker could craft a link
    like ``/auth/google/start?next=https://evil.example.com`` and
    capture the user's post-login state.
    """
    default = "/feed"
    if not candidate:
        return default
    if not isinstance(candidate, str):
        return default
    if "://" in candidate or candidate.startswith("//"):
        return default
    if not candidate.startswith("/"):
        return default
    return candidate


# ---------------------------------------------------------------------------
# Default org id helper
# ---------------------------------------------------------------------------


def _default_org_id() -> str:
    """Return the configured default organization id (single-org MVP).

    Per AAP Section 0.7.2, the MVP runtime serves exactly one
    organization. ``DEFAULT_ORG_ID`` is the canonical UUID that all
    new users are assigned to and the scope under which login lookups
    occur. Future multi-org work would replace this single read with
    an org-resolution step (e.g., subdomain or SSO claim mapping).

    Returns:
        The string-form UUID of the default organization.
    """
    org_id: str = current_app.config["DEFAULT_ORG_ID"]
    return org_id


# ---------------------------------------------------------------------------
# POST /auth/login -- email/password fallback flow
# ---------------------------------------------------------------------------


@auth_bp.route("/login", methods=["POST"])
def login() -> tuple[Response, int]:
    """Authenticate via email + password and mint a session cookie.

    Request body (JSON)::

        {"email": "alice@example.com", "password": "..."}

    Validation: ``email`` is a valid RFC-5322 address; ``password`` is
    a non-empty string up to 128 chars (per :class:`LoginRequest`).

    Success response (HTTP 200) -- NO JWT in body, only user info::

        {
            "user": {
                "id": "...",
                "email": "...",
                "display_name": "...",
                "role": "Contributor",
                "created_at": "..."
            }
        }
        Set-Cookie: session=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=28800

    Failure responses:
        - 422 on schema validation failure (missing email, malformed
          email, empty password). The global ValidationFailedError
          handler renders the field-scoped errors.
        - 401 on invalid credentials. The error message is generic
          (``"Invalid credentials."``) to avoid leaking whether the
          email is registered (anti-enumeration per AAP Section 0.7.4).

    Returns:
        ``(response, 200)`` on success. The cookie is attached to the
        response; subsequent SPA requests carry it automatically because
        the fetch wrapper at ``frontend/src/api/client.ts`` includes
        ``credentials: 'include'``.
    """
    # Step 1: parse JSON body. ``silent=True`` so a malformed JSON
    # payload yields ``None`` rather than raising; we map that to a
    # consistent 422 envelope.
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[
                {
                    "loc": ["body"],
                    "msg": "Expected a JSON object with 'email' and 'password'.",
                    "type": "invalid_json",
                }
            ],
        )

    # Step 2: schema validation via pydantic. We catch ValidationError
    # explicitly so we can drop the ``input``/``ctx``/``url`` fields
    # from each error before surfacing to the client -- the ``input``
    # field would echo back the password (PII).
    try:
        payload = LoginRequest.model_validate(raw_body)
    except ValidationError as exc:
        safe_fields: list[dict[str, Any]] = [
            {
                "loc": [str(seg) for seg in err.get("loc", ())],
                "msg": str(err.get("msg", "Invalid value.")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        raise ValidationFailedError(
            message="The request payload failed validation.",
            fields=safe_fields,
        ) from exc

    # Step 3: authenticate. ``authenticate_email_password`` raises
    # AuthError on every failure path (unknown email, wrong password,
    # OAuth-only user). The global error handler maps AuthError to
    # HTTP 401 with the generic anti-enumeration message.
    user = authenticate_email_password(
        email=payload.email,
        password=payload.password.get_secret_value(),
    )

    # Step 4: emit audit event in a fresh transaction. The user object
    # is detached from the authenticate session, but ``user.id`` and
    # ``user.org_id`` are scalar attributes that survive detachment
    # because the SQLAlchemy session is configured with
    # ``expire_on_commit=False``.
    with db.session() as db_session, db_session.begin():
        record_login_audit(user, method="password", db_session=db_session)

    # Step 5: mint the session JWT and shape the response. The cookie
    # is set OUTSIDE the transaction because the JWT mint does not
    # need a database round-trip; doing it here keeps the transaction
    # scope narrow.
    token = mint_session_jwt(user)
    body = LoginResponse(user=UserRead.model_validate(user))
    response: Response = make_response(jsonify(body.model_dump(mode="json")), 200)
    _set_session_cookie(response, token)

    _logger.info(
        "auth_login_success",
        extra={
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "method": "password",
        },
    )
    return response, 200


# ---------------------------------------------------------------------------
# POST /auth/logout -- clear the session cookie
# ---------------------------------------------------------------------------


@auth_bp.route("/logout", methods=["POST"])
def logout() -> tuple[Response, int]:
    """Logout the current session.

    Behavior:

    * Clears the session cookie by setting an empty value with
      ``max_age=0``. Browsers delete the cookie immediately.
    * Emits a F-013 ``authentication`` audit event when ``g.session``
      is populated (so we can correlate the logout with the prior
      login).
    * Always returns 200, regardless of whether a session was
      present. This is intentional: a stale-token logout (the browser
      sent an expired or invalid cookie) should still succeed because
      the goal is to clear state from the client.

    The endpoint may or may not see ``g.session`` populated depending
    on whether :mod:`app.middleware.auth` treats ``/auth/logout`` as a
    public path (skip auth) or a protected path (require auth). Both
    flows are supported here via the ``getattr(g, "session", None)``
    guard.

    Success response (HTTP 200)::

        {}
        Set-Cookie: session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0

    Returns:
        ``(response, 200)`` -- always 200; the operation is idempotent.
    """
    # The auth middleware populates ``g.session`` for protected paths
    # and skips public ones. We tolerate either configuration: if
    # ``g.session`` is present, we have an authenticated actor and
    # can emit the audit event; otherwise the logout is a best-effort
    # cookie-clearing operation.
    session_obj = getattr(g, "session", None)

    if session_obj is not None:
        try:
            with db.session() as db_session, db_session.begin():
                record_logout_audit(session_obj, db_session=db_session)
            _logger.info(
                "auth_logout_success",
                extra={
                    "user_id": str(session_obj.user_id),
                    "org_id": str(session_obj.org_id),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Never fail logout because of a DB problem -- the
            # browser-side cookie clear must always succeed. Log the
            # failure for forensic follow-up so operators can
            # reconcile the audit trail.
            _logger.error(
                "auth_logout_audit_failed",
                extra={"error_class": type(exc).__name__},
            )
    else:
        _logger.info("auth_logout_no_session")

    response: Response = make_response(jsonify({}), 200)
    _clear_session_cookie(response)
    return response, 200


# ---------------------------------------------------------------------------
# GET /auth/google/start -- initiate Google OAuth flow
# ---------------------------------------------------------------------------


@auth_bp.route("/google/start", methods=["GET"])
def google_start() -> Response:
    """Initiate the Google OAuth 2.0 authorization-code flow with PKCE.

    Authlib's Flask integration generates the ``state`` parameter and
    PKCE ``code_verifier`` automatically and persists them in the
    framework's session storage. The browser is redirected (HTTP 302)
    to Google's authorization URL with all required parameters
    (``response_type=code``, ``client_id``, ``redirect_uri``,
    ``scope=openid+email+profile``, ``state``, ``code_challenge``,
    ``code_challenge_method=S256``).

    No request body. No JWT required (this endpoint is for
    unauthenticated users initiating login). The endpoint is in
    :data:`app.middleware.auth._PUBLIC_PATHS` so the auth middleware
    does not intercept the request.

    If Google OAuth is not configured (``GOOGLE_OAUTH_CLIENT_ID`` and
    ``GOOGLE_OAUTH_CLIENT_SECRET`` empty), the Authlib registry has no
    ``google`` client and the attribute access raises ``AttributeError``
    which the global error handler converts to HTTP 500. Production
    deployments configure both credentials so this path is unreachable
    outside of test environments.

    Returns:
        A Flask Response (HTTP 302 redirect) pointing at Google's
        authorization URL.
    """
    # ``url_for(..., _external=True)`` constructs an absolute URL
    # (scheme + host) suitable for Google's redirect_uri. Production
    # deployments override this with ``GOOGLE_OAUTH_REDIRECT_URI`` so
    # the URL exactly matches the value registered in the Google
    # Cloud Console (Google rejects redirect URIs that do not match
    # byte-for-byte).
    redirect_uri = current_app.config.get("GOOGLE_OAUTH_REDIRECT_URI") or url_for(
        "auth.google_callback", _external=True
    )

    _logger.info(
        "auth_google_start",
        extra={"redirect_uri": redirect_uri},
    )

    # ``oauth.google`` resolves to the Authlib client registered by
    # ``app.extensions.init_oauth_clients`` when ``GOOGLE_OAUTH_CLIENT_ID``
    # and ``GOOGLE_OAUTH_CLIENT_SECRET`` are configured. If neither is
    # configured the attribute access raises ``AttributeError`` which the
    # global error handler maps to HTTP 500. ``authorize_redirect`` then
    # returns a Flask Response with the ``Location`` header set to
    # Google's authorization endpoint; Authlib persists the state and
    # PKCE code_verifier in the framework's session storage so the
    # callback handler can validate and exchange them.
    return oauth.google.authorize_redirect(redirect_uri)


# ---------------------------------------------------------------------------
# GET /auth/google/callback -- handle the Google OAuth callback
# ---------------------------------------------------------------------------


@auth_bp.route("/google/callback", methods=["GET"])
def google_callback() -> Response:
    """Complete the Google OAuth 2.0 flow and mint a session cookie.

    Flow:

    1. Detect ``?error=<code>`` (RFC 6749 Sec 4.1.2.1) and redirect
       the SPA to ``/login`` with a generic error indicator. We do
       NOT call ``authorize_access_token`` on the error path because
       Authlib's helper would itself raise on the error param,
       producing a less informative response.
    2. Verify Google OAuth is configured -- raise :class:`AuthError`
       otherwise so the SPA's login screen falls back to
       email/password.
    3. Call ``oauth.google.authorize_access_token()`` which:
       a. Validates the ``state`` parameter against the persisted
          framework state.
       b. Exchanges the authorization code for an access token + ID
          token at Google's token endpoint.
       c. Validates the ID token's signature against Google's JWKS,
          plus the ``iss``/``aud``/``exp`` claims.
       d. Returns a token dict whose ``userinfo`` key carries the
          verified ID token claims.
    4. Pass the verified claims to
       :func:`app.services.auth.upsert_oauth_user` which is the SOLE
       writer of the ``users`` row from OAuth claims. The upsert is
       atomic with the F-013 ``authentication`` audit emission via
       :func:`app.services.auth.record_login_audit`.
    5. Mint the session JWT, set the cookie, and redirect the SPA to
       ``/feed`` (or to a validated ``next`` parameter).

    OAuth access/refresh tokens are NEVER persisted nor exposed to
    the SPA per AAP Section 0.7.4. Only the locally minted session
    JWT crosses the SPA boundary.

    Failure paths:
        - ``?error=...`` from Google -> 302 redirect to
          ``/login?error=oauth_failed``.
        - Authlib state mismatch / token exchange failure -> 401 via
          :class:`AuthError`.
        - Missing or malformed ID token claims -> 401 via
          :class:`AuthError`.

    Returns:
        A 302 redirect to ``/feed`` (or ``next``) on success, or a
        302 redirect to ``/login`` with an error indicator on
        Google-side failures.
    """
    # Step 1: short-circuit on Google-side error responses. Per RFC
    # 6749 Sec 4.1.2.1, an error response includes ``?error=<code>``;
    # we redirect the SPA to /login with a generic error indicator
    # so URL-history snooping does not leak Google's specific error
    # code.
    oauth_error = request.args.get("error")
    if oauth_error:
        _logger.info(
            "auth_google_callback_error_response",
            extra={"oauth_error": oauth_error},
        )
        # Redirect to /login with a generic error indicator. The SPA
        # renders a "Sign-in failed" toast.
        return make_response(redirect("/login?error=oauth_failed"))

    # Step 2: exchange the authorization code for an ID token.
    # ``oauth.google.authorize_access_token`` performs state validation,
    # code exchange, and ID-token signature validation against Google's
    # JWKS in a single call. Any of these subroutines may raise; we catch
    # broadly because Authlib uses many exception types (OAuthError, jwt
    # errors, network errors, AttributeError when the client is not
    # registered) and we want uniform 401 handling that says "OAuth
    # callback failed" regardless of the underlying cause -- per AAP
    # Section 0.7.4 anti-enumeration we do not leak which validation
    # step failed.
    try:
        token_data = oauth.google.authorize_access_token()
    except Exception as exc:
        _logger.warning(
            "auth_google_callback_failed",
            extra={"error_class": type(exc).__name__},
        )
        raise AuthError(message="OAuth callback failed.") from exc

    # Step 3a: extract the verified ID token claims. Authlib >= 1.0
    # populates ``token['userinfo']`` when the ID token contains the
    # expected ``nonce`` and was successfully validated against the
    # provider's JWKS.
    claims: Any = None
    if isinstance(token_data, dict):
        claims = token_data.get("userinfo")
    if not isinstance(claims, dict) or not claims.get("email"):
        _logger.warning("auth_google_callback_missing_claims")
        raise AuthError(message="OAuth ID token missing required claims.")

    # Step 4: upsert the user, emit audit, mint JWT -- all inside one
    # transaction. The transaction boundary is critical per AAP
    # Section 0.7.1 invariant 6 (Atomic state-change + audit pair):
    # if either the upsert or the audit emit fails, both roll back
    # so we never have a User row without a corresponding audit
    # event.
    org_id = _default_org_id()
    try:
        with db.session() as db_session, db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims=dict(claims),
                org_id=org_id,
            )

            # Emit the F-013 ``authentication`` audit event with
            # ``method="oauth_google"`` so SIEM tooling can
            # distinguish password logins from OAuth logins.
            record_login_audit(user, method="oauth_google", db_session=db_session)

            # Mint the JWT inside the transaction so the user
            # object's attributes are still fresh (no
            # expired-attribute access after commit).
            token = mint_session_jwt(user)

            # Capture scalar identifiers BEFORE the session closes
            # so we can log them outside the ``with`` block without
            # triggering a ``DetachedInstanceError``.
            user_id = str(user.id)
            user_org_id = str(user.org_id)
    except ValueError as exc:
        # ``upsert_oauth_user`` raises ValueError when required ID
        # token claims are missing (defense-in-depth check on top of
        # Authlib's validation). Convert to 401 since the cause is a
        # malformed ID token.
        _logger.warning(
            "auth_google_callback_upsert_failed",
            extra={"error_class": type(exc).__name__},
        )
        raise AuthError(message="OAuth user creation failed.") from exc

    # Step 5: redirect the SPA to /feed (or to a validated ``next``).
    # The cookie is set on the redirect response so the browser sends
    # it on the next request to the SPA.
    next_target = _safe_next_path(request.args.get("next"))
    response: Response = make_response(redirect(next_target))
    _set_session_cookie(response, token)

    _logger.info(
        "auth_google_callback_success",
        extra={
            "user_id": user_id,
            "org_id": user_org_id,
            "method": "oauth_google",
        },
    )
    return response


# ---------------------------------------------------------------------------
# GET /api/me -- session introspection probe
# ---------------------------------------------------------------------------


@me_bp.route("/me", methods=["GET"])
def get_me() -> tuple[Response, int]:
    """Return the current session info (F-012).

    Used by the SPA's ``AuthProvider`` on mount to hydrate session
    state. A 401 from this endpoint indicates "not logged in" -- the
    AuthProvider interprets that as the unauthenticated state without
    crashing.

    The JWT claims include ``user_id``, ``org_id``, ``role``, ``email``,
    ``display_name`` (per :func:`app.services.auth.mint_session_jwt`),
    but NOT ``created_at`` which the :class:`UserRead` schema requires.
    We perform a single-row primary-key lookup against ``users`` to
    hydrate ``created_at`` and refresh the role -- so role mutations
    take effect on the next ``/api/me`` call without requiring the
    user to log out and log back in.

    Success response (HTTP 200)::

        {
            "user": {
                "id": "...",
                "email": "...",
                "display_name": "...",
                "role": "Contributor",
                "created_at": "...",
            },
            "authenticated": true,
        }

    Failure response (HTTP 401): emitted by the auth middleware when
    no valid session JWT is present (the ``/api/`` prefix is in
    :data:`_PROTECTED_PREFIXES`). This handler additionally raises
    :class:`AuthError` defensively if the session refers to a deleted
    user.

    Returns:
        ``(response, 200)`` with the :class:`SessionRead` body.
    """
    # The middleware has already populated g.session on protected
    # paths (the ``/api/`` prefix matches ``_PROTECTED_PREFIXES``).
    # If we somehow reach here without one, treat it as an auth
    # failure -- defense in depth.
    session_obj = getattr(g, "session", None)
    if session_obj is None:  # pragma: no cover - defensive
        raise AuthError(message="No active session.")

    # Single-row primary-key lookup. Sub-millisecond at any tenant
    # scale. The lookup gives the SPA fresh role information without
    # requiring re-login when an admin changes their role.
    with db.session() as db_session:
        user = db_session.execute(
            select(User).where(User.id == session_obj.user_id)
        ).scalar_one_or_none()

        if user is None:
            # The token's user_id no longer maps to a row (e.g., user
            # was hard-deleted). Treat as session invalidation so the
            # SPA prompts re-authentication.
            _logger.warning(
                "auth_me_user_not_found",
                extra={"user_id": str(session_obj.user_id)},
            )
            raise AuthError(message="Session refers to a deleted user.")

        # Validate INSIDE the transaction so any ORM-mode reads
        # (created_at, role, etc.) succeed before the session closes.
        # This avoids a potential DetachedInstanceError after commit.
        body = SessionRead(
            user=UserRead.model_validate(user),
            authenticated=True,
        )

    return jsonify(body.model_dump(mode="json")), 200
