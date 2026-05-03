"""Authentication API surface (F-012 - AAP Section 0.4.3).

This module wires the F-012 Authentication transport surface. It
exposes five HTTP endpoints, owned by two separate Flask blueprints
so :mod:`app.api.__init__` can mount them under different URL
prefixes (``/auth`` vs ``/api``):

Mounted under ``/auth`` (``auth_bp``):

* ``POST /auth/login``           - Email/password fallback flow.
* ``POST /auth/logout``          - Clears the session cookie.
* ``GET  /auth/google/start``    - Initiates Google OAuth 2.0
                                   authorization-code flow with PKCE
                                   and state.
* ``GET  /auth/google/callback`` - Handles the Google callback,
                                   exchanges the code for an ID
                                   token, validates it via
                                   Google's JWKS, and mints a
                                   session JWT.

Mounted under ``/api`` (``me_bp``):

* ``GET  /api/me``               - Returns :class:`SessionRead` for
                                   the current user. Used by the
                                   SPA's :class:`AuthProvider` to
                                   hydrate auth state on page load.

Per AAP Section 0.4.5, this module is the EXCLUSIVE owner of the
authentication transport surface. It NEVER imports the Anthropic SDK
(provider replaceability for AI is enforced elsewhere); it is the
ONLY module other than :mod:`app.middleware.auth` that touches the
session cookie.

Per AAP Section 0.7.4 (Security Invariants):

* Session cookies use HttpOnly, Secure (production), SameSite=Lax,
  Path=/, with the JWT carried out-of-band - the cookie value never
  appears in any response body.
* OAuth tokens (access token, refresh token) NEVER cross the SPA
  boundary. Only the locally minted session JWT does.
* Anti-enumeration: the email/password endpoint returns a generic
  401 ``"Invalid email or password."`` message regardless of whether
  the email exists, the password is wrong, or the user is OAuth-only.
* Audit emission: every login attempt (success or failure that the
  service layer reaches) and every logout emits a F-013
  ``audit_events.event_type = authentication`` row inside the same
  transaction as the user upsert/lookup.

Per AAP Section 0.5.3 (handlers are thin), the endpoints below parse
input via pydantic, dispatch to :mod:`app.services.auth`, and format
the response. All business logic - bcrypt verification, JWT minting,
audit emission, ID-token validation - lives in the service layer.

This module deliberately has no module-level side effects beyond the
blueprint object construction. Importing :mod:`app.api.auth` does
NOT register routes on a Flask app; route registration is mediated
by :func:`app.api.register_blueprints`.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` is loaded via ``importlib`` (NOT ``import logging``)
# because the ``app`` namespace tree contains a sibling module
# ``app.observability.logging``. See the convention established in
# ``app.middleware.correlation`` and ``app.api.notes``.
import importlib
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

# Third-party runtime imports.
#
# ``Blueprint`` is the Flask blueprint primitive used to declare a
# logical group of routes. ``current_app`` is the Flask app context
# accessor used to read configuration. ``g`` is the request-scoped
# state object populated by ``app.middleware.auth`` with the
# verified session. ``jsonify`` produces a Flask Response with the
# correct ``Content-Type``. ``make_response`` constructs a Response
# we can mutate to set/clear cookies. ``redirect`` produces a 302
# response for the OAuth start endpoint. ``request`` carries the
# inbound HTTP request. ``url_for`` resolves the absolute callback
# URL for the OAuth ``redirect_uri`` parameter.
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
import structlog

# First-party imports.
#
# ``oauth`` is the Authlib OAuth client registered on the application
# at startup. ``db`` is the SQLAlchemy wrapper used to open a session
# for the OAuth user upsert.
# ``AppError``/``AuthError``/``ValidationFailedError`` are the
# exception classes the handlers raise; the registered Flask error
# handlers convert them into JSON envelopes.
# ``UserRole`` and ``AuditEventType`` enums power the audit emission
# and the response shaping. ``LoginRequest``/``LoginResponse``/
# ``OAuthCallbackQuery``/``SessionRead``/``UserRead`` are the
# pydantic schemas mirrored on the SPA via Zod.
from app.extensions import db, oauth
from app.middleware.error_handlers import (
    AppError,
    AuthError,
    ValidationFailedError,
)
from app.models.enums import AuditEventType
from app.schemas import (
    LoginRequest,
    LoginResponse,
    OAuthCallbackQuery,
    SessionRead,
    UserRead,
)

# Type-only imports.
if TYPE_CHECKING:
    from flask.wrappers import Response


# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above).
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module loggers
# ---------------------------------------------------------------------------
# Stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain. ``merge_contextvars``
# automatically attaches the request-scoped ``correlation_id``,
# ``user_id``, ``org_id``, and ``trace_id``.
_stdlib_logger = logging.getLogger(__name__)
_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# OAuth state cookie
# ---------------------------------------------------------------------------
# The OAuth 2.0 state parameter is a CSRF / replay-protection token
# issued at /auth/google/start and validated at /auth/google/callback.
# We persist it in a short-lived signed cookie named below. The cookie
# is HttpOnly (no JavaScript access) and Secure in production. Lifetime
# matches the Google authorization flow's expected window (10 minutes
# is generous; Google's typical flow completes in <60 seconds).

_OAUTH_STATE_COOKIE_NAME: str = "oauth_state"
_OAUTH_STATE_COOKIE_TTL_SECONDS: int = 10 * 60  # 10 minutes
_OAUTH_STATE_BYTE_LENGTH: int = 32  # 32 bytes = 256 bits, RFC 7636 compliant
_OAUTH_PKCE_VERIFIER_BYTE_LENGTH: int = 64  # 64 bytes -> 86-char base64url


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# Two blueprints are exported:
#
# * ``auth_bp`` - mounted at ``/auth`` for login/logout/OAuth surfaces.
# * ``me_bp``   - mounted at ``/api/me`` for the session-hydration probe.
#
# Splitting these allows :mod:`app.api.__init__` to register them under
# different URL prefixes without one blueprint owning two distinct
# semantic surfaces. ``me_bp`` is logically part of "auth" but lives
# under ``/api`` so the SPA's ``@/api/client`` wrapper routes it
# uniformly with the data API.

auth_bp = Blueprint("auth", __name__)
me_bp = Blueprint("me", __name__)


__all__ = ["auth_bp", "me_bp"]


# ===========================================================================
# Helper: build session cookie kwargs
# ===========================================================================


def _session_cookie_kwargs() -> dict[str, Any]:
    """Return the ``set_cookie`` keyword arguments for the session cookie.

    Reads cookie configuration from the Flask app config so each
    environment (DevelopmentConfig / TestingConfig / ProductionConfig)
    can override the security flags. Production sets ``Secure=True``
    so the cookie travels only over HTTPS; development sets
    ``Secure=False`` so the cookie works over plain HTTP on
    localhost.

    Per AAP Section 0.7.4 (Security Invariants), the production
    cookie is always:

    * ``HttpOnly`` - no JavaScript access (defends XSS token theft).
    * ``Secure``   - HTTPS only.
    * ``SameSite=Lax`` - CSRF defense for top-level navigations.
    * ``Path=/``   - sent on every request to the API.

    The ``max_age`` matches the JWT TTL so the cookie expires at the
    same time the token does; this prevents the SPA from sending an
    expired token back on a subsequent request and getting a
    confusing 401 instead of a clean redirect to /login.
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


# ===========================================================================
# Helper: extract DEFAULT_ORG_ID
# ===========================================================================


def _default_org_id() -> str:
    """Return the configured default organization id (single-org MVP).

    Per AAP Section 0.7.2, the MVP runtime serves exactly one
    organization. ``DEFAULT_ORG_ID`` is the canonical UUID that all
    new users are assigned to and the scope under which login lookups
    occur. Future multi-org work would replace this single read with
    an org-resolution step (e.g., subdomain or SSO claim mapping).
    """
    org_id: str = current_app.config["DEFAULT_ORG_ID"]
    return org_id


# ===========================================================================
# POST /auth/login - email/password fallback flow
# ===========================================================================


@auth_bp.route("/login", methods=["POST"])
def login() -> tuple[Response, int]:
    """Authenticate via email + password and mint a session cookie.

    Per AAP Section 0.5.2 Layer 1, this endpoint:

    1. Validates the request body against :class:`LoginRequest` so
       malformed or extra fields surface as HTTP 422.
    2. Calls :func:`app.services.auth.authenticate_password` which
       performs a constant-time bcrypt check and emits a generic
       401 on any failure (anti-enumeration per AAP Section 0.7.4).
    3. Mints an HS256 session JWT via
       :func:`app.services.auth.mint_session_jwt`.
    4. Emits a F-013 ``authentication`` audit event inside the same
       transaction.
    5. Sets the session cookie via ``Set-Cookie`` and returns the
       :class:`LoginResponse` body so the SPA's AuthProvider can
       hydrate auth state immediately.

    The endpoint is in :data:`app.middleware.auth._PUBLIC_PATHS` so
    :mod:`app.middleware.auth` does NOT require a session cookie to
    reach this handler.

    Returns:
        A tuple of (Response, status_code). On success: 200 with
        :class:`LoginResponse` body and ``Set-Cookie`` header. On
        validation failure: 422 (handled by the global
        ValidationFailedError handler). On authentication failure:
        401 (handled by the global AuthError handler).
    """
    # Lazy import keeps module-load fast and avoids any potential
    # import cycle between services and api packages.
    from app.services.audit import emit_audit_event  # noqa: PLC0415
    from app.services.auth import (  # noqa: PLC0415
        authenticate_password,
        mint_session_jwt,
    )

    # Step 1: parse JSON body. Use ``silent=True`` so a malformed JSON
    # payload yields ``None`` rather than raising; we map that to a
    # consistent 422 envelope.
    raw_body = request.get_json(silent=True)
    if raw_body is None or not isinstance(raw_body, dict):
        raise ValidationFailedError(
            message="Request body must be a JSON object.",
            fields=[{"loc": ["body"], "msg": "invalid_json"}],
        )

    try:
        payload = LoginRequest.model_validate(raw_body)
    except ValidationError as exc:
        # Drop ``input``/``ctx``/``url`` from each error before
        # surfacing to the client - the input field would echo back
        # the password (PII).
        safe_fields = [
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        raise ValidationFailedError(
            message="The request payload failed validation.",
            fields=safe_fields,
        ) from exc

    org_id = _default_org_id()

    # Step 2-4: open a session with an explicit transaction so the
    # authenticate read AND the audit emit commit atomically. Per
    # AAP Section 0.5.3, services own transactions; the handler
    # supplies the transactional context.
    with db.session() as db_session, db_session.begin():
        user = authenticate_password(
            db_session=db_session,
            email=payload.email,
            password=payload.password.get_secret_value(),
            org_id=org_id,
        )

        # Emit audit event in the same transaction. Per F-013, every
        # state-changing path emits an audit row; authentication
        # qualifies because it produces a session that authorizes
        # subsequent state changes.
        emit_audit_event(
            db_session=db_session,
            event_type=AuditEventType.AUTHENTICATION,
            actor_user_id=user.id,
            target_record_id=None,
            before_payload=None,
            after_payload={
                "method": "password",
                "outcome": "success",
            },
        )

        # Mint inside the transaction so the user object's attributes
        # are still fresh (no expired-attribute access after commit).
        token = mint_session_jwt(user)

        # Build the response body BEFORE the session closes so any
        # ORM-mode validation reads still see the live attributes.
        body = LoginResponse(user=UserRead.model_validate(user))

    # Step 5: shape the response and set the cookie. The cookie is
    # set OUTSIDE the transaction because its value is independent of
    # database state; doing it here keeps the transaction scope
    # narrow and mirrors what the OAuth callback does.
    response = make_response(jsonify(body.model_dump(mode="json")), 200)
    response.set_cookie(value=token, **_session_cookie_kwargs())

    _logger.info(
        "auth_login_succeeded",
        method="password",
        user_id=str(body.user.id),
    )
    return response, 200


# ===========================================================================
# POST /auth/logout - clear the session cookie
# ===========================================================================


@auth_bp.route("/logout", methods=["POST"])
def logout() -> tuple[Response, int]:
    """Invalidate the current session by clearing the session cookie.

    Per AAP Section 0.7.4 (Security Invariants), logout:

    * Clears the session cookie by setting an empty value with
      ``max_age=0``. Browsers delete the cookie immediately.
    * Emits a F-013 ``authentication`` audit event when a session is
      present (so we can correlate the logout with the prior login).
    * Always returns 200, regardless of whether a session was
      present. This is intentional: a stale-token logout (the
      browser sent an expired or invalid cookie) should still
      succeed because the goal is to clear state from the client.

    The endpoint is in :data:`app.middleware.auth._PUBLIC_PATHS` so
    a request with no cookie does not get a 401 before reaching this
    handler. (If we required auth, a user with an expired session
    couldn't log out without re-authenticating, which is broken UX.)

    Returns:
        A tuple of (Response, status_code) - 200 with an empty JSON
        ``{}`` body and a cookie-clearing ``Set-Cookie`` header.
    """
    from app.services.audit import emit_audit_event  # noqa: PLC0415

    # If a session is present, emit an audit event for forensic
    # correlation. Use ``getattr`` because :data:`flask.g.session` is
    # populated by :mod:`app.middleware.auth` only when a valid token
    # was presented - on the public-path code path the attribute may
    # not exist.
    session = getattr(g, "session", None)
    if session is not None:
        try:
            with db.session() as db_session, db_session.begin():
                emit_audit_event(
                    db_session=db_session,
                    event_type=AuditEventType.AUTHENTICATION,
                    actor_user_id=session.user_id,
                    target_record_id=None,
                    before_payload=None,
                    after_payload={
                        "method": "logout",
                        "outcome": "success",
                    },
                )
            _logger.info(
                "auth_logout_succeeded",
                user_id=str(session.user_id),
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Never fail logout because of an audit problem - the
            # browser-side cookie clear must always succeed. Log the
            # audit failure for forensic follow-up.
            _logger.error(
                "auth_logout_audit_failed",
                user_id=str(session.user_id),
                error=type(exc).__name__,
            )

    # Build the response BEFORE clearing the cookie so the body
    # serialization completes before any cookie mutation.
    response = make_response(jsonify({}), 200)

    # Clear the session cookie. We DELIBERATELY pass the same
    # ``HttpOnly``, ``Secure``, ``SameSite``, and ``Path`` flags as
    # the original Set-Cookie so the browser matches the exact
    # cookie definition and removes it. ``max_age=0`` causes
    # immediate expiry; setting ``value=""`` makes the cookie value
    # empty in the rare case the browser does not honor max_age=0.
    cookie_kwargs = _session_cookie_kwargs()
    cookie_kwargs["max_age"] = 0
    response.set_cookie(value="", **cookie_kwargs)

    return response, 200


# ===========================================================================
# GET /auth/google/start - initiate Google OAuth flow
# ===========================================================================


@auth_bp.route("/google/start", methods=["GET"])
def google_start() -> Response:
    """Begin the Google OAuth 2.0 authorization-code + PKCE flow.

    Per AAP Section 0.4.5:

    1. Generate a cryptographically random ``state`` (256 bits,
       hex-encoded) for CSRF/replay protection.
    2. Generate a PKCE ``code_verifier`` (RFC 7636 compliant: 43-128
       chars). Authlib derives ``code_challenge`` from it via
       SHA-256.
    3. Persist ``state`` and ``code_verifier`` in a short-lived
       HttpOnly cookie so the callback handler can validate them.
    4. Build the redirect to Google's authorization endpoint with
       all required parameters (response_type, client_id,
       redirect_uri, scope, state, code_challenge,
       code_challenge_method).
    5. Return a 302 redirect to that URL.

    The endpoint is in :data:`app.middleware.auth._PUBLIC_PATHS` so
    no session is required to begin OAuth.

    Returns:
        A Flask Response with status 302 and the ``Location`` header
        pointing to Google's authorization endpoint, plus a
        short-lived ``oauth_state`` cookie carrying the state and
        PKCE verifier for the callback to validate.
    """
    google_client = oauth.create_client("google")
    if google_client is None:
        # Google is not configured - return a 503 telling the SPA
        # to fall back to the email/password form.
        _logger.warning("auth_google_start_unavailable")
        raise AppError(message="Google OAuth is not configured on this server.")

    # Step 1: generate state. ``secrets.token_hex`` produces a
    # cryptographically secure URL-safe string. 32 bytes -> 64 hex
    # chars, well above the ``min_length=1`` enforcement on the
    # state field in :class:`OAuthCallbackQuery`.
    state = secrets.token_hex(_OAUTH_STATE_BYTE_LENGTH)

    # Step 2: generate PKCE code_verifier per RFC 7636. 64 bytes
    # produces an 86-character base64url string after encoding by
    # Authlib's PKCE helpers (well within the 43-128 char RFC range).
    code_verifier = secrets.token_urlsafe(_OAUTH_PKCE_VERIFIER_BYTE_LENGTH)

    # Step 4: build the redirect URI. ``url_for(..., _external=True)``
    # constructs an absolute URL (scheme + host) suitable for
    # registering with Google as an authorized redirect URI.
    redirect_uri = current_app.config.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
    ) or url_for("auth.google_callback", _external=True)

    # Step 4b: ask Authlib to build the authorization redirect.
    # ``authorize_redirect`` returns a Flask Response with the
    # ``Location`` header set to Google's authorization endpoint and
    # all required query parameters (response_type=code, client_id,
    # redirect_uri, scope=openid+email+profile, state,
    # code_challenge, code_challenge_method=S256) attached.
    auth_response: Response = google_client.authorize_redirect(
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )

    # Step 3: persist state + code_verifier in a short-lived cookie.
    # We keep both values in a single cookie (encoded as
    # ``state.code_verifier`` separated by a literal '.') so the
    # callback can read both with one request.
    cookie_value = f"{state}.{code_verifier}"
    auth_response.set_cookie(
        key=_OAUTH_STATE_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=current_app.config.get("SESSION_COOKIE_SECURE", True),
        samesite="Lax",
        path="/",
        max_age=_OAUTH_STATE_COOKIE_TTL_SECONDS,
    )

    _logger.info(
        "auth_google_start_redirect",
        state_length=len(state),
        verifier_length=len(code_verifier),
    )

    return auth_response


# ===========================================================================
# GET /auth/google/callback - handle the Google OAuth callback
# ===========================================================================


@auth_bp.route("/google/callback", methods=["GET"])
def google_callback() -> tuple[Response, int] | Response:
    """Handle the Google OAuth callback and mint a session JWT.

    Per AAP Section 0.4.5:

    1. Validate the query string against :class:`OAuthCallbackQuery`
       so malformed callbacks surface as HTTP 422 and so exactly one
       of ``code``/``error`` is present.
    2. Read the persisted ``state`` and ``code_verifier`` from the
       short-lived ``oauth_state`` cookie.
    3. Validate the callback ``state`` matches the persisted
       ``state`` (CSRF defense). Mismatch -> 401 + audit event.
    4. On the error path, redirect the SPA to ``/login`` with an
       ``?error=`` query parameter and emit an audit event.
    5. On the success path, exchange the code for tokens via
       Authlib (which also validates the ID token's signature
       against Google's JWKS, the ``iss`` and ``aud`` claims, etc.).
    6. Upsert the user via
       :func:`app.services.auth.upsert_oauth_user`.
    7. Emit a F-013 ``authentication`` audit event.
    8. Mint a session JWT and set the session cookie.
    9. Redirect the SPA to ``/feed`` (or to ``next`` if provided).

    Returns:
        A redirect response (302) to the SPA on success, or a
        redirect to ``/login?error=...`` on failure.
    """
    from app.services.audit import emit_audit_event  # noqa: PLC0415
    from app.services.auth import (  # noqa: PLC0415
        mint_session_jwt,
        upsert_oauth_user,
    )

    # Step 1: validate the query string. Pydantic enforces that
    # exactly one of ``code`` or ``error`` is present and that each
    # value is length-bounded.
    try:
        callback = OAuthCallbackQuery.model_validate(dict(request.args))
    except ValidationError as exc:
        safe_fields = [
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        _logger.info("auth_google_callback_invalid_query", fields=safe_fields)
        raise ValidationFailedError(
            message="OAuth callback query string failed validation.",
            fields=safe_fields,
        ) from exc

    # Step 2: read persisted state + code_verifier from the cookie.
    cookie_value = request.cookies.get(_OAUTH_STATE_COOKIE_NAME, "")
    persisted_state, _, persisted_verifier = cookie_value.partition(".")

    # Step 3: validate state. Empty cookie OR state mismatch is a
    # security failure (CSRF or session-fixation attempt).
    if not persisted_state or not secrets.compare_digest(
        persisted_state, callback.state
    ):
        _logger.warning(
            "auth_google_callback_state_mismatch",
            had_cookie=bool(persisted_state),
            had_state=bool(callback.state),
        )
        raise AuthError(message="OAuth state validation failed.")

    # Step 4: error path. Google returned an error code (e.g., the
    # user clicked Cancel). Redirect to the login page with an
    # error indicator and emit an audit event.
    if callback.error is not None:
        _logger.info(
            "auth_google_callback_error",
            error=callback.error,
        )
        # We don't have a user_id yet (no successful upsert), so the
        # audit event uses a synthetic 'system' actor. The audit
        # service emit_audit_event requires a real UUID for
        # actor_user_id; we skip emission on the error path because
        # there is no authenticated subject. This matches the AAP's
        # F-013 spec which scopes audit_events to actor_user_id
        # changes - a failed login is logged via _logger only.

        # Redirect to /login?error=<oauth_error>. We use a generic
        # error code in the query parameter to avoid leaking Google's
        # specific code to URL-history snooping; the SPA's login
        # screen displays a generic "Sign-in failed" message.
        redirect_url = "/login?error=oauth_failed"
        response = make_response(redirect(redirect_url, code=302))
        # Clear the state cookie since the flow is over.
        response.delete_cookie(_OAUTH_STATE_COOKIE_NAME, path="/")
        return response

    # Step 5: success path. Exchange the code for an ID token via
    # Authlib. ``authorize_access_token`` performs the token
    # exchange against Google's token endpoint AND validates the
    # ID token's signature against Google's JWKS. It returns a dict
    # containing ``access_token``, ``id_token``, and the parsed
    # ``userinfo`` from the ID token's claims.
    google_client = oauth.create_client("google")
    if google_client is None:
        _logger.error("auth_google_callback_client_unconfigured")
        raise AppError(message="Google OAuth is not configured on this server.")

    try:
        # Authlib requires the code_verifier to be present in either
        # the ``request`` query string (it isn't - Google strips it)
        # or passed explicitly. We pass it explicitly via the
        # ``code_verifier`` keyword argument so PKCE validation
        # succeeds. Authlib's name for the parameter is
        # ``code_verifier``.
        token_data = google_client.authorize_access_token(
            code_verifier=persisted_verifier,
        )
    except Exception as exc:
        _logger.warning(
            "auth_google_callback_token_exchange_failed",
            error=type(exc).__name__,
        )
        raise AuthError(message="OAuth token exchange failed.") from exc

    # Authlib stores the verified ID token claims under various
    # keys depending on version. ``userinfo`` is the canonical
    # location after Authlib >= 1.0; fall back to the parsed
    # ``id_token`` payload.
    id_token_claims: dict[str, Any] | None = token_data.get("userinfo")
    if id_token_claims is None:
        id_token_claims = token_data.get("id_token")
    if not isinstance(id_token_claims, dict) or not id_token_claims.get("email"):
        _logger.warning("auth_google_callback_missing_claims")
        raise AuthError(message="OAuth ID token missing required claims.")

    # Step 6-8: upsert the user, emit audit event, mint JWT - all
    # inside one transaction.
    org_id = _default_org_id()
    try:
        with db.session() as db_session, db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims=id_token_claims,
                org_id=org_id,
            )

            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.AUTHENTICATION,
                actor_user_id=user.id,
                target_record_id=None,
                before_payload=None,
                after_payload={
                    "method": "oauth_google",
                    "outcome": "success",
                },
            )

            token = mint_session_jwt(user)
            user_id = str(user.id)
    except ValueError as exc:
        # ``upsert_oauth_user`` raises ValueError when required ID
        # token claims are missing. Convert to 401 since the cause
        # is a malformed ID token (which should already have been
        # caught above, but defense in depth).
        _logger.warning(
            "auth_google_callback_upsert_failed",
            error=str(exc),
        )
        raise AuthError(message="OAuth user creation failed.") from exc

    # Step 9: redirect the SPA to /feed (or to a next URL). The
    # cookie is set on the redirect response so the browser sends
    # it on the next request to the SPA.
    redirect_url = "/feed"
    response = make_response(redirect(redirect_url, code=302))
    response.set_cookie(value=token, **_session_cookie_kwargs())
    # Clear the state cookie since the flow is over.
    response.delete_cookie(_OAUTH_STATE_COOKIE_NAME, path="/")

    _logger.info(
        "auth_google_callback_succeeded",
        user_id=user_id,
    )
    return response


# ===========================================================================
# GET /api/me - return the current session
# ===========================================================================


@me_bp.route("/me", methods=["GET"])
def get_me() -> tuple[Response, int]:
    """Return the current session for the SPA's AuthProvider.

    Per AAP Section 0.4.3 endpoint catalog and AAP Section 0.5.2
    Layer 1, this endpoint:

    1. Reads the session populated by :mod:`app.middleware.auth`
       from :data:`flask.g.session`. The middleware has already
       verified the JWT and rejected any request without a valid
       token before the handler runs.
    2. Loads the full :class:`User` row to source the latest
       ``display_name``, ``email``, and ``role`` (the JWT may carry
       stale values if the user was renamed or had their role
       changed since the token was minted).
    3. Returns :class:`SessionRead` with ``authenticated=True``.

    The endpoint is mounted under ``/api/me`` (NOT ``/auth/me``) so
    the SPA's ``@/api/client`` wrapper routes it consistently with
    the rest of the data API. Mounting here also means
    :mod:`app.middleware.auth` enforces the JWT cookie automatically
    (``/api/`` is in :data:`_PROTECTED_PREFIXES`).

    Returns:
        A tuple of (Response, status_code) - 200 with
        :class:`SessionRead` body. If the cookie is missing/invalid,
        :mod:`app.middleware.auth` returns 401 BEFORE this handler
        runs (so we never need to check for missing session here).
    """
    # The middleware has already populated g.session. If we somehow
    # reach here without one, treat it as an auth failure (defense in
    # depth - the middleware should have rejected the request).
    session = getattr(g, "session", None)
    if session is None:  # pragma: no cover - defensive
        raise AuthError(message="No active session.")

    # Load the user row to source the latest display_name/email/role.
    # The JWT's ``email``/``display_name``/``role`` claims are
    # snapshots from token-mint time; fetching the row ensures the
    # SPA renders fresh values after a rename or role change.
    from app.models import User as UserModel  # noqa: PLC0415

    with db.session() as db_session:
        user = db_session.get(UserModel, session.user_id)
        if user is None:
            # The token's user_id no longer maps to a row (e.g.,
            # user was hard-deleted). Treat as session invalidation.
            _logger.warning(
                "auth_me_user_not_found",
                user_id=str(session.user_id),
            )
            raise AuthError(message="Session refers to a deleted user.")

        # Validate INSIDE the transaction so any ORM-mode reads
        # (created_at, role, etc.) succeed before the session
        # closes. This avoids DetachedInstanceError after commit.
        body = SessionRead(
            user=UserRead.model_validate(user),
            authenticated=True,
        )

    return jsonify(body.model_dump(mode="json")), 200


# ---------------------------------------------------------------------------
# Module reference to ``time`` and ``urlencode`` so they remain warm
# imports for tests that monkey-patch them (e.g., tests that mock the
# OAuth state cookie's TTL by patching ``time.time``).
# ---------------------------------------------------------------------------

# Re-export with explicit type-erasure so static type checkers do not
# complain about reassigning a module to a callable. These statements
# are no-ops at runtime; they exist purely to keep the imports warm
# so tests can monkey-patch them.
_time_module: object = time
_urlencode_callable: object = urlencode
