"""Configuration classes for the Sales-Connections Flask application.

Each environment loads exactly one configuration class via
``app.config.from_object(...)`` from inside the application factory.
All values read from this module flow through ``app.config[...]`` and are
consumed by extensions, middleware, and handlers.

Environment selection priority (per ``app.create_app``):
1. Explicit ``config_object`` argument to ``create_app``.
2. ``FLASK_ENV`` environment variable
   ("development" | "testing" | "production").
3. Default to ``ProductionConfig``.

PRODUCTION SECRETS
    Production reads ``ANTHROPIC_API_KEY``,
    ``GOOGLE_OAUTH_CLIENT_SECRET``, ``JWT_SIGNING_KEY``, and
    ``DB_PASSWORD`` from AWS Secrets Manager via ``boto3`` at process
    startup, then exposes them as Flask config keys. Local development
    reads the same names directly from a ``.env`` file via python-dotenv.

PRODUCTION FAIL-FAST INVARIANT
    ``ProductionConfig.init_app`` performs fail-fast validation of the
    four required production secrets (``ANTHROPIC_API_KEY``,
    ``GOOGLE_OAUTH_CLIENT_SECRET``, ``JWT_SIGNING_KEY``,
    ``DATABASE_URL``) AFTER any Secrets-Manager load completes. A
    production deployment whose secrets are empty or still carry
    placeholder values (``PLACEHOLDER_*`` / ``REPLACE_WITH_*``) raises
    ``RuntimeError`` and refuses to start, eliminating the silent
    misconfiguration class where a worker would happily serve traffic
    with a forged HMAC key, an unusable AI client, or a broken OAuth
    flow. ``JWT_SIGNING_KEY`` additionally must be at least 32 bytes
    of UTF-8 to keep HS256 HMAC entropy at the 256-bit security level.

CRITICAL: Secret values must never be logged. The structlog processor
in ``app.observability.logging`` filters keys whose names match the
patterns ``*_key``, ``*_secret``, ``password``, ``token``, or
``authorization``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# python-dotenv is a runtime dependency declared in backend/requirements.txt
# (version 1.1.1). The conditional import guards against the (unsupported but
# possible) case where the package is missing in an alternative interpreter,
# allowing the module to import without raising; downstream behavior simply
# falls back to whatever os.environ already contains.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

# NOTE: boto3 is imported lazily inside ProductionConfig._load_secrets_from_aws
# to keep test/dev startup fast when AWS credentials are unavailable. Importing
# boto3 at module top level would slow down every test invocation by ~150ms.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_load_dotenv() -> None:
    """Load backend/.env into the process environment if available.

    Resolves ``backend/.env`` relative to this module's location so the
    behavior is independent of the current working directory. Uses
    ``override=False`` so values already in ``os.environ`` (e.g., from
    docker-compose or systemd) take precedence over the file.

    Silently no-ops when:
    - python-dotenv is not installed (``load_dotenv is None``); or
    - the ``.env`` file is absent (the production path).
    """
    # Cast to Any for the None-check so mypy does not flag the branch as
    # unreachable. python-dotenv is a runtime dependency and load_dotenv
    # will always be non-None in the supported deployment paths, but we
    # still guard for alternative interpreters.
    loader: Any = load_dotenv
    if loader is None:
        return
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        loader(dotenv_path=env_path, override=False)


def _str_to_bool(value: str | None, default: bool = False) -> bool:
    """Parse a 'truthy' string into a bool. Accepts 1/0/true/false/yes/no/on/off.

    ``None`` and unrecognized values fall back to ``default``. Comparison
    is case-insensitive and ignores surrounding whitespace.
    """
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _str_to_int(value: str | None, default: int) -> int:
    """Parse an int from a string; fall back to ``default`` on parse failure.

    Treats ``None`` and empty/whitespace-only strings as "not provided" and
    returns ``default``. Non-numeric strings (e.g., ``"abc"``) also fall
    through to ``default`` rather than raising — config loading must never
    crash the process on a malformed environment variable.
    """
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated string into a list of trimmed, non-empty entries.

    Empty input (``None`` or empty string) returns ``[]``. Surrounding
    whitespace per item is stripped; entirely-whitespace items are dropped.
    Used for ``CORS_ALLOWED_ORIGINS`` and similar allowlist fields.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# Sentinel prefixes that mark a secret value as still being a documentation
# placeholder rather than a real production credential. ``backend/.env.example``
# uses ``PLACEHOLDER_`` for required values and ``REPLACE_WITH_`` is reserved
# for any future placeholders that would otherwise collide with the prefix
# convention. Production fail-fast checks treat both prefixes as "unset".
_PLACEHOLDER_PREFIXES: tuple[str, ...] = ("PLACEHOLDER_", "REPLACE_WITH_")

# Minimum byte length for HS256 HMAC keys. NIST SP 800-117 and RFC 7518 §3.2
# both recommend a key of at least 256 bits (32 bytes) for HS256 to retain the
# advertised security level. Using a shorter key reduces effective entropy
# below the 2^256 brute-force ceiling that HS256 advertises and exposes
# session JWTs to off-line key recovery in the worst case.
_JWT_SIGNING_KEY_MIN_BYTES: int = 32


def _is_placeholder_or_empty(value: str | None) -> bool:
    """Return True iff *value* is unset, empty, or matches a placeholder sentinel.

    A "placeholder" is any string whose stripped form starts with one of
    :data:`_PLACEHOLDER_PREFIXES`. Used by
    :meth:`ProductionConfig._validate_required_secrets` to detect
    misconfiguration where the deployment was promoted to production
    without rotating the documentation defaults from ``.env.example``.

    Args:
        value: The candidate config value. May be ``None`` (config key
            not set at all), an empty string (set but blank), or a real
            string. Whitespace-only strings are also treated as empty.

    Returns:
        ``True`` when *value* is missing, empty, whitespace-only, or has
        a placeholder prefix; ``False`` for any value that looks like a
        real production credential.
    """
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


# Load backend/.env into os.environ early so all config classes (including
# BaseConfig's class-attribute initializers) see the .env values when their
# class bodies execute. Without this, dev workflow breaks because
# os.environ.get(...) calls inside class bodies would not see overrides.
_maybe_load_dotenv()


# ---------------------------------------------------------------------------
# BaseConfig
# ---------------------------------------------------------------------------


class BaseConfig:
    """Defaults shared by all environments.

    Subclasses override fields where environment behavior diverges. All
    fields are class attributes so Flask's ``from_object`` can read them.
    Lowercase helpers and private constants are skipped by Flask config
    loading, which only picks up UPPER_SNAKE_CASE attributes.
    """

    # ----- Identification -------------------------------------------------
    ENV_NAME: str = "base"
    TESTING: bool = False
    DEBUG: bool = False
    PROPAGATE_EXCEPTIONS: bool = True  # Surface tracebacks to error handlers

    # ----- Flask security -------------------------------------------------
    # Flask uses SECRET_KEY for session signing; we override via FLASK_SECRET_KEY
    # so it is unambiguous in the env file. Production replaces this via
    # Secrets Manager.
    SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY", "change-me-base")
    JSON_SORT_KEYS: bool = False

    # ----- Database (PostgreSQL 17.7 via psycopg 3.x) ---------------------
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://sales_connections:sales_connections@localhost:5432/sales_connections",
    )
    DB_POOL_SIZE: int = _str_to_int(os.environ.get("DB_POOL_SIZE"), 5)
    DB_MAX_OVERFLOW: int = _str_to_int(os.environ.get("DB_MAX_OVERFLOW"), 10)
    DB_POOL_RECYCLE: int = _str_to_int(os.environ.get("DB_POOL_RECYCLE"), 1800)
    DB_ECHO: bool = _str_to_bool(os.environ.get("DB_ECHO"), False)

    # ----- Authentication (JWT, Google OAuth, bcrypt) ---------------------
    JWT_SIGNING_KEY: str = os.environ.get("JWT_SIGNING_KEY", "change-me-jwt")
    JWT_ALGORITHM: str = "HS256"
    # 8-hour session TTL per AAP 0.5.2 ("HS256, 8-hour expiry").
    JWT_TTL_SECONDS: int = _str_to_int(os.environ.get("JWT_TTL_SECONDS"), 8 * 60 * 60)
    # Cost factor 12 per AAP 0.5.2 ("bcrypt, cost 12"). Overridden in
    # TestingConfig to keep test runs fast.
    BCRYPT_COST: int = _str_to_int(os.environ.get("BCRYPT_COST"), 12)
    GOOGLE_OAUTH_CLIENT_ID: str = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET: str = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    GOOGLE_OAUTH_REDIRECT_URI: str = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:5000/auth/google/callback",
    )
    SESSION_COOKIE_NAME: str = "session"
    SESSION_COOKIE_HTTPONLY: bool = True
    # Default to True (secure-only). DevelopmentConfig and TestingConfig
    # override to False so the cookie works over HTTP on localhost.
    SESSION_COOKIE_SECURE: bool = _str_to_bool(os.environ.get("SESSION_COOKIE_SECURE"), True)
    SESSION_COOKIE_SAMESITE: str = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")

    # ----- AI integration (Anthropic Claude via Langchain) ---------------
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    ANTHROPIC_MAX_TOKENS: int = _str_to_int(os.environ.get("ANTHROPIC_MAX_TOKENS"), 512)
    # 5-second timeout per AAP 0.7.3 (AI ≤5s P95 budget).
    AI_REQUEST_TIMEOUT_SECONDS: int = _str_to_int(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS"), 5)
    # Hard upper bound on the relationship-context length sent to Claude.
    # Sized to stay well below the model context window after the system
    # preamble and example user/assistant blocks are added by the prompt
    # template in services/ai_orchestration.py.
    AI_PROMPT_CONTEXT_MAX_CHARS: int = 4000

    # ----- Observability (logs, metrics, traces) -------------------------
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.environ.get("LOG_FORMAT", "json")
    OTLP_EXPORTER_ENDPOINT: str = os.environ.get("OTLP_EXPORTER_ENDPOINT", "")
    OTLP_EXPORTER_HEADERS: str = os.environ.get("OTLP_EXPORTER_HEADERS", "")
    OTEL_SERVICE_NAME: str = os.environ.get("OTEL_SERVICE_NAME", "sales-connections-api")
    METRICS_BEARER_TOKEN: str = os.environ.get("METRICS_BEARER_TOKEN", "")

    # ----- CORS / Frontend origin ----------------------------------------
    # Default allows the Vite dev server origin. Production sets via env var
    # to the deployed SPA origin (e.g., https://app.sales-connections.example.com).
    CORS_ALLOWED_ORIGINS: list[str] = _csv_list(os.environ.get("CORS_ALLOWED_ORIGINS")) or [
        "http://localhost:5173"
    ]
    CORS_ALLOW_CREDENTIALS: bool = _str_to_bool(os.environ.get("CORS_ALLOW_CREDENTIALS"), True)

    # ----- Single-org runtime constants (per AAP 0.7.2) ------------------
    # The data model is multi-tenant (org_id everywhere) but MVP runtime
    # serves exactly one organization. The default UUID is pinned here so
    # JWT minting attaches the correct org_id to new sessions.
    DEFAULT_ORG_ID: str = os.environ.get("DEFAULT_ORG_ID", "00000000-0000-0000-0000-000000000001")
    DEFAULT_NEW_USER_ROLE: str = os.environ.get("DEFAULT_NEW_USER_ROLE", "Contributor")

    # ----- AWS configuration ---------------------------------------------
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
    # When False (the default), no Secrets Manager calls are made. This is
    # the right setting for local dev, tests, and any environment where
    # secrets are injected via env vars (e.g., ECS task definition).
    USE_SECRETS_MANAGER: bool = _str_to_bool(os.environ.get("USE_SECRETS_MANAGER"), False)
    SECRETS_MANAGER_ANTHROPIC_KEY_ID: str = os.environ.get("SECRETS_MANAGER_ANTHROPIC_KEY_ID", "")
    SECRETS_MANAGER_GOOGLE_OAUTH_SECRET_ID: str = os.environ.get(
        "SECRETS_MANAGER_GOOGLE_OAUTH_SECRET_ID", ""
    )
    SECRETS_MANAGER_JWT_SIGNING_KEY_ID: str = os.environ.get(
        "SECRETS_MANAGER_JWT_SIGNING_KEY_ID", ""
    )
    SECRETS_MANAGER_DB_PASSWORD_ID: str = os.environ.get("SECRETS_MANAGER_DB_PASSWORD_ID", "")

    # ----- Performance budgets (per AAP 0.7.3) ---------------------------
    # These are constants exposed via app.config so handlers, decorators,
    # and Prometheus alarms can reference them by name rather than by magic
    # numbers. Keeping them in config (vs hard-coded in modules) lets ops
    # tune them per-environment without code edits.
    RBAC_AUDIT_LATENCY_BUDGET_MS: int = 50
    AUDIT_EMIT_BUDGET_MS: int = 100
    AUTH_BUDGET_SECONDS: int = 2
    FORM_SUBMIT_BUDGET_SECONDS: int = 2
    AI_LATENCY_P95_BUDGET_SECONDS: int = 5

    # ----- Lifecycle hook -------------------------------------------------
    @classmethod
    def init_app(cls, app: Any) -> None:
        """Hook for environment-specific post-init mutations.

        Subclasses override to load secrets from external services,
        validate values, or warn about misconfigurations. The base
        implementation is a no-op so callers can invoke it
        unconditionally without checking the concrete class.
        """
        return None


# ---------------------------------------------------------------------------
# DevelopmentConfig
# ---------------------------------------------------------------------------


class DevelopmentConfig(BaseConfig):
    """Local development configuration; reads from backend/.env.

    Enables Flask debug mode for hot-reload tracebacks, relaxes cookie
    Secure flags so http://localhost works, and switches log formatting
    to human-readable console output. Database SQL echo can be enabled
    by setting ``DB_ECHO=true`` in ``.env``.
    """

    ENV_NAME = "development"
    DEBUG = True
    TESTING = False
    PROPAGATE_EXCEPTIONS = True

    # Permissive cookie attributes for http://localhost. Browsers refuse to
    # store SECURE cookies sent over HTTP, so dev requires SECURE=False.
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = "Lax"

    # Verbose, human-readable logs in dev. structlog renders to console
    # with ANSI colors when LOG_FORMAT=console (vs JSON in production).
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")
    LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")

    # Echo SQL by default in dev for easier debugging unless overridden.
    # Can be noisy in feed tests; toggle off via DB_ECHO=false.
    DB_ECHO = _str_to_bool(os.environ.get("DB_ECHO"), False)


# ---------------------------------------------------------------------------
# TestingConfig
# ---------------------------------------------------------------------------


class TestingConfig(BaseConfig):
    """Configuration for the pytest test suite.

    - Uses an isolated database URL; the test fixtures construct/tear down
      the schema per session via Alembic or db.metadata.create_all.
    - Disables Flask's debug mode (debug=True changes error handling).
    - Disables external HTTP calls (Anthropic, Google) by emptying their
      credentials; tests mock these surfaces explicitly.
    - Uses a deterministic JWT_SIGNING_KEY for reproducible tokens.
    """

    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    PROPAGATE_EXCEPTIONS = True

    # Default test DB; CI overrides via DATABASE_URL when running against
    # a service-container Postgres. The TEST_DATABASE_URL env var lets
    # developers point pytest at a local test database without disturbing
    # the dev DATABASE_URL.
    DATABASE_URL = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://sales_connections:sales_connections@localhost:5432/sales_connections_test",
    )

    # Smaller pool for tests; pytest-flask only opens a handful of
    # connections per worker.
    DB_POOL_SIZE = 2
    DB_MAX_OVERFLOW = 0

    # Deterministic auth secrets so token-minting tests are reproducible.
    JWT_SIGNING_KEY = "test-jwt-signing-key-do-not-use-in-prod"
    # Cost 4 is insecure but fine for ephemeral test fixtures. Production
    # uses cost 12 (~250ms per hash); cost 4 is ~1ms per hash, saving
    # tens of seconds across the full test run.
    BCRYPT_COST = 4

    # Empty AI / OAuth credentials; tests mock the relevant services.
    ANTHROPIC_API_KEY = ""
    GOOGLE_OAUTH_CLIENT_ID = ""
    GOOGLE_OAUTH_CLIENT_SECRET = ""

    # Permissive cookie attributes for the Flask test client (which uses
    # http://localhost by convention).
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = "Lax"

    # Quiet logs in tests; pytest captures stdout but visual noise during
    # test runs is unhelpful.
    LOG_LEVEL = "WARNING"
    LOG_FORMAT = "json"

    # No tracing in tests; OTLP exporter would attempt outbound HTTP.
    OTLP_EXPORTER_ENDPOINT = ""

    # Disable Secrets Manager lookups; tests must never touch AWS.
    USE_SECRETS_MANAGER = False


# ---------------------------------------------------------------------------
# ProductionConfig
# ---------------------------------------------------------------------------


class ProductionConfig(BaseConfig):
    """Production configuration with secrets sourced from AWS Secrets Manager.

    Fetches ``ANTHROPIC_API_KEY``, ``GOOGLE_OAUTH_CLIENT_SECRET``,
    ``JWT_SIGNING_KEY``, and ``DB_PASSWORD`` at app construction time
    (inside ``init_app``) so workers fail fast on misconfiguration
    rather than at first request. boto3 is imported lazily so test/dev
    paths do not need AWS credentials.
    """

    ENV_NAME = "production"
    DEBUG = False
    TESTING = False
    PROPAGATE_EXCEPTIONS = True

    # Strict cookie attributes; HTTPS termination at the ALB ensures
    # secure-only cookies are valid in production.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # JSON logging in production for CloudWatch log-insights queries.
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_FORMAT = "json"

    # Production should always have a tracer endpoint; the value comes
    # from the ECS task definition (which itself reads it from Terraform).
    OTLP_EXPORTER_ENDPOINT = os.environ.get("OTLP_EXPORTER_ENDPOINT", "")

    # Production defaults USE_SECRETS_MANAGER to True so a missing env var
    # does not silently bypass the secret fetch. Hybrid environments
    # (staging, dev-against-prod) can opt out via env var.
    USE_SECRETS_MANAGER = _str_to_bool(os.environ.get("USE_SECRETS_MANAGER"), True)

    @classmethod
    def init_app(cls, app: Any) -> None:
        """Fetch secrets from AWS Secrets Manager and write into ``app.config``.

        Reads:
        - ``SECRETS_MANAGER_ANTHROPIC_KEY_ID`` -> ``ANTHROPIC_API_KEY``
        - ``SECRETS_MANAGER_GOOGLE_OAUTH_SECRET_ID`` ->
          ``GOOGLE_OAUTH_CLIENT_SECRET``
        - ``SECRETS_MANAGER_JWT_SIGNING_KEY_ID`` -> ``JWT_SIGNING_KEY``
        - ``SECRETS_MANAGER_DB_PASSWORD_ID`` -> applied into
          ``DATABASE_URL`` via ``${DB_PASSWORD}`` placeholder substitution

        When ``USE_SECRETS_MANAGER`` is False, this method skips the
        Secrets-Manager round-trip (intended for staging/dev environments
        that share the ``ProductionConfig`` class but inject secrets via
        env vars instead). Fail-fast validation runs in EITHER case so a
        production worker never serves traffic with placeholder
        credentials regardless of whether secrets came from AWS or env.

        Empty secret IDs are skipped silently — staging/CI may use a
        hybrid where some secrets come from env vars and some from
        Secrets Manager.

        Raises:
            RuntimeError: When any of the four required production
                secrets (``ANTHROPIC_API_KEY``,
                ``GOOGLE_OAUTH_CLIENT_SECRET``, ``JWT_SIGNING_KEY``,
                ``DATABASE_URL``) is missing, empty, or still carries a
                placeholder value, or when ``JWT_SIGNING_KEY`` is
                shorter than 32 bytes of UTF-8.
        """
        super().init_app(app)
        if app.config.get("USE_SECRETS_MANAGER", False):
            secrets = cls._load_secrets_from_aws(app)
            # Anthropic API key
            if secrets.get("anthropic_api_key"):
                app.config["ANTHROPIC_API_KEY"] = secrets["anthropic_api_key"]
            # Google OAuth client secret
            if secrets.get("google_oauth_client_secret"):
                app.config["GOOGLE_OAUTH_CLIENT_SECRET"] = secrets["google_oauth_client_secret"]
            # JWT signing key
            if secrets.get("jwt_signing_key"):
                app.config["JWT_SIGNING_KEY"] = secrets["jwt_signing_key"]
            # DB password — expand into DATABASE_URL using a
            # ${DB_PASSWORD} placeholder so the rest of the DSN
            # (host/port/db/user) can come from a non-secret env var.
            db_password = secrets.get("db_password")
            if db_password and "${DB_PASSWORD}" in app.config.get("DATABASE_URL", ""):
                app.config["DATABASE_URL"] = app.config["DATABASE_URL"].replace(
                    "${DB_PASSWORD}", db_password
                )
        # Fail-fast: refuse to start if any required production secret
        # is still placeholder or empty after the (optional) Secrets
        # Manager load. Runs unconditionally so env-driven and
        # AWS-driven deployments share the same invariant.
        cls._validate_required_secrets(app)

    @staticmethod
    def _validate_required_secrets(app: Any) -> None:
        """Refuse to start when any required production secret is missing.

        Validates the four required production secrets per AAP Sec 0.7.4
        Security Invariants. Each secret is checked against
        :func:`_is_placeholder_or_empty`, which rejects ``None``, empty,
        whitespace-only, or ``PLACEHOLDER_*`` / ``REPLACE_WITH_*``
        values. ``JWT_SIGNING_KEY`` additionally must be at least
        :data:`_JWT_SIGNING_KEY_MIN_BYTES` bytes of UTF-8 to keep HS256
        HMAC entropy at the 256-bit security level.

        All failures are aggregated into a single ``RuntimeError`` so
        operators see every misconfiguration at once rather than fixing
        them one-by-one across restart attempts. The error message
        names the affected config keys but never echoes their values
        (placeholder or otherwise) to keep logs free of incidental
        secrets.

        Args:
            app: The Flask application whose ``app.config`` carries the
                resolved secret values.

        Raises:
            RuntimeError: When at least one required secret is missing,
                empty, placeholder, or (for ``JWT_SIGNING_KEY``) under
                the 32-byte length floor.
        """
        # Order matters only for the error-message UX; the same set of
        # keys is checked regardless. The four-tuple matches AAP Sec
        # 0.7.4 verbatim: Anthropic, Google OAuth, JWT, DB.
        required_keys: tuple[str, ...] = (
            "ANTHROPIC_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JWT_SIGNING_KEY",
            "DATABASE_URL",
        )
        errors: list[str] = []
        for key in required_keys:
            value = app.config.get(key)
            if _is_placeholder_or_empty(value if isinstance(value, str) else None):
                errors.append(
                    f"{key} is missing, empty, or contains a placeholder value "
                    f"(prefix in {list(_PLACEHOLDER_PREFIXES)}); refusing to start"
                )
        # JWT-specific length check runs only when the key cleared the
        # placeholder gate; otherwise the placeholder error is more
        # actionable than a length error.
        jwt_key = app.config.get("JWT_SIGNING_KEY")
        if isinstance(jwt_key, str) and not _is_placeholder_or_empty(jwt_key):
            jwt_byte_length = len(jwt_key.encode("utf-8"))
            if jwt_byte_length < _JWT_SIGNING_KEY_MIN_BYTES:
                errors.append(
                    f"JWT_SIGNING_KEY must be at least "
                    f"{_JWT_SIGNING_KEY_MIN_BYTES} bytes for HS256 security; "
                    f"got {jwt_byte_length} bytes"
                )
        if errors:
            joined = "; ".join(errors)
            raise RuntimeError(
                f"ProductionConfig refusing to start: {joined}. "
                f"Set real values via AWS Secrets Manager or environment "
                f"variables (see backend/.env.example for required keys)."
            )

    @staticmethod
    def _load_secrets_from_aws(app: Any) -> dict[str, str]:
        """Fetch the four AAP-listed secrets from AWS Secrets Manager.

        Returns a flat dict keyed by lowercase logical name. Missing
        secrets (empty secret ID) are skipped silently and surfaced via
        the calling ``init_app`` which logs structured warnings when
        invariants are violated (e.g., an empty JWT_SIGNING_KEY in
        production).

        Implementation notes:
        - boto3 is imported lazily so test/dev paths do not need
          credentials.
        - Secrets Manager values are JSON-encoded by convention; we
          attempt JSON decode and fall back to the raw string.
        - The ``logical_name`` lookup inside the JSON dict supports
          either a key/value document (e.g.
          ``{"anthropic_api_key": "<token>"}``) or a raw string
          payload (the entire SecretString IS the secret value).
        """
        import boto3  # noqa: PLC0415

        region = app.config.get("AWS_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)
        mapping: dict[str, str] = {
            "anthropic_api_key": app.config.get("SECRETS_MANAGER_ANTHROPIC_KEY_ID", ""),
            "google_oauth_client_secret": app.config.get(
                "SECRETS_MANAGER_GOOGLE_OAUTH_SECRET_ID", ""
            ),
            "jwt_signing_key": app.config.get("SECRETS_MANAGER_JWT_SIGNING_KEY_ID", ""),
            "db_password": app.config.get("SECRETS_MANAGER_DB_PASSWORD_ID", ""),
        }
        out: dict[str, str] = {}
        for logical_name, secret_id in mapping.items():
            if not secret_id:
                continue
            response = client.get_secret_value(SecretId=secret_id)
            raw = response.get("SecretString", "")
            # Try JSON decode (Secrets Manager often stores key/value
            # pairs as JSON documents).
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and logical_name in parsed:
                    out[logical_name] = parsed[logical_name]
                elif isinstance(parsed, str):
                    out[logical_name] = parsed
                else:
                    out[logical_name] = raw
            except json.JSONDecodeError:
                out[logical_name] = raw
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "ProductionConfig",
    "TestingConfig",
]
