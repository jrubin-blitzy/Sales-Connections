"""API tests for the health blueprint.

Verifies liveness (``/healthz``) and readiness (``/readyz``) endpoints used
by:

- AWS ALB target group health checks
- ECS task health checks
- Operator smoke tests during deploys

Key invariants verified by this module:

- Both endpoints are PUBLIC: they MUST NOT require authentication.
- ``/healthz`` is unconditional: it MUST return 200 even when the database
  is unreachable or in a degraded state (true liveness, not readiness).
- ``/readyz`` performs a real SQL round-trip (``SELECT 1``) and returns 503
  if the database is unreachable, the connection pool is exhausted, or
  the round-trip exceeds the 1-second budget.
- ``/readyz`` response on DB failure does NOT leak the full SQLAlchemy
  exception message (which can include connection strings, table names,
  internal SQL fragments). Only an exception-class name (e.g.,
  ``OperationalError``) is acceptable.

Implementation notes
--------------------
The health blueprint is registered WITHOUT a ``url_prefix``; the routes
mount at the application root (``/healthz`` and ``/readyz``).

The production handler in :mod:`app.api.health` calls
``db.engine.connect()`` followed by ``conn.execute(text("SELECT 1"))`` and
catches ``sqlalchemy.exc.SQLAlchemyError``. To simulate failure, tests
substitute the underlying engine reference (``db._engine``) with a
``MagicMock`` whose ``connect`` method either raises (connection failure)
or returns a context manager whose ``execute`` raises (query failure).

Patching the public ``db.engine`` property directly is not possible
because the property has no setter; patching the underlying attribute
``db._engine`` achieves the same effect because the property simply
returns ``self._engine``.

Per AAP Section 0.5.2 Layer 0:

    "/healthz returns 200 unconditionally; /readyz returns 200 only if a
    SELECT 1 round-trip to RDS succeeds within 1 second."

Per AAP Section 0.7.5 (Observability rule):

    "The application is not complete until it is observable. Every
    deliverable must include ... health/readiness checks ..."
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.extensions import db

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
# The exact paths exposed by the health blueprint. Defined here so tests
# read like prose ("get /healthz") rather than carrying a string literal at
# every call site.
_HEALTHZ_PATH: str = "/healthz"
_READYZ_PATH: str = "/readyz"

# A path that is REQUIRED to be authenticated. Used by the auth-bypass
# sanity check to confirm the auth middleware is actually engaged on
# non-public routes (so the bypass tests for /healthz and /readyz mean
# something).
_PROTECTED_API_PATH: str = "/api/connections"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_mock_engine_with_connection_failure(orig_message: str) -> MagicMock:
    """Build a mock engine whose ``connect()`` raises ``OperationalError``.

    The ``OperationalError`` constructor signature is
    ``OperationalError(statement, params, orig)``. Tests use ``orig_message``
    to control the embedded text that the production handler MUST NOT echo
    to the response body. The ``statement`` argument carries the same text
    so the secrecy guarantee is verified against both surfaces.
    """
    mock_engine = MagicMock(name="mock_engine")
    mock_engine.connect.side_effect = OperationalError(
        orig_message,
        None,
        Exception(orig_message),
    )
    return mock_engine


def _make_mock_engine_with_query_failure(orig_message: str) -> MagicMock:
    """Build a mock engine whose ``execute()`` raises ``SQLAlchemyError``.

    The mock returns a context-manager-shaped object from ``connect()``;
    the connection's ``execute`` call raises ``SQLAlchemyError`` to simulate
    a failed ``SELECT 1`` round-trip after a successful TCP handshake.
    """
    mock_engine = MagicMock(name="mock_engine")
    mock_conn = MagicMock(name="mock_conn")
    mock_conn.execute.side_effect = SQLAlchemyError(orig_message)

    # ``with db.engine.connect() as conn:`` requires the return value of
    # ``connect()`` to be a context manager. ``MagicMock`` supports the
    # context-manager protocol natively via ``__enter__`` / ``__exit__``;
    # configuring ``__enter__`` to return ``mock_conn`` lets the production
    # handler see ``conn`` as the alias.
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    return mock_engine


# ---------------------------------------------------------------------------
# TestLivenessEndpoint - GET /healthz
# ---------------------------------------------------------------------------


class TestLivenessEndpoint:
    """Tests for ``GET /healthz`` (liveness probe).

    The liveness probe is unconditionally successful while the WSGI
    worker is alive. It MUST NOT touch the database, MUST NOT require
    authentication, and MUST respond with HTTP 200 in every scenario
    that has a Flask app at all.
    """

    def test_healthz_returns_200_unconditionally(self, client: Any) -> None:
        """Anonymous GET /healthz returns 200 with a JSON body."""
        response = client.get(_HEALTHZ_PATH)
        assert response.status_code == 200

    def test_healthz_no_authentication_required(self, client: Any) -> None:
        """The liveness path is in the auth-bypass allowlist.

        An unauthenticated client (no session cookie, no Authorization
        header) MUST receive a 200, NOT a 401. This proves the auth
        middleware's ``_PUBLIC_PATHS`` whitelist includes /healthz.
        """
        response = client.get(_HEALTHZ_PATH)
        assert response.status_code == 200, (
            "Liveness probe should bypass authentication, but got "
            f"status {response.status_code} (likely auth middleware "
            "incorrectly blocking /healthz)."
        )
        assert response.status_code != 401

    def test_healthz_works_when_db_is_down(self, client: Any) -> None:
        """Liveness MUST succeed even when the database is unreachable.

        We simulate DB downtime by replacing the underlying SQLAlchemy
        engine with a mock whose ``connect()`` raises ``OperationalError``,
        then verify (a) the endpoint still returns 200 and (b) the
        liveness handler never invoked ``db.engine.connect()`` (because
        it does not depend on the database at all).
        """
        mock_engine = _make_mock_engine_with_connection_failure(
            "simulated outage: connection refused"
        )
        with patch.object(db, "_engine", new=mock_engine):
            response = client.get(_HEALTHZ_PATH)

        assert response.status_code == 200, (
            "Liveness probe MUST succeed even when the DB is down; "
            "the handler must not depend on database connectivity."
        )
        # Crucial invariant: the liveness handler never even attempts
        # to acquire a connection. If this assertion fails, the handler
        # is doing too much work and would create false-negative
        # liveness failures during DB outages, causing ECS to kill
        # otherwise-healthy workers.
        mock_engine.connect.assert_not_called()

    def test_healthz_response_body_shape(self, client: Any) -> None:
        """Liveness response is JSON with a stable, documented shape.

        Per :func:`app.api.health.liveness` the success body is::

            {"status": "ok", "service": "<otel service name>"}

        Tests pin the keys but tolerate the service-name value being
        configuration-dependent.
        """
        response = client.get(_HEALTHZ_PATH)
        assert response.status_code == 200
        # Content-Type MUST be JSON (jsonify in Flask always sets this).
        assert response.is_json, (
            "Liveness response MUST be JSON; got Content-Type "
            f"{response.headers.get('Content-Type')}"
        )
        body = response.get_json()
        assert isinstance(body, dict)
        assert body.get("status") == "ok"
        # The service name is sourced from OTEL_SERVICE_NAME or defaults
        # to "sales-connections-api"; tolerate both.
        assert "service" in body
        assert isinstance(body["service"], str)
        assert body["service"]  # non-empty

    def test_healthz_no_correlation_id_required(self, client: Any) -> None:
        """Liveness does NOT require a client-supplied correlation header.

        The correlation middleware auto-generates a UUID when no inbound
        ``X-Correlation-Id`` header is present, and echoes the resolved
        value in the response. This test confirms that flow works for
        the public liveness path (i.e., the correlation middleware runs
        even when auth is skipped).
        """
        response = client.get(_HEALTHZ_PATH)
        assert response.status_code == 200
        # The middleware echoes the correlation ID back; tolerate either
        # an explicit header or its absence (some test configurations
        # may strip per-request headers in test client mode), but if
        # present the value must be a non-empty string.
        echoed = response.headers.get("X-Correlation-Id")
        if echoed is not None:
            assert isinstance(echoed, str)
            assert echoed.strip(), (
                "X-Correlation-Id was set but empty; correlation "
                "middleware should generate a fresh UUID when absent."
            )

    def test_healthz_via_post_returns_405(self, client: Any) -> None:
        """POST /healthz is not a valid liveness invocation.

        The blueprint declares only the GET method, so POSTing to the
        same path MUST yield 405 Method Not Allowed (Flask + Werkzeug
        produce this automatically).
        """
        response = client.post(_HEALTHZ_PATH)
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# TestReadinessEndpoint - GET /readyz
# ---------------------------------------------------------------------------


class TestReadinessEndpoint:
    """Tests for ``GET /readyz`` (readiness probe).

    The readiness probe MUST verify database connectivity via a bounded
    ``SELECT 1`` round-trip. It returns 200 when the database is healthy
    and 503 when any of the following holds:

    - The connection pool is exhausted.
    - The TCP/TLS handshake fails (connection refused, DNS failure).
    - The query itself fails (driver error, broken statement).
    - The round-trip exceeds the 1-second budget defined in
      :data:`app.api.health._READYZ_DB_TIMEOUT_SECONDS`.

    On 503 the response body MUST NOT echo SQLAlchemy or psycopg
    internals (DSN, host, credentials embedded in error messages).
    """

    def test_readyz_returns_200_with_healthy_db(self, client: Any) -> None:
        """A working DB connection produces a 200 response.

        Relies on the ``client`` fixture from conftest providing a Flask
        app with an initialized database engine that points at the test
        Postgres instance. The fixture's per-test SAVEPOINT isolation is
        independent of the engine-level connection used by the readiness
        probe (which acquires a fresh connection from the pool).
        """
        response = client.get(_READYZ_PATH)
        assert response.status_code == 200, (
            f"Expected 200 with a healthy DB, got {response.status_code}; "
            f"body={response.get_data(as_text=True)!r}"
        )

    def test_readyz_no_authentication_required(self, client: Any) -> None:
        """The readiness path is in the auth-bypass allowlist.

        Anonymous requests MUST NOT receive a 401. Status 200 (healthy)
        or 503 (degraded) are both acceptable, but never 401 or 403.
        """
        response = client.get(_READYZ_PATH)
        assert response.status_code != 401, (
            "Readiness probe MUST bypass authentication (auth middleware "
            "_PUBLIC_PATHS allowlist); got 401 which means the bypass is "
            "broken."
        )
        assert response.status_code != 403
        assert response.status_code in {200, 503}

    def test_readyz_returns_503_on_db_connection_failure(self, client: Any) -> None:
        """A connection failure surfaces as HTTP 503.

        Replaces the engine with a mock whose ``connect()`` raises
        ``OperationalError`` to simulate a refused TCP connection (the
        most common DB outage symptom in production). The handler MUST
        catch the exception and convert it to a 503, NOT propagate to a
        500.
        """
        mock_engine = _make_mock_engine_with_connection_failure(
            "simulated outage: connection refused"
        )
        with patch.object(db, "_engine", new=mock_engine):
            response = client.get(_READYZ_PATH)

        assert response.status_code == 503
        assert response.is_json
        body: dict[str, Any] = response.get_json()
        # Verify the failure was reported through the documented shape:
        # {"status": "degraded", "service": ..., "checks": {"database":
        # {"status": "error", ..., "error": "OperationalError"}}}
        assert body.get("status") == "degraded"
        db_check = body.get("checks", {}).get("database", {})
        assert db_check.get("status") == "error"
        # The error field carries the exception CLASS NAME only.
        assert db_check.get("error") == "OperationalError", (
            "Expected the exception class name 'OperationalError' in the "
            f"response body, got {db_check.get('error')!r}; the readiness "
            "handler should not echo full SQLAlchemy error messages."
        )

    def test_readyz_returns_503_when_select_1_raises(self, client: Any) -> None:
        """A query-time SQLAlchemy error surfaces as HTTP 503.

        Simulates the case where the TCP handshake succeeds but the
        ``SELECT 1`` query itself fails (driver error, broken cursor,
        statement timeout). The handler MUST catch ``SQLAlchemyError``
        and convert to 503, just like for ``OperationalError``.
        """
        mock_engine = _make_mock_engine_with_query_failure("simulated query failure")
        with patch.object(db, "_engine", new=mock_engine):
            response = client.get(_READYZ_PATH)

        assert response.status_code == 503
        assert response.is_json
        body: dict[str, Any] = response.get_json()
        assert body.get("status") == "degraded"
        db_check = body.get("checks", {}).get("database", {})
        assert db_check.get("status") == "error"
        # The reported class name is the most specific subclass that
        # was raised; SQLAlchemyError is the documented top-level
        # parent so the test accepts either it or any subclass name.
        reported_error = db_check.get("error")
        assert isinstance(reported_error, str)
        assert reported_error, "Error class name must be a non-empty string"

    def test_readyz_returns_503_on_timeout_exceeding_1_second_budget(self, client: Any) -> None:
        """A round-trip exceeding the 1-second budget returns 503.

        Per AAP Section 0.5.2 Layer 0, the readiness probe MUST complete
        within 1 second; a successful but slow round-trip is treated as
        a degraded result so the orchestrator can route around the
        slow worker.

        Implementation: the handler uses ``time.perf_counter()`` to
        measure wall-clock duration. We patch ``time.perf_counter``
        inside the health module to return controlled values whose
        difference exceeds the 1-second threshold, simulating a slow
        round-trip without actually sleeping in the test process.
        """
        # The handler calls ``time.perf_counter()`` twice (at start
        # and at end of the DB round-trip); we configure a callable
        # side_effect that returns a sequence of monotonically-
        # increasing values so the difference between the FIRST TWO
        # calls inside the health probe exceeds the 1.0 s budget.
        #
        # Patching ``app.api.health.time.perf_counter`` replaces the
        # ``perf_counter`` attribute on the shared ``time`` module
        # globally, so other callers of ``time.perf_counter()`` (e.g.,
        # the metrics middleware before/after request hooks) also
        # consume from the iterator. Using a callable side_effect
        # rather than a finite list avoids ``StopIteration`` when the
        # number of incidental calls grows.
        call_count = {"n": 0}

        def fake_perf_counter() -> float:
            # Returns values such that the FIRST and SECOND health-
            # probe calls span 1.5 s. Subsequent metrics-middleware
            # calls receive larger values and do not interfere.
            call_count["n"] += 1
            n = call_count["n"]
            # Skip metrics before_request (n=1) by returning an early
            # value, then the health probe sees a 0.0 -> 1.5 jump on
            # its two calls. We over-allocate by giving 0 for n<=1,
            # 0.0 for n==2, 1.5 for n==3, then increasing values.
            if n <= 1:
                return 0.0
            if n == 2:
                return 0.0
            if n == 3:
                return 1.5
            return 1.5 + (n - 3) * 0.001

        with patch("app.api.health.time.perf_counter", side_effect=fake_perf_counter):
            response = client.get(_READYZ_PATH)

        assert response.status_code == 503, (
            f"Expected 503 when DB round-trip exceeds 1s budget, got "
            f"{response.status_code}; the handler must convert a slow "
            "but technically successful round-trip into a 503."
        )
        body: dict[str, Any] = response.get_json()
        assert body.get("status") == "degraded"
        db_check = body.get("checks", {}).get("database", {})
        assert db_check.get("status") == "error"
        # The handler reports a "TimeoutExceeded" sentinel for budget
        # violations; this is distinct from a SQLAlchemy exception class
        # name so operators can differentiate slow-DB from broken-DB.
        assert db_check.get("error") == "TimeoutExceeded", (
            "Expected the budget-violation sentinel 'TimeoutExceeded' in "
            f"the response, got {db_check.get('error')!r}."
        )

    def test_readyz_does_not_leak_sql_internals(self, client: Any) -> None:
        """Failure responses MUST NOT echo SQL/credential internals.

        The readiness probe is reachable (over the AWS VPC private
        network in production, but defensively so) by orchestration
        agents that may be misconfigured to hit a public endpoint. To
        defend against an accidentally-public readiness endpoint
        leaking infrastructure details, the handler MUST strip the
        full exception message and surface only the exception class
        name. This test forces an ``OperationalError`` whose message
        contains credential-like substrings and asserts none appear in
        the response body.
        """
        leaky_message = (
            "FATAL: password authentication failed for user 'sales_app' "
            "from host '10.0.1.42' (DSN postgresql://sales_app:hunter2@"
            "rds.example.internal:5432/sales_connections)"
        )
        mock_engine = _make_mock_engine_with_connection_failure(leaky_message)
        with patch.object(db, "_engine", new=mock_engine):
            response = client.get(_READYZ_PATH)

        assert response.status_code == 503
        # Compare against the FULL response body as a string so that
        # PII appearing in any nested key (status, service, checks,
        # checks.database.error, etc.) is caught.
        body_text = response.get_data(as_text=True)

        # CRITICAL: the leaked credentials, hostname, and password MUST
        # NOT appear anywhere in the response body. Each forbidden
        # substring is checked individually so failure messages name
        # the leak precisely.
        assert "sales_app" not in body_text, (
            "Username 'sales_app' leaked in readiness response body."
        )
        assert "10.0.1.42" not in body_text, (
            "IP address '10.0.1.42' leaked in readiness response body."
        )
        assert "hunter2" not in body_text, "Password 'hunter2' leaked in readiness response body."
        assert "rds.example.internal" not in body_text, (
            "Hostname 'rds.example.internal' leaked in readiness response."
        )
        assert "password" not in body_text.lower(), (
            "Word 'password' appears in body; the handler must not echo "
            "credential-related text from exception messages."
        )

        # Acceptable contents: exception CLASS NAME ONLY.
        body: dict[str, Any] = response.get_json()
        db_check = body.get("checks", {}).get("database", {})
        assert db_check.get("error") == "OperationalError"

    def test_readyz_response_body_shape_on_success(self, client: Any) -> None:
        """Success response shape pins the documented JSON contract.

        Per :func:`app.api.health.readiness`, the success body is::

            {
                "status": "ok",
                "service": "<otel service name>",
                "checks": {
                    "database": {"status": "ok", "duration_ms": <float>}
                },
            }
        """
        response = client.get(_READYZ_PATH)
        assert response.status_code == 200
        assert response.is_json
        body: dict[str, Any] = response.get_json()
        assert isinstance(body, dict)
        # Top-level required keys.
        assert body.get("status") == "ok"
        assert "service" in body
        assert isinstance(body["service"], str)
        assert "checks" in body
        assert isinstance(body["checks"], dict)
        # Nested database check structure.
        db_check = body["checks"].get("database")
        assert isinstance(db_check, dict)
        assert db_check.get("status") == "ok"
        assert "duration_ms" in db_check
        assert isinstance(db_check["duration_ms"], int | float)
        assert db_check["duration_ms"] >= 0.0

    def test_readyz_response_body_shape_on_failure(self, client: Any) -> None:
        """Failure response shape pins the documented JSON contract.

        Per :func:`app.api.health.readiness`, the failure body is::

            {
                "status": "degraded",
                "service": "<otel service name>",
                "checks": {
                    "database": {
                        "status": "error",
                        "duration_ms": <float>,
                        "error": "<class name>",
                    }
                },
            }

        Note: this shape is INTENTIONALLY DIFFERENT from the standard
        ``{"error": {"code": ..., "message": ...}}`` envelope used by
        application API errors. The readiness probe is consumed by
        orchestration agents (ALB, ECS) that parse the documented
        health-check schema, not the application error envelope.
        """
        mock_engine = _make_mock_engine_with_connection_failure("simulated")
        with patch.object(db, "_engine", new=mock_engine):
            response = client.get(_READYZ_PATH)

        assert response.status_code == 503
        assert response.is_json
        body: dict[str, Any] = response.get_json()
        assert body.get("status") == "degraded"
        assert "service" in body
        assert "checks" in body
        db_check = body["checks"].get("database", {})
        assert db_check.get("status") == "error"
        assert "duration_ms" in db_check
        assert isinstance(db_check["duration_ms"], int | float)
        assert "error" in db_check
        assert isinstance(db_check["error"], str)
        assert db_check["error"]  # non-empty class name

    def test_readyz_correlation_id_propagated(self, client: Any) -> None:
        """A client-supplied X-Correlation-Id is echoed in the response.

        The correlation middleware honors the inbound header (when
        well-formed) and surfaces the same value on the response.
        Operators rely on this to follow a single request across
        client logs and server logs.
        """
        sentinel = "test-readyz-correlation-id-12345"
        response = client.get(
            _READYZ_PATH,
            headers={"X-Correlation-Id": sentinel},
        )
        # Both 200 (healthy) and 503 (degraded test envs) are acceptable;
        # the contract being tested is the correlation echo, not the
        # readiness state.
        assert response.status_code in {200, 503}
        echoed = response.headers.get("X-Correlation-Id")
        assert echoed == sentinel, (
            f"Expected the inbound X-Correlation-Id={sentinel!r} to be "
            f"echoed in the response, got {echoed!r}."
        )

    def test_readyz_via_post_returns_405(self, client: Any) -> None:
        """POST /readyz is not a valid readiness invocation.

        The blueprint declares only the GET method, so POSTing to the
        same path MUST yield 405 Method Not Allowed.
        """
        response = client.post(_READYZ_PATH)
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# TestHealthEndpointsAuthBypass - cross-check auth middleware
# ---------------------------------------------------------------------------


class TestHealthEndpointsAuthBypass:
    """Cross-check that the health paths are correctly listed in
    :data:`app.middleware.auth._PUBLIC_PATHS`.

    These tests are functionally redundant with the per-endpoint
    "no_authentication_required" tests but exist as a SEPARATE class
    so the auth-middleware contract is documented as its own concern.
    A regression that breaks the public-path allowlist would fail
    every test in this class with a recognizable signal (all paths
    suddenly returning 401), making the diagnosis fast.
    """

    def test_healthz_in_public_paths(self, client: Any) -> None:
        """Anonymous GET /healthz MUST NOT trigger the auth middleware.

        If the auth middleware drops /healthz from its public allowlist,
        ECS task health checks will start failing with 401 and the
        orchestrator will kill the worker. This test prevents that
        regression at PR time.
        """
        response = client.get(_HEALTHZ_PATH)
        assert response.status_code == 200
        assert response.status_code != 401

    def test_readyz_in_public_paths(self, client: Any) -> None:
        """Anonymous GET /readyz MUST NOT trigger the auth middleware.

        The ALB target group health check is anonymous (it sends no
        cookie, no Authorization header). If /readyz starts requiring
        auth, the ALB will mark every task as unhealthy and the service
        will go offline.
        """
        response = client.get(_READYZ_PATH)
        # 200 (healthy) or 503 (degraded) are the acceptable outcomes;
        # 401 specifically is a regression signal.
        assert response.status_code != 401
        assert response.status_code in {200, 503}

    def test_protected_path_requires_auth_for_comparison(self, client: Any) -> None:
        """Sanity check: a non-public /api path returns 401 anonymously.

        This test exists to confirm the auth middleware IS engaged. If
        the middleware were broken (e.g., not registered), the per-path
        bypass tests above would produce false positives -- every path
        would "succeed" not because of the bypass list but because no
        auth is enforced anywhere. This sanity check catches that.
        """
        # An anonymous client (no session cookie set) hitting a path
        # under /api/* MUST receive a 401.
        response = client.get(_PROTECTED_API_PATH)
        # 401 (preferred) or 403 (also acceptable) confirms auth is on.
        # Anything 2xx would indicate the auth middleware is broken or
        # not registered, in which case the bypass tests above are
        # vacuous.
        assert response.status_code in {401, 403}, (
            f"Expected 401/403 from anonymous request to a protected "
            f"path, got {response.status_code}; the auth middleware "
            "may not be registered, which would invalidate the "
            "public-path bypass tests above."
        )


# Pytest expects test classes to NOT have an __init__ method; the classes
# above intentionally define no constructor so they remain "test classes"
# under pytest's default collector. The ``pytest`` module-level reference
# below ties the explicit ``import pytest`` to a runtime use site, which
# (a) prevents linters from flagging the import as unused and (b) acts as
# a defensive sanity check that the pytest runtime is importable when this
# file is loaded as a regular Python module (e.g., by ad-hoc smoke tests).
assert pytest is not None
