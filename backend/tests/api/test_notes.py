"""API tests for the notes blueprint (F-002).

Covers ``POST /api/notes/generate`` happy path, RBAC matrix, validation,
AI timeout / failure handling, sanitization invariants, and the
provider-replaceability invariant.

Key invariants verified by this module:

- Only Contributor and Admin can generate notes; Viewer receives 403
  because note generation is part of the contributor's submit flow,
  not the sales rep's lead-claim flow.
- The endpoint returns 504 on AI timeout with ``error.code='ai_timeout'``
  (not 502 or 500); the AI failure is non-blocking and the frontend can
  retry or skip note generation.
- ``NoteGenerationRequest.extra='forbid'`` rejects unknown payload fields
  with 422.
- The handler sanitizes user-supplied ``relationship_context`` BEFORE
  forwarding it to the AI service; control characters and bidi unicode
  must NOT reach the LLM provider (AAP s 0.7.4 sanitization invariant).
- Only ``app.services.ai_orchestration`` imports ``anthropic`` or
  ``langchain_anthropic``; the API blueprint MUST NOT import these
  directly (provider-replaceability invariant per AAP s 0.7.7).
- AI failure does NOT block subsequent ``POST /api/connections`` calls;
  this test verifies the two-call flow is independent.

Mocking strategy
----------------

The notes blueprint imports its dependency at module load time::

    from app.services.ai_orchestration import (
        AIServiceUnavailableError,
        generate_outreach_notes,
    )

Per Python's ``from X import Y`` semantics, ``app.api.notes`` carries
its own local binding of ``generate_outreach_notes``. Patching
``app.services.ai_orchestration.generate_outreach_notes`` (as the
``mock_anthropic`` conftest fixture does) replaces the source-module
attribute but does NOT affect the local binding in the handler. To
intercept the call from the API layer, tests in this file patch
``app.api.notes.generate_outreach_notes`` directly via
``monkeypatch.setattr``.

For sanitization tests we want the real ``generate_outreach_notes``
to run (so the real ``sanitize_for_ai_prompt`` is invoked) but the
deepest LLM call replaced. The TestingConfig sets
``ANTHROPIC_API_KEY=""`` which would short-circuit the service with
a 503; we override the key per-test via
``monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", ...)`` and
patch ``app.services.ai_orchestration._call_chat_anthropic`` to
capture the sanitized text the worker thread would have sent to
Claude.
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib
import inspect
import pkgutil
from typing import Any
from unittest.mock import patch
import uuid

import pytest

from app.schemas.note_generation import (
    NoteGenerationRequest,
    NoteGenerationResponse,
)
from app.services import ai_orchestration as ai_orchestration_module
from app.services.ai_orchestration import (
    AIServiceUnavailableError,
)

# Imports overview
# ----------------
#
# Standard library:
#
# - ``importlib`` provides dynamic module-import primitives used in
#   the provider-replaceability tests to walk every ``app.api.*``
#   submodule.
# - ``inspect.getsource`` retrieves textual source for
#   forbidden-import scanning.
# - ``pkgutil.iter_modules`` walks every submodule under the
#   ``app.api`` package.
# - ``uuid`` parses the record IDs returned by
#   ``POST /api/connections`` in the non-blocking failure test.
# - ``datetime.UTC`` and ``datetime.datetime`` construct the
#   timezone-aware UTC ``generated_at`` field on
#   ``NoteGenerationResponse`` stub instances via
#   ``datetime.now(UTC)``; the project convention is timezone-aware
#   UTC, never naive.
# - ``typing.Any`` is used for permissive annotations on test helper
#   returns and JSON response bodies.
# - ``unittest.mock.patch`` provides ``patch.object`` used as a
#   context manager to mock the deep LLM call so the real
#   ``generate_outreach_notes`` runs with sanitization and the test
#   can inspect the call args.
#
# Note: this module deliberately does NOT import ``sys`` because it
# does NOT manipulate the module cache. The provider-replaceability
# tests inspect the source file via ``inspect.getsource`` on the
# already-imported module rather than evicting and re-importing,
# which would violate test isolation by changing what
# ``getattr(app.api, "notes")`` resolves to and silently breaking
# subsequent tests' ``monkeypatch.setattr`` calls.
#
# Third-party:
#
# - ``pytest`` provides the test runner, assertion rewriting, fixture
#   injection (auto-discovered from the parent ``conftest.py``),
#   class-level markers (``@pytest.mark.rbac``), and the built-in
#   ``monkeypatch`` fixture used to swap module attributes for the
#   duration of a single test.
#
# First-party (absolute paths only per ``flake8-tidy-imports``):
#
# - ``NoteGenerationRequest`` / ``NoteGenerationResponse`` are the
#   pydantic 2.x request / response schemas for the F-002 endpoint.
# - ``ai_orchestration_module`` is the AI orchestration MODULE
#   imported as a module so provider-replaceability tests can pass
#   it to ``inspect.getsource`` and walk its attributes.
# - ``AIServiceUnavailableError`` is raised inline in test mocks
#   with ``code='ai_timeout' / 'ai_error' / 'ai_not_configured' /
#   'ai_unavailable'`` and corresponding
#   ``status_code=504 / 502 / 503 / 504`` to verify the global
#   error handler maps each AI failure mode to the correct HTTP
#   status with the expected ``error.code`` envelope.

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_response(
    *,
    ai_notes: str = "Suggested outreach talking points.",
    model: str = "claude-sonnet-4-5",
) -> NoteGenerationResponse:
    """Construct a deterministic ``NoteGenerationResponse`` for tests.

    Centralising the construction here keeps the mock setup compact
    and ensures every test produces a tz-aware UTC ``generated_at``
    that satisfies the ``AwareDatetime`` constraint on the schema.

    Args:
        ai_notes: The AI-generated text. Defaults to a short
            sentence suitable for substring assertions.
        model: The Claude model identifier surfaced on the response.
            Defaults to the pinned-default model.

    Returns:
        A validated ``NoteGenerationResponse`` instance.
    """
    return NoteGenerationResponse(
        ai_notes=ai_notes,
        model=model,
        generated_at=datetime.now(UTC),
    )


def _build_connection_payload(
    *,
    full_name: str = "Jane Doe",
    linkedin_slug: str | None = None,
) -> dict[str, Any]:
    """Construct a minimal valid ``POST /api/connections`` payload.

    Used by the non-blocking failure test in
    ``TestNoteGenerationNonBlockingFailure``. The payload mirrors the
    minimal valid shape from ``test_connections.py`` so the test
    exercises a real connection-create path without needing to import
    the connections-test fixture.

    Args:
        full_name: The connection target's full name. Defaults to
            ``Jane Doe``.
        linkedin_slug: Optional LinkedIn slug; when ``None``, a
            unique random slug is generated so concurrent test runs
            do not collide on the unique partial index.

    Returns:
        A ``dict`` representing the JSON body for
        ``POST /api/connections``.
    """
    slug = linkedin_slug or f"jane-doe-{uuid.uuid4().hex[:8]}"
    return {
        "full_name": full_name,
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "company": "Acme Corp",
        "job_title": "VP of Operations",
        "relationship_context": "We worked together at a previous company.",
        "involvement": "Warm Intro",
    }


# Sample relationship context used across multiple test classes. Mirrors
# the user's verbatim example from AAP s 0.1.2 ("We went to college
# together, he's now VP of Ops at a Series B logistics startup") so
# the tests pin against the canonical reference example.
_SAMPLE_CONTEXT: str = (
    "We went to college together; she's now VP of Ops at a Series B logistics startup."
)


# ---------------------------------------------------------------------------
# TestNoteGenerationHappyPath
# ---------------------------------------------------------------------------


class TestNoteGenerationHappyPath:
    """Happy-path tests for ``POST /api/notes/generate``.

    Each test mocks ``generate_outreach_notes`` AT THE HANDLER MODULE
    boundary (``app.api.notes.generate_outreach_notes``) because the
    handler imports the function via ``from X import Y``, which
    creates a module-local binding. Patching the source module's
    attribute (``app.services.ai_orchestration.generate_outreach_notes``)
    would NOT affect the handler's local binding.
    """

    def test_contributor_generates_notes_returns_200(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Contributor POST returns 200 with the response body shape.

        Verifies the F-002 happy path: a Contributor calls the
        endpoint with valid ``relationship_context``, the service
        returns a ``NoteGenerationResponse``, and the handler
        serialises it to JSON.
        """
        captured: list[NoteGenerationRequest] = []

        def spy(payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            """Capture the request and return a deterministic response."""
            captured.append(payload)
            return _make_response(ai_notes="Suggested outreach talking points...")

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert "ai_notes" in body
        assert "model" in body
        assert "generated_at" in body
        assert body["ai_notes"] == "Suggested outreach talking points..."
        # The handler MUST forward the validated pydantic request to
        # the service; the spy captures it for inspection. The
        # captured value type is ``NoteGenerationRequest`` proving the
        # handler validated the payload before calling the service.
        assert len(captured) == 1
        assert isinstance(captured[0], NoteGenerationRequest)
        assert captured[0].relationship_context == _SAMPLE_CONTEXT

    def test_admin_can_generate_notes(
        self,
        admin_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Admin role is admitted by the ``@requires_role`` decorator.

        Per AAP s 6.2 RBAC permission matrix, both Contributor and
        Admin can generate notes.
        """

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response(ai_notes="Admin-generated outreach notes.")

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = admin_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ai_notes"] == "Admin-generated outreach notes."

    def test_response_shape_matches_schema(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Response body has exactly ``ai_notes``, ``model``, ``generated_at``.

        Verifies the response mirrors ``NoteGenerationResponse`` exactly
        (no leaked private fields, no missing public fields). This
        protects the SPA contract against accidental schema drift.
        """

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response()

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200
        body = response.get_json()
        # Exactly the three documented fields.
        assert set(body.keys()) == {"ai_notes", "model", "generated_at"}

    def test_response_ai_notes_is_string(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ai_notes`` is serialised as a JSON string."""

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response(ai_notes="A string of notes.")

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["ai_notes"], str)
        assert len(body["ai_notes"]) > 0

    def test_response_generated_at_is_iso8601(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``generated_at`` is serialised as an ISO-8601 datetime string.

        ``NoteGenerationResponse.generated_at`` is typed as
        ``pydantic.AwareDatetime``; ``model_dump(mode='json')`` in the
        handler converts it to ISO-8601 so the SPA can parse it via
        ``Date.parse(...)``. Verifies the format is parseable as a
        datetime.
        """

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response()

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200
        body = response.get_json()
        # ``datetime.fromisoformat`` accepts the standard ``YYYY-MM-DD
        # HH:MM:SS+00:00`` and the ``YYYY-MM-DDTHH:MM:SS+00:00`` shapes
        # that pydantic's ``model_dump(mode='json')`` emits.
        parsed = datetime.fromisoformat(body["generated_at"])
        # MUST be timezone-aware: aligns with the AwareDatetime contract.
        assert parsed.tzinfo is not None

    def test_response_model_is_string(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``model`` is a non-empty string identifying the Claude model."""

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response(model="claude-sonnet-4-5")

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["model"], str)
        assert body["model"] == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# TestNoteGenerationRBAC
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestNoteGenerationRBAC:
    """RBAC matrix tests for ``POST /api/notes/generate``.

    Per AAP s 6.2 RBAC permission matrix, the endpoint admits only
    Contributor and Admin. Viewer (Sales Rep) is rejected because the
    note-generation flow is part of the contributor's submit path,
    not the sales rep's lead-claim path. Anonymous callers are
    rejected by the auth middleware before RBAC even runs.
    """

    def test_viewer_forbidden(
        self,
        viewer_client: Any,
    ) -> None:
        """Viewer (Sales Rep) is rejected with 403 ``forbidden``.

        The ``@requires_role(CONTRIBUTOR, ADMIN)`` decorator on the
        view function rejects the Viewer role BEFORE the handler
        body runs, so no AI service call is ever attempted (the test
        does not need to mock the service).
        """
        response = viewer_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 403
        body = response.get_json()
        # Per AAP s 0.4.3 the error envelope is uniform across all
        # endpoints; the SPA dispatches typed handlers on
        # ``error.code``.
        assert body["error"]["code"] == "forbidden"

    def test_contributor_allowed(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Contributor role is admitted (200)."""

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response()

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200

    def test_admin_allowed(
        self,
        admin_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Admin role is admitted (200)."""

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response()

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)
        response = admin_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 200

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated callers are rejected by the auth middleware (401).

        The auth middleware rejects requests without a valid session
        cookie BEFORE the @requires_role decorator runs. The expected
        response is 401 ``unauthorized``, not 403 ``forbidden`` (the
        latter is reserved for an authenticated user with the wrong
        role).
        """
        response = client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestNoteGenerationValidation
# ---------------------------------------------------------------------------


class TestNoteGenerationValidation:
    """Schema validation tests for ``POST /api/notes/generate``.

    Validation runs in the handler before any service call is made,
    so these tests do NOT need to mock the AI service: the rejected
    payload never reaches it.

    Verifies the three field-level defenses on
    ``NoteGenerationRequest``:

    * ``min_length=1`` rejects empty / whitespace-only inputs after
      ``str_strip_whitespace=True`` strips them.
    * ``max_length=4000`` rejects oversized inputs.
    * ``extra='forbid'`` rejects any client-supplied override (e.g.,
      ``model``, ``temperature``) that would let a malicious client
      steer the Claude call.
    """

    def test_missing_relationship_context_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """POST ``{}`` is rejected with 422 ``validation_failed``.

        ``relationship_context`` is a required field; pydantic's
        ``missing`` error class produces a 422 with field-level
        detail referencing the missing field name.
        """
        response = contributor_client.post("/api/notes/generate", json={})
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"
        # The field-level detail MUST identify the missing field so
        # the SPA can highlight it. Search across all fields rather
        # than assert a specific index because pydantic's error order
        # is not guaranteed across versions.
        fields = body["error"].get("fields", [])
        assert any("relationship_context" in str(field.get("loc", [])) for field in fields), (
            f"Expected 'relationship_context' in fields, got: {fields}"
        )

    def test_empty_relationship_context_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """POST with ``relationship_context=""`` is rejected with 422.

        The schema enforces ``min_length=1`` so an empty string
        triggers a ``string_too_short`` validation error. This
        defends against the no-op AI round-trip case.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": ""},
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_relationship_context_max_4000_chars_boundary(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exactly 4000 chars is accepted (boundary case OK).

        Per the schema, ``max_length=4000`` is inclusive: a 4000-char
        payload is admitted. The AI service is mocked so the test
        does not depend on any real network call.
        """

        def spy(_payload: NoteGenerationRequest, **_kwargs: Any) -> NoteGenerationResponse:
            return _make_response()

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", spy)

        # Use a non-whitespace character so the schema's
        # ``str_strip_whitespace=True`` does not reduce the length.
        boundary_input = "x" * 4000
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": boundary_input},
        )
        assert response.status_code == 200

    def test_relationship_context_over_4000_chars_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """4001 chars is rejected with 422 ``validation_failed``.

        Verifies the schema's ``max_length=4000`` exclusive upper
        bound. The AI service is NOT mocked because validation
        rejects the payload before any service call.
        """
        oversize_input = "x" * 4001
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": oversize_input},
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_extra_field_rejected_by_extra_forbid(
        self,
        contributor_client: Any,
    ) -> None:
        """Unknown payload fields are rejected by ``extra='forbid'``.

        Per AAP s 0.7.4, the model selection happens server-side
        from Flask config; client-supplied ``model``,
        ``temperature``, ``max_tokens``, etc. would let a malicious
        caller steer the Claude call. The schema's
        ``extra='forbid'`` config rejects any unknown field with
        422.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Valid context.",
                "model": "gpt-4",  # client-supplied model override -- forbidden
            },
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_non_string_relationship_context_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Non-string ``relationship_context`` is rejected with 422.

        Verifies pydantic's runtime type validation: a JSON number
        is not coerced to a string at the schema layer.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": 12345},
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_null_relationship_context_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Explicit ``null`` ``relationship_context`` is rejected with 422.

        Verifies pydantic does not silently coerce ``None`` to the
        empty string for a required field.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": None},
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_whitespace_only_relationship_context_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Whitespace-only ``relationship_context`` is rejected with 422.

        The schema's ``str_strip_whitespace=True`` runs BEFORE the
        ``min_length=1`` check, so a payload of ``"   "`` becomes
        ``""`` post-strip and is rejected with a clear field-scoped
        error rather than passing through to the AI service.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": "   "},
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_malformed_json_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """A non-JSON request body is rejected with a 422 envelope.

        The handler uses ``request.get_json(silent=True)`` so a
        malformed body returns ``None``; the explicit branch raises
        ``ValidationFailedError`` with field-scoped ``invalid_json``
        detail rather than letting werkzeug produce its default
        HTML error page.
        """
        response = contributor_client.post(
            "/api/notes/generate",
            data="not-valid-json-at-all",
            content_type="application/json",
        )
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_validation_error_does_not_echo_user_input(
        self,
        contributor_client: Any,
    ) -> None:
        """Validation error envelope does NOT echo the user's text.

        Per AAP s 0.7.4 PII invariant, the error response MUST NOT
        leak the user-supplied ``relationship_context`` in the
        ``error.fields`` payload. The error envelope's ``input`` /
        ``ctx`` fields from pydantic are dropped by the handler (see
        ``backend/app/api/notes.py``).
        """
        # A unique marker that would NEVER appear in any sanitized
        # response or generic error message; if it surfaces in the
        # body we have a leak.
        secret_marker = "TOPSECRET_PII_MARKER_42_xyz_unique_test_token"
        response = contributor_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": secret_marker,
                "extra_field_to_force_422": "value",
            },
        )
        assert response.status_code == 422
        body_text = response.get_data(as_text=True)
        assert secret_marker not in body_text, (
            "Validation error response leaked the user-supplied "
            "relationship_context; AAP s 0.7.4 PII invariant violated."
        )


# ---------------------------------------------------------------------------
# TestNoteGenerationTimeout
# ---------------------------------------------------------------------------


class TestNoteGenerationTimeout:
    """AI failure handling tests for ``POST /api/notes/generate``.

    Per AAP s 0.4.4, when the AI provider call fails, the handler
    returns a non-blocking error envelope so the SPA can render an
    "AI unavailable; you can still submit" affordance. The four
    documented failure codes map to four distinct HTTP statuses:

    * ``ai_timeout``        -> 504 (provider did not respond in time)
    * ``ai_error``          -> 502 (provider returned an error)
    * ``ai_not_configured`` -> 503 (API key is empty)
    * ``ai_unavailable``    -> 504 (provider is generally unavailable)

    The global ``AppError`` handler reads ``status_code`` and
    ``error_code`` off the raised ``AIServiceUnavailableError`` and
    builds the canonical envelope; tests do not need to register a
    per-error handler.
    """

    def test_ai_timeout_returns_504(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AIServiceUnavailableError(code='ai_timeout', status=504)`` -> 504.

        The handler does NOT catch ``AIServiceUnavailableError``
        locally; the global ``AppError`` handler reads the per-instance
        ``status_code=504`` and ``error_code='ai_timeout'`` and builds
        the response. This preserves the thin-handler convention
        from AAP s 0.5.3.
        """

        def raise_timeout(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            raise AIServiceUnavailableError(
                message="AI provider timed out.",
                code="ai_timeout",
                status_code=504,
            )

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_timeout)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 504
        body = response.get_json()
        assert body["error"]["code"] == "ai_timeout"
        # The user-facing message is non-empty so the SPA can render
        # it directly in a toast without hard-coding fallback text.
        assert isinstance(body["error"]["message"], str)
        assert len(body["error"]["message"]) > 0

    def test_ai_provider_error_returns_502(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AIServiceUnavailableError(code='ai_error', status=502)`` -> 502.

        ``ai_error`` represents a non-timeout failure from the
        provider (e.g., 5xx response, unrecognized SDK exception).
        The handler returns 502 so the SPA can distinguish it from
        a timeout.
        """

        def raise_error(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            raise AIServiceUnavailableError(
                message="AI provider returned an error.",
                code="ai_error",
                status_code=502,
            )

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_error)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 502
        body = response.get_json()
        assert body["error"]["code"] == "ai_error"

    def test_ai_not_configured_returns_503(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AIServiceUnavailableError(code='ai_not_configured', status=503)`` -> 503.

        ``ai_not_configured`` means ``ANTHROPIC_API_KEY`` is empty
        (a configuration error, not a runtime error). The 503
        status lets the SPA render an "AI service not configured"
        toast distinct from the transient ``ai_timeout`` /
        ``ai_error`` cases.
        """

        def raise_not_configured(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            raise AIServiceUnavailableError(
                message="Anthropic API key is not configured.",
                code="ai_not_configured",
                status_code=503,
            )

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_not_configured)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 503
        body = response.get_json()
        assert body["error"]["code"] == "ai_not_configured"

    def test_ai_unavailable_returns_504(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AIServiceUnavailableError(code='ai_unavailable', status=504)`` -> 504.

        ``ai_unavailable`` is the catch-all code used when the
        watchdog cancels a stuck request. It maps to the same 504
        status as ``ai_timeout`` because both indicate provider
        unavailability; the SPA branches on the ``error.code`` value
        to render the appropriate retry affordance.
        """

        def raise_unavailable(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            raise AIServiceUnavailableError(
                message="AI provider is currently unavailable.",
                code="ai_unavailable",
                status_code=504,
            )

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_unavailable)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 504
        body = response.get_json()
        assert body["error"]["code"] == "ai_unavailable"

    def test_ai_unexpected_error_returns_500_with_generic_message(
        self,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unhandled exception is mapped to a generic 500 envelope.

        Per AAP s 0.7.4, the global error handler:

        * Logs the FULL exception including any sensitive content
          for engineers (via structured logging with correlation_id).
        * Returns a GENERIC user-facing message; the actual exception
          class, its ``str()`` repr, and any internal details are NOT
          leaked to the client.

        This test verifies that a hostile RuntimeError carrying a
        secret-shaped string in its message does not bleed into the
        user-visible response body.
        """
        secret_in_exception = "credentials=PROJECT_SECRET_VALUE_xyz123"

        def raise_unexpected(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            # A bare ``RuntimeError`` is NOT an ``AppError`` subclass;
            # it falls through to the catch-all ``Exception`` handler
            # in ``app.middleware.error_handlers``.
            raise RuntimeError(f"internal AI bug with {secret_in_exception}")

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_unexpected)

        response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert response.status_code == 500
        body = response.get_json()
        # Generic ``internal_error`` code, NOT a per-exception code.
        assert body["error"]["code"] == "internal_error"
        # The secret-shaped string MUST NOT appear in the response
        # body anywhere (message, fields, correlation_id).
        body_text = response.get_data(as_text=True)
        assert "credentials" not in body_text, (
            "Internal-error response leaked an exception-message "
            "secret; AAP s 0.7.4 invariant violated."
        )
        assert "PROJECT_SECRET_VALUE" not in body_text
        assert "xyz123" not in body_text


# ---------------------------------------------------------------------------
# TestNoteGenerationSanitization
# ---------------------------------------------------------------------------


class TestNoteGenerationSanitization:
    """Sanitization invariant tests for ``POST /api/notes/generate``.

    Per AAP s 0.7.4, "User-supplied relationship context sanitized
    server-side before AI prompt." These tests verify that the
    sanitization pipeline (``app.utils.sanitization.sanitize_for_ai_prompt``)
    runs BEFORE the LLM call, so control characters and bidi unicode
    never reach the Anthropic SDK.

    Mocking strategy
    ----------------

    Unlike the happy-path / RBAC / timeout tests (which mock
    ``app.api.notes.generate_outreach_notes`` to bypass the entire
    service), these tests need the REAL ``generate_outreach_notes``
    to run so the real ``sanitize_for_ai_prompt`` is invoked. We mock
    the deepest layer (``_call_chat_anthropic``) instead and capture
    its kwargs to verify the ``sanitized`` argument is clean.

    The TestingConfig sets ``ANTHROPIC_API_KEY=""`` which would
    short-circuit the service with ``AIServiceUnavailableError(
    code='ai_not_configured', status=503)`` BEFORE reaching
    ``_call_chat_anthropic``. Each test in this class overrides the
    config key via ``monkeypatch.setitem(app.config, ...)`` so the
    real service path executes end-to-end against the mocked LLM
    call.
    """

    def test_control_characters_stripped_from_relationship_context(
        self,
        app: Any,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ASCII control chars are stripped before reaching the LLM.

        Submits a payload containing ``\\x00`` (NULL), ``\\x01`` (SOH),
        ``\\x07`` (BEL), and ``\\x1b`` (ESC) characters; verifies the
        sanitizer (invoked by the real service) strips them before
        the deeper ``_call_chat_anthropic`` mock receives the value.
        """
        # Override the empty TestingConfig API key so the service
        # does not short-circuit with 503 before reaching
        # _call_chat_anthropic.
        monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "test-fake-key-not-used")

        with patch.object(
            ai_orchestration_module,
            "_call_chat_anthropic",
            return_value="Generated text",
        ) as mock_llm:
            response = contributor_client.post(
                "/api/notes/generate",
                json={
                    "relationship_context": "Hello\x00World\x01alpha\x07beta\x1bend",
                },
            )

        assert response.status_code == 200
        # The mock MUST have been called once -- the real service
        # path executed end-to-end against the real sanitizer.
        assert mock_llm.call_count == 1
        # ``_call_chat_anthropic`` is called with kwargs by
        # ``_invoke_with_timeout``; the ``sanitized`` kwarg carries
        # the post-sanitization text.
        captured_sanitized: Any = mock_llm.call_args.kwargs.get("sanitized")
        assert captured_sanitized is not None, (
            f"_call_chat_anthropic was not called with 'sanitized' kwarg: {mock_llm.call_args}"
        )
        # Every control char MUST be absent from the sanitized text.
        for forbidden in ("\x00", "\x01", "\x07", "\x1b"):
            assert forbidden not in captured_sanitized, (
                f"Control character {forbidden!r} reached the LLM call "
                f"(captured: {captured_sanitized!r}). Sanitization "
                f"invariant violated."
            )
        # The legitimate text content survives the sanitization.
        assert "Hello" in captured_sanitized
        assert "World" in captured_sanitized
        assert "alpha" in captured_sanitized
        assert "beta" in captured_sanitized

    def test_bidi_unicode_stripped(
        self,
        app: Any,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bidi / zero-width Unicode chars are stripped before the LLM.

        Submits a payload containing:

        * ``U+202E`` (RIGHT-TO-LEFT OVERRIDE) -- bidi attack vector
        * ``U+200B`` (ZERO WIDTH SPACE)        -- invisible separator
        * ``U+200E`` (LEFT-TO-RIGHT MARK)      -- bidi formatting

        Verifies the sanitizer's ``_strip_invisible_unicode`` step
        removes them before the LLM call.
        """
        monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "test-fake-key-not-used")

        with patch.object(
            ai_orchestration_module,
            "_call_chat_anthropic",
            return_value="Generated text",
        ) as mock_llm:
            response = contributor_client.post(
                "/api/notes/generate",
                json={
                    # Embed bidi marks INSIDE the visible text so the
                    # whole string still passes the schema's
                    # ``str_strip_whitespace=True`` (which only
                    # strips outer whitespace).
                    "relationship_context": ("Hello\u202eattack\u200binvisible\u200esubmit"),
                },
            )

        assert response.status_code == 200
        assert mock_llm.call_count == 1
        captured_sanitized: Any = mock_llm.call_args.kwargs.get("sanitized")
        assert captured_sanitized is not None
        for forbidden in ("\u202e", "\u200b", "\u200e"):
            assert forbidden not in captured_sanitized, (
                f"Bidi/zero-width char {forbidden!r} reached the LLM "
                f"call (captured: {captured_sanitized!r}). Sanitization "
                f"invariant violated."
            )
        # The legitimate visible text content is preserved.
        assert "Hello" in captured_sanitized
        assert "attack" in captured_sanitized
        assert "invisible" in captured_sanitized
        assert "submit" in captured_sanitized

    def test_normal_text_passes_through_unchanged(
        self,
        app: Any,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plain ASCII text without control chars is forwarded as-is.

        Submits the canonical user-prompt example from AAP s 0.1.2
        and verifies the LLM call sees substantively the same text.
        Sanitization is non-destructive on clean inputs: it only
        removes the byte-level offenders documented in
        ``app/utils/sanitization.py``.
        """
        monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "test-fake-key-not-used")

        clean_text = _SAMPLE_CONTEXT  # No control chars, no bidi marks.

        with patch.object(
            ai_orchestration_module,
            "_call_chat_anthropic",
            return_value="Generated text",
        ) as mock_llm:
            response = contributor_client.post(
                "/api/notes/generate",
                json={"relationship_context": clean_text},
            )

        assert response.status_code == 200
        assert mock_llm.call_count == 1
        captured_sanitized: Any = mock_llm.call_args.kwargs.get("sanitized")
        assert captured_sanitized is not None
        # On clean ASCII input the sanitizer is effectively the
        # identity function (modulo NFC normalization which is also
        # a no-op for ASCII). The clean text passes through verbatim.
        assert captured_sanitized == clean_text

    def test_sanitizer_runs_before_llm_call(
        self,
        app: Any,
        contributor_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanitization is invoked at least once before the LLM call.

        Patches both the sanitizer (to confirm it is called) and the
        LLM client (to short-circuit the real network path). The
        order of calls is enforced by the production code in
        ``generate_outreach_notes``: sanitize first, then submit to
        the thread pool, then ``_call_chat_anthropic``.
        """
        monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", "test-fake-key-not-used")

        with (
            patch.object(
                ai_orchestration_module,
                "sanitize_for_ai_prompt",
                wraps=ai_orchestration_module.sanitize_for_ai_prompt,
            ) as mock_sanitize,
            patch.object(
                ai_orchestration_module,
                "_call_chat_anthropic",
                return_value="Generated text",
            ) as mock_llm,
        ):
            response = contributor_client.post(
                "/api/notes/generate",
                json={"relationship_context": _SAMPLE_CONTEXT},
            )

        assert response.status_code == 200
        # The sanitizer MUST be called at least once with the user's
        # text. ``wraps=...`` lets the real sanitizer run so the
        # downstream service code receives a real sanitized string.
        assert mock_sanitize.call_count >= 1
        # And the LLM call MUST follow the sanitizer invocation.
        assert mock_llm.call_count == 1


# ---------------------------------------------------------------------------
# TestNoteGenerationProviderReplaceability
# ---------------------------------------------------------------------------


class TestNoteGenerationProviderReplaceability:
    """Provider-replaceability invariant tests (AAP s 0.7.7).

    Per AAP s 0.7.7: "No direct Anthropic SDK usage outside
    ``services/ai_orchestration.py``. Provider-replaceability
    invariant." This invariant lets a future provider swap (OpenAI,
    Gemini, Azure OpenAI, etc.) be a single-file change without
    touching any feature handler.

    These tests assert the invariant by inspecting the source code
    of every ``app.api.*`` submodule and verifying it contains zero
    direct imports of ``anthropic`` or ``langchain_anthropic``. The
    only allowed access path is via
    ``app.services.ai_orchestration.generate_outreach_notes``.
    """

    # The set of import statement substrings we forbid in any
    # ``app.api.*`` source file. We check both the bare ``import X``
    # form AND the ``from X import ...`` form to catch every shape
    # of direct dependency. ``langchain_core`` is also forbidden
    # because it is the message-construction layer that the AI
    # orchestration uses internally; feature handlers should not see
    # those types either.
    _FORBIDDEN_IMPORT_SUBSTRINGS: tuple[str, ...] = (
        "import anthropic",
        "from anthropic",
        "import langchain_anthropic",
        "from langchain_anthropic",
        "import langchain_core",
        "from langchain_core",
    )

    def test_blueprint_does_not_import_anthropic_directly(self) -> None:
        """``app.api.notes`` source contains no direct AI SDK imports.

        Approach: import the already-cached module via
        ``importlib.import_module`` (which returns the cached
        instance from ``sys.modules`` for an already-imported
        module) and pass it to ``inspect.getsource``. The source is
        read from disk via the module's ``__file__`` attribute, so
        a re-import is unnecessary; the on-disk file is the
        authoritative source of truth for what the module imports.

        Test-isolation safety
        ---------------------

        This test deliberately avoids the
        ``del sys.modules["app.api.notes"]`` -> re-import pattern
        that would force a fresh module instance. That pattern
        would leak across tests: pytest's ``monkeypatch.setattr``
        resolves a string target like
        ``"app.api.notes.generate_outreach_notes"`` via ATTRIBUTE
        LOOKUP through the parent package (``getattr(app.api,
        "notes")`` rather than ``sys.modules["app.api.notes"]``).
        ``importlib.import_module`` updates BOTH the cache AND the
        parent-package attribute when re-importing; restoring only
        the cache leaves the parent attribute pointing at the new
        module copy. Subsequent tests that monkeypatch
        ``generate_outreach_notes`` would then silently patch the
        re-imported copy while the Flask app's already-registered
        blueprint view function continues to dereference the
        original copy, and the patch would have no effect.
        Skipping the eviction entirely keeps the test idempotent
        across orderings.
        """
        notes_module = importlib.import_module("app.api.notes")

        # Verify the blueprint object IS exported by the module so
        # the test runner can invoke the endpoint via the Flask test
        # client. The presence of ``notes_bp`` is part of the public
        # contract.
        assert hasattr(notes_module, "notes_bp"), (
            "app.api.notes is missing the notes_bp Blueprint export"
        )

        # ``inspect.getsource`` reads from disk via the module's
        # ``__file__`` attribute; the result is the verbatim text of
        # the on-disk source file, which is the canonical answer to
        # "does this module have a forbidden import statement?".
        source = inspect.getsource(notes_module)

        for forbidden in self._FORBIDDEN_IMPORT_SUBSTRINGS:
            assert forbidden not in source, (
                f"app.api.notes contains forbidden import substring "
                f"'{forbidden}'. Per AAP s 0.7.7, only "
                f"app.services.ai_orchestration may import anthropic / "
                f"langchain_anthropic / langchain_core. All AI calls "
                f"from feature handlers MUST go through "
                f"app.services.ai_orchestration.generate_outreach_notes."
            )

    def test_only_ai_orchestration_imports_anthropic_packages(self) -> None:
        """No ``app.api.*`` submodule imports an AI SDK package directly.

        Walks every submodule of ``app.api`` (auth, connections,
        notes, tags, admin, health, plus any future siblings) and
        asserts each source file contains zero forbidden import
        substrings. This is the strongest form of the invariant: a
        future feature handler that accidentally imports the SDK
        will fail this test without any other coordination.
        """
        # Import ``app.api`` package so its ``__path__`` is available
        # to ``pkgutil.iter_modules``. The package is already loaded
        # by ``create_app``, but importing it here defends against
        # test-runner orderings that may not have triggered the
        # eager load yet.
        api_pkg = importlib.import_module("app.api")
        offending: list[str] = []
        for _finder, name, _ispkg in pkgutil.iter_modules(api_pkg.__path__, prefix="app.api."):
            module = importlib.import_module(name)
            try:
                source = inspect.getsource(module)
            except (OSError, TypeError):
                # OSError: source file not on disk (zip-imported or
                # frozen). TypeError: module is built-in or has no
                # __file__. Skip silently because the invariant only
                # applies to first-party Python source files.
                continue
            for forbidden in self._FORBIDDEN_IMPORT_SUBSTRINGS:
                if forbidden in source:
                    offending.append(f"{name}: '{forbidden}'")
        assert not offending, (
            "Forbidden AI SDK imports found in app.api.* modules. Per "
            "AAP s 0.7.7, only app.services.ai_orchestration may import "
            "anthropic / langchain_anthropic / langchain_core. "
            "Offending modules:\n" + "\n".join(offending)
        )


# ---------------------------------------------------------------------------
# TestNoteGenerationNonBlockingFailure
# ---------------------------------------------------------------------------


class TestNoteGenerationNonBlockingFailure:
    """AI failure does NOT block subsequent connection submission.

    Per AAP s 0.4.4 Surface 2: "AI failure does NOT roll back the
    form submit. The frontend is responsible for the two-call flow:
    optional ``POST /api/notes/generate`` first, then
    ``POST /api/connections`` with the (optionally edited)
    ``ai_notes`` payload."

    The two endpoints MUST be decoupled:

    * The notes endpoint does NOT touch the database.
    * The notes endpoint does NOT mutate session state.
    * A failed notes call leaves no stale transaction or rolled-back
      side effect that would fail a subsequent connections call.
    """

    def test_ai_failure_does_not_block_subsequent_connection_submit(
        self,
        contributor_client: Any,
        organization: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed notes call followed by successful connection create.

        Workflow:

        1. Mock ``generate_outreach_notes`` to raise
           ``AIServiceUnavailableError(code='ai_timeout', status=504)``.
        2. ``POST /api/notes/generate`` returns 504.
        3. Without resetting state, ``POST /api/connections`` with a
           valid record payload returns 201.
        4. The new connection record's id is a parseable UUID.

        The ``organization`` fixture seeds the canonical default
        organization the contributor user belongs to, so the
        connection-create path has a real org row to scope against.
        """

        # Step 1: install the AI failure mock.
        def raise_timeout(*_args: Any, **_kwargs: Any) -> NoteGenerationResponse:
            raise AIServiceUnavailableError(
                message="AI provider timed out.",
                code="ai_timeout",
                status_code=504,
            )

        monkeypatch.setattr("app.api.notes.generate_outreach_notes", raise_timeout)

        # Step 2: POST /api/notes/generate -> 504.
        notes_response = contributor_client.post(
            "/api/notes/generate",
            json={"relationship_context": _SAMPLE_CONTEXT},
        )
        assert notes_response.status_code == 504
        assert notes_response.get_json()["error"]["code"] == "ai_timeout"

        # Step 3: without resetting state, POST /api/connections.
        # The connection-create payload includes a placeholder
        # ``relationship_context`` that the user typed manually
        # because the AI call failed. The connection-create endpoint
        # does NOT call the AI service so the failed mock above does
        # not affect this path.
        connection_payload = _build_connection_payload()
        connection_response = contributor_client.post("/api/connections", json=connection_payload)
        assert connection_response.status_code == 201, (
            f"Connection create failed after a failed AI call. The two "
            f"endpoints MUST be independent. Response body: "
            f"{connection_response.get_data(as_text=True)}"
        )

        # Step 4: verify the new record's id is a parseable UUID.
        body = connection_response.get_json()
        assert "id" in body, f"Connection response missing 'id': {body}"
        # ``uuid.UUID`` raises ``ValueError`` on a non-UUID string;
        # the cast is the simplest way to assert the format.
        record_id = uuid.UUID(body["id"])
        assert record_id is not None
        # Defensive: the persisted record carries the contributor's
        # owner attribution server-side; the ``owner_user_id`` MUST
        # be present per AAP s 0.7.4.
        assert "owner_user_id" in body
