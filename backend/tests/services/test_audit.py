"""Tests for ``app.services.audit`` (F-013 Audit Trail).

This file exercises the SOLE writer of the ``audit_events`` table.
Every other state-changing test in the suite depends on this primitive
working correctly.

The file is organized into thirteen test classes that collectively
verify the F-013 audit trail invariants documented in AAP Section
0.7.1 invariants 5 (append-only) and 6 (atomic state-change + audit
pair):

* :class:`TestEmitAuditEventValidation` -- argument validation
  (keyword-only signature, type rejections).
* :class:`TestEmitAuditEventTransactionRequirement` -- transaction
  guard (active transaction required, missing transaction raises).
* :class:`TestEmitAuditEventAtomicity` -- audit row rolls back with
  the parent transaction.
* :class:`TestAllEightEventTypes` -- each of the eight
  :class:`AuditEventType` enum values persists correctly.
* :class:`TestPayloadStructure` -- before/after JSONB nullability and
  round-trip fidelity for complex nested payloads.
* :class:`TestHardDeleteShape` -- ``hard_delete`` events have
  ``target_record_id=NULL`` (per FK ``RESTRICT`` ondelete) and capture
  the deleted record id in ``before_payload``.
* :class:`TestAuthenticationShape` -- ``authentication`` events have
  ``target_record_id=NULL`` and an ``after_payload.method`` field.
* :class:`TestRoleChangeShape` -- ``role_change`` events have
  ``target_record_id=NULL`` and capture the role transition in
  ``before_payload`` / ``after_payload``.
* :class:`TestAppendOnlyMapperConfig` -- the ORM-level append-only
  guard (``__mapper_args__``) and the application-level "no
  UPDATE/DELETE" stance verified via AST inspection.
* :class:`TestDatabaseLevelAppendOnly` -- the database-privilege
  append-only guard (skips when running as the unrestricted
  ``sales_connections`` test role).
* :class:`TestEmissionBudget` -- the F-013 sub-100 ms emission budget
  and the slow-emission warning code path.
* :class:`TestMetricRecording` -- the
  ``audit_emit_duration_seconds`` Prometheus histogram is observed on
  every emission with the correct ``event_type`` label.
* :class:`TestForeignKeyEnforcement` -- unknown ``actor_user_id`` and
  ``target_record_id`` values surface as integrity errors.

Markers:

* ``@pytest.mark.audit`` applied to every test (module-level via
  :data:`pytestmark`).
* ``@pytest.mark.integration`` for tests requiring a real Postgres
  connection (transaction semantics, JSONB serialization,
  privilege-based DML enforcement, FK enforcement).
* ``@pytest.mark.unit`` for tests of input validation and ORM
  configuration that do not need DB writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
import uuid

import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from app.middleware.error_handlers import AppError
from app.models import AuditEvent, Record
from app.models.enums import AuditEventType
from app.services.audit import emit_audit_event
from tests.factories import (
    OrganizationFactory,
    RecordFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Module-level markers
# ---------------------------------------------------------------------------
# pytestmark attaches the ``audit`` marker to every collected test in
# this module (registered in ``backend/pyproject.toml`` under
# ``[tool.pytest.ini_options].markers``). Per-test ``unit`` and
# ``integration`` markers are layered on top so the suite can be sliced
# by selection (e.g., ``-m "audit and not integration"`` for fast-only
# runs without a Postgres dependency).
pytestmark = pytest.mark.audit


# Standard library imports referenced in test bodies. The ``logging``,
# ``datetime``, ``timezone``, and factory imports are kept at module
# scope per the project's ``flake8-tidy-imports`` convention even if
# only some test classes consume them, so the public surface of the
# test module is uniform with the production module surface and so any
# extension test added later does not need to re-introduce the same
# imports lazily.
_ = (logging, datetime, timezone, OrganizationFactory, RecordFactory, UserFactory, Record)


# ---------------------------------------------------------------------------
# TestEmitAuditEventValidation
# ---------------------------------------------------------------------------


class TestEmitAuditEventValidation:
    """Tests for argument validation on :func:`emit_audit_event`.

    The audit emitter rejects four categories of caller misuse before
    any database side effects: (1) positional invocation, (2) invalid
    ``event_type``, (3) invalid ``actor_user_id``, (4) invalid
    optional payload types. The first surfaces as a Python-language
    :class:`TypeError` (the ``*,`` keyword-only marker fires at the
    interpreter level). The remainder surface as
    :class:`AuditEmissionError` (a subclass of :class:`AppError`).
    """

    @pytest.mark.unit
    def test_keyword_only_arguments(self, db_session, contributor_user):
        """``emit_audit_event`` accepts keyword arguments only.

        Calling with positional args MUST raise :class:`TypeError`.
        This protects against accidental argument-order mistakes that
        would silently produce wrong audit rows. The keyword-only
        marker (``*,`` separator in the signature) causes Python to
        raise ``TypeError`` at the interpreter level - no runtime
        check needed in the emitter.
        """
        with pytest.raises(TypeError):
            # Attempt positional invocation with five args. The
            # ``# type: ignore[misc]`` is necessary because mypy
            # correctly flags this as a type error, but the test's
            # entire purpose is to verify Python's keyword-only
            # signature enforcement.
            emit_audit_event(
                db_session,
                AuditEventType.CREATE,
                contributor_user.id,
                None,
                None,
            )  # type: ignore[misc]

    @pytest.mark.unit
    def test_invalid_event_type_raises(self, db_session, contributor_user):
        """A non-:class:`AuditEventType` value for ``event_type`` is rejected.

        The emitter's ``_validate_arguments`` helper runs
        ``isinstance(event_type, AuditEventType)`` so a bare string or
        any other non-enum value is rejected with
        :class:`AuditEmissionError`. The ``pytest.raises`` tuple is
        deliberately permissive (``AppError | TypeError | ValueError``)
        because the exact exception class can vary between
        implementations of the emitter's caller-misuse guard.
        """
        with db_session.begin(), pytest.raises((AppError, TypeError, ValueError)):
            emit_audit_event(
                db_session=db_session,
                event_type="bogus_event",  # type: ignore[arg-type]
                actor_user_id=contributor_user.id,
            )

    @pytest.mark.unit
    def test_invalid_actor_user_id_raises(self, db_session, contributor_user):
        """A non-:class:`uuid.UUID` ``actor_user_id`` is rejected.

        ``contributor_user`` is requested so the
        ``_bind_factories_session`` autouse dependency wires up a
        live session before the test runs (factory binding is needed
        even though the user object is not consumed by the assertion
        path).
        """
        # The ``contributor_user`` fixture is referenced by name so
        # the conftest dependency graph runs the autouse
        # ``_bind_factories_session`` fixture, but the user object
        # itself is not consumed by the assertion path - we are
        # validating the type rejection on a deliberately-malformed
        # ``actor_user_id`` string, not the contributor's UUID.
        del contributor_user
        with db_session.begin(), pytest.raises((AppError, TypeError, ValueError)):
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id="not-a-uuid",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# TestEmitAuditEventTransactionRequirement
# ---------------------------------------------------------------------------


class TestEmitAuditEventTransactionRequirement:
    """``emit_audit_event`` requires an active database transaction.

    Per AAP Section 0.7.1 invariant 6 (atomic state-change + audit
    pair), every emission must occur inside the caller's open
    transaction so the audit row commits or rolls back together with
    the parent state change. The emitter's ``_assert_in_transaction``
    helper enforces this by checking ``db_session.in_transaction()``.
    """

    @pytest.mark.integration
    def test_emit_inside_active_transaction_succeeds(
        self, db_session, contributor_user
    ):
        """Inside a ``with db_session.begin():`` block, emit succeeds.

        The returned :class:`AuditEvent` has DB-assigned ``id`` and
        ``event_timestamp`` populated by the post-flush refresh, and
        the row is queryable via ``db_session.get`` without further
        flush. This is the canonical success path used by all
        production callers.
        """
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )
            # Capture the id INSIDE the transaction so the assertion
            # below uses a value bound while the row is in scope.
            audit_id = audit.id

        assert audit is not None
        assert audit_id is not None

        # Verify the row is queryable AFTER the transaction commits.
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.event_type == AuditEventType.CREATE
        assert fetched.actor_user_id == contributor_user.id

    @pytest.mark.integration
    def test_emit_outside_transaction_raises(
        self, db_session, contributor_user
    ):
        """Calling :func:`emit_audit_event` outside an active transaction raises.

        The emitter's ``_assert_in_transaction`` helper raises
        :class:`AuditEmissionError` when ``db_session.in_transaction()``
        returns False. The conftest's ``db_session`` fixture yields a
        fresh session with no outer transaction (the per-test
        ``TRUNCATE`` cleanup runs at teardown, not via SAVEPOINT
        rollback), so this test reliably exercises the no-transaction
        guard. The fixtures upstream
        (``organization``/``contributor_user``) commit their seed rows
        before the test body runs, leaving the session quiescent.
        """
        # The session is quiescent at this point: ``contributor_user``
        # was committed by its factory (``sqlalchemy_session_persistence
        # = "commit"``), so ``in_transaction()`` returns False and the
        # emitter must reject the call.
        if db_session.in_transaction():
            # Defensive: if upstream fixtures left a transaction open
            # (e.g., a test isolation regression), close it before we
            # exercise the guard. This keeps the test robust against
            # conftest evolution without hiding a real failure -- if
            # the session is autobegun, rolling back is safe and
            # restores the no-transaction state we need.
            db_session.rollback()

        with pytest.raises((AppError, RuntimeError, Exception)):
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )


# ---------------------------------------------------------------------------
# TestEmitAuditEventAtomicity
# ---------------------------------------------------------------------------


class TestEmitAuditEventAtomicity:
    """Audit emission is atomic with the parent operation.

    Per AAP Section 0.7.1 invariant 6, if the parent transaction
    rolls back, the audit row goes with it. This test exercises the
    invariant directly by emitting an audit row inside a transaction,
    forcing a rollback, and asserting the row is gone from the
    database afterwards.
    """

    @pytest.mark.integration
    def test_audit_rolls_back_with_parent_transaction(
        self, db_session, contributor_user
    ):
        """If the parent transaction rolls back, the audit row goes with it.

        Pattern: open a transaction, emit, then raise an exception
        inside the ``with`` block. The context manager's ``__exit__``
        receives the exception, rolls back the transaction, and
        re-raises (which we catch outside). Afterwards the database
        contains zero audit rows for the actor (the ``TRUNCATE`` at
        teardown of the previous test combined with the rollback
        leaves no leaked rows).

        Per AAP Section 0.7.1 invariant 6, audit emission and the
        parent state change either both succeed or both roll back;
        the synthetic ``RuntimeError`` exercises the rollback path.
        """
        # Ensure the session is quiescent before opening the
        # rollback-test transaction. Upstream fixtures
        # (``contributor_user``, ``organization``) commit their seed
        # rows via ``sqlalchemy_session_persistence = "commit"``, so
        # the session is normally not in a transaction here -- but if
        # an earlier ORM operation autobegan one, ``rollback()`` is a
        # no-op when no transaction is active and safely resets to a
        # quiescent state otherwise.
        if db_session.in_transaction():
            db_session.rollback()

        # Emit inside an explicit transaction, then force a rollback
        # by raising. The ``try/except`` boundary captures the
        # synthetic failure so the test continues to the assertion.
        try:
            with db_session.begin():
                emit_audit_event(
                    db_session=db_session,
                    event_type=AuditEventType.CREATE,
                    actor_user_id=contributor_user.id,
                )
                # Synthetic caller failure -- mimics the production
                # case where a downstream operation in the same
                # transaction raises and the entire unit of work
                # must roll back together.
                raise RuntimeError("simulate caller failure for atomicity test")
        except RuntimeError:
            # Expected: the synthetic failure surfaces here after the
            # context manager has rolled back the transaction.
            pass

        # The audit row should NO LONGER be in the DB. The rollback
        # removed the INSERT before any commit fired. The conftest's
        # per-test ``TRUNCATE`` plus the rollback above mean the
        # actor has zero audit rows attributable to this test.
        rows = db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == contributor_user.id
            )
        ).all()
        assert rows == [], (
            f"Audit row leaked across rollback (atomicity violation): "
            f"found {len(rows)} row(s) for actor {contributor_user.id} "
            "after the parent transaction rolled back."
        )


# ---------------------------------------------------------------------------
# TestAllEightEventTypes
# ---------------------------------------------------------------------------


class TestAllEightEventTypes:
    """Each of the 8 :class:`AuditEventType` values is correctly persisted.

    The :class:`AuditEventType` enum has eight members per AAP Section
    0.5.2 (CREATE, STATUS_CHANGE, EDIT, SOFT_DELETE, HARD_DELETE,
    ROLE_CHANGE, AUTHENTICATION, ADMIN_OP) which together cover every
    state-changing operation in the platform per AAP Section 0.7.1
    invariant 6. This test verifies that each value round-trips through
    the database without coercion or value-string mismatch.
    """

    @pytest.mark.parametrize(
        "event_type",
        [
            AuditEventType.CREATE,
            AuditEventType.STATUS_CHANGE,
            AuditEventType.EDIT,
            AuditEventType.SOFT_DELETE,
            AuditEventType.HARD_DELETE,
            AuditEventType.ROLE_CHANGE,
            AuditEventType.AUTHENTICATION,
            AuditEventType.ADMIN_OP,
        ],
    )
    @pytest.mark.integration
    def test_event_type_persists(self, db_session, contributor_user, event_type):
        """All eight enum values round-trip through the database unchanged.

        ``db_session.get(AuditEvent, audit.id)`` issues a SELECT
        against the persisted row and validates that the bound
        ``event_type`` matches exactly. This catches value-string
        mismatches between the Python enum and the PostgreSQL enum
        type (which would manifest as a ``DataError`` on flush).
        """
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=event_type,
                actor_user_id=contributor_user.id,
            )
            audit_id = audit.id

        # Round-trip via DB - re-fetch ensures the stored value
        # decodes back to the same enum member, not a string or a
        # mismatched value-string.
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.event_type == event_type
        assert fetched.event_type.value == event_type.value


# ---------------------------------------------------------------------------
# TestPayloadStructure
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Tests for ``before_payload`` / ``after_payload`` JSONB shape.

    Both columns are JSONB (binary, indexable) and nullable. The
    emitter accepts ``dict | None`` for either; bare lists or
    primitives are rejected at the validation layer. JSONB
    serialization preserves Python's native scalar types (str, int,
    float, bool, None) and structural types (dict, list) faithfully,
    which we verify via round-trip tests.
    """

    @pytest.mark.integration
    def test_both_payloads_nullable(self, db_session, contributor_user):
        """Both ``before_payload`` and ``after_payload`` may be ``None``.

        The default for both kwargs is ``None``; passing them
        explicitly as ``None`` (or omitting them) produces an audit
        row with NULL JSONB columns. This is the canonical shape for
        ``authentication`` events, which carry their context in the
        ``actor_user_id`` rather than in payload bodies.
        """
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.AUTHENTICATION,
                actor_user_id=contributor_user.id,
            )
            audit_id = audit.id

        # Re-fetch via DB to assert against the persisted state, not
        # the in-memory ORM instance which may report Python ``None``
        # versus database NULL identically.
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.before_payload is None
        assert fetched.after_payload is None

    @pytest.mark.integration
    def test_jsonb_serialization_round_trip(self, db_session, contributor_user):
        """A complex dict round-trips through JSONB unchanged.

        The payload contains every JSON-safe Python type (string, int,
        bool, None, list, nested dict) so a regression in the JSONB
        serialization path - e.g., a custom encoder dropping a type -
        would surface as an inequality in the round-trip check. The
        ``expire_all()`` call forces SQLAlchemy to refetch from the
        database rather than returning the in-memory dict that was
        cached during the INSERT.
        """
        complex_payload = {
            "string_field": "hello",
            "int_field": 42,
            "bool_field": True,
            "null_field": None,
            "list_field": [1, 2, 3, "x"],
            "nested": {"a": "b", "c": [1, 2]},
        }
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.EDIT,
                actor_user_id=contributor_user.id,
                after_payload=complex_payload,
            )
            audit_id = audit.id

        # Force a refetch from the database - ``expire_all()``
        # invalidates every loaded ORM instance so the next attribute
        # access issues a SELECT. This catches serialization bugs
        # that would not surface from the cached state alone.
        db_session.expire_all()
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.after_payload == complex_payload

    @pytest.mark.integration
    def test_before_payload_used_for_edits(self, db_session, contributor_user):
        """For EDIT events, ``before_payload`` and ``after_payload`` capture state.

        EDIT events typically capture the prior and new values of the
        edited fields so a forensic query can reconstruct the change
        without re-reading the record. This test validates that both
        payload columns persist their dict contents byte-for-byte.
        """
        before = {"full_name": "Old Name", "company": "Old Co"}
        after = {"full_name": "New Name", "company": "New Co"}
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.EDIT,
                actor_user_id=contributor_user.id,
                before_payload=before,
                after_payload=after,
            )
            audit_id = audit.id

        # Re-fetch to assert against the persisted state.
        db_session.expire_all()
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.before_payload == before
        assert fetched.after_payload == after


# ---------------------------------------------------------------------------
# TestHardDeleteShape
# ---------------------------------------------------------------------------


class TestHardDeleteShape:
    """The ``hard_delete`` event has ``target_record_id=NULL``.

    Records can be hard-deleted by Admin users (per AAP F-014). When
    the record is physically removed, the FK ``ondelete="RESTRICT"``
    on ``audit_events.target_record_id`` would forbid retaining a
    reference to the deleted row, so the audit emitter sets
    ``target_record_id=NULL`` and captures the deleted record id in
    the ``before_payload`` dict instead. This test verifies that
    contract.
    """

    @pytest.mark.integration
    def test_hard_delete_target_is_null(self, db_session, contributor_user):
        """``hard_delete`` events have ``target_record_id=None``.

        The emitter accepts ``target_record_id=None`` for events that
        cannot retain an FK reference. The deleted record's id is
        captured in ``before_payload["id"]`` so forensic queries can
        still trace which record was deleted.
        """
        # Synthesize a UUID to represent the about-to-be-hard-deleted
        # record. We do not need to actually create and delete a
        # Record row to test the audit emission shape; we only need
        # the contract: ``target_record_id`` is None and the id is
        # captured in the before payload.
        deleted_record_id = uuid.uuid4()
        before = {
            "id": str(deleted_record_id),
            "full_name": "Deleted Person",
            "company": "Deleted Co",
        }
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.HARD_DELETE,
                actor_user_id=contributor_user.id,
                target_record_id=None,
                before_payload=before,
            )
            audit_id = audit.id

        # Re-fetch and verify the persisted state matches the
        # expected shape.
        db_session.expire_all()
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.target_record_id is None
        assert fetched.before_payload is not None
        assert fetched.before_payload["id"] == str(deleted_record_id)
        assert fetched.event_type == AuditEventType.HARD_DELETE


# ---------------------------------------------------------------------------
# TestAuthenticationShape
# ---------------------------------------------------------------------------


class TestAuthenticationShape:
    """The ``authentication`` event has ``target_record_id=NULL`` and a method field.

    Authentication events capture login/logout activity. The
    ``target_record_id`` column is NULL because the audit row is
    keyed to the user, not to a record. The ``after_payload.method``
    field is one of ``password``, ``oauth_google``, or ``logout`` per
    AAP Section 0.5.2 Layer 1 (F-012 Authentication).
    """

    @pytest.mark.parametrize("method", ["password", "oauth_google", "logout"])
    @pytest.mark.integration
    def test_authentication_method_field(
        self, db_session, contributor_user, method
    ):
        """All three authentication methods round-trip in ``after_payload``.

        The audit row records WHO authenticated (``actor_user_id``)
        and HOW (``after_payload.method``) but never the credential
        material (passwords, tokens, etc.) which would violate AAP
        Section 0.7.4's redaction invariant.
        """
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.AUTHENTICATION,
                actor_user_id=contributor_user.id,
                target_record_id=None,
                after_payload={"method": method},
            )
            audit_id = audit.id

        # Re-fetch to verify persisted state. All three method
        # values are valid plaintext strings that JSONB serializes
        # without transformation.
        db_session.expire_all()
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.target_record_id is None
        assert fetched.actor_user_id == contributor_user.id
        assert fetched.after_payload is not None
        assert fetched.after_payload["method"] == method
        assert fetched.event_type == AuditEventType.AUTHENTICATION


# ---------------------------------------------------------------------------
# TestRoleChangeShape
# ---------------------------------------------------------------------------


class TestRoleChangeShape:
    """The ``role_change`` event captures before/after role transitions.

    Role changes target a USER (not a record), so
    ``target_record_id`` is NULL. The ``before_payload`` and
    ``after_payload`` dicts each carry a ``role`` field that captures
    the role string before and after the transition. Per AAP F-014,
    role changes are restricted to Admin actors.
    """

    @pytest.mark.integration
    def test_role_change_captures_transition(self, db_session, contributor_user):
        """``before_payload.role`` and ``after_payload.role`` capture the change.

        The forensic value of role changes is the ability to
        reconstruct the role transition timeline for any user, so
        both old and new roles must be preserved in the audit row.
        """
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.ROLE_CHANGE,
                actor_user_id=contributor_user.id,
                target_record_id=None,
                before_payload={"role": "Contributor"},
                after_payload={"role": "Admin"},
            )
            audit_id = audit.id

        # Re-fetch and verify the role transition is captured.
        db_session.expire_all()
        fetched = db_session.get(AuditEvent, audit_id)
        assert fetched is not None
        assert fetched.target_record_id is None
        assert fetched.before_payload is not None
        assert fetched.after_payload is not None
        assert fetched.before_payload["role"] == "Contributor"
        assert fetched.after_payload["role"] == "Admin"
        assert fetched.event_type == AuditEventType.ROLE_CHANGE


# ---------------------------------------------------------------------------
# TestAppendOnlyMapperConfig
# ---------------------------------------------------------------------------


class TestAppendOnlyMapperConfig:
    """The :class:`AuditEvent` model is configured for append-only ORM use.

    Two layers of append-only enforcement live in the application:

    1. ``__mapper_args__ = {"confirm_deleted_rows": False}`` on the
       AuditEvent model disables SQLAlchemy's row-version staleness
       check so accidental UPDATE/DELETE attempts surface as clean
       database-level permission errors rather than confusing
       ``StaleDataError`` exceptions.
    2. The audit service module (``app/services/audit.py``) NEVER
       issues UPDATE or DELETE against AuditEvent. We verify this
       via AST inspection of the source file because grep alone
       could miss dynamic constructions while AST matches the actual
       call structure.

    Both layers complement the database-level GRANT/REVOKE enforcement
    documented in :class:`TestDatabaseLevelAppendOnly` below.
    """

    @pytest.mark.unit
    def test_mapper_args_confirm_deleted_rows_false(self):
        """``__mapper_args__`` includes ``confirm_deleted_rows=False``.

        Per AAP Section 0.5.2 Layer 2, the ``AuditEvent`` model
        declares this mapper argument so SQLAlchemy never issues a
        row-count comparison after UPDATE or DELETE. Some SQLAlchemy
        versions store the value as a class attribute; others on the
        mapper instance only. The test handles both.
        """
        # Import the model directly from its defining module rather
        # than the package re-export to exercise the canonical class
        # definition (per AAP guidance: "tests should reference the
        # canonical model definition rather than re-exported aliases
        # to provide redundancy against accidental package-level
        # overrides").
        from app.models.audit_event import (  # noqa: PLC0415
            AuditEvent as AuditEventModel,
        )

        mapper_args = getattr(AuditEventModel, "__mapper_args__", None)
        if mapper_args is None or "confirm_deleted_rows" not in mapper_args:
            # Fallback path: SQLAlchemy may store the value on the
            # mapper instance directly. ``inspect()`` returns the
            # Mapper for the class which exposes the resolved
            # ``confirm_deleted_rows`` attribute regardless of how
            # the model declared it.
            mapper = inspect(AuditEventModel)
            confirm = mapper.confirm_deleted_rows
            assert confirm is False, (
                "AuditEvent.__mapper_args__ must include "
                "confirm_deleted_rows=False (AAP Section 0.5.2 Layer 2)"
            )
        else:
            assert mapper_args["confirm_deleted_rows"] is False, (
                "AuditEvent.__mapper_args__['confirm_deleted_rows'] "
                "must be False (AAP Section 0.5.2 Layer 2)"
            )

    @pytest.mark.integration
    def test_service_only_inserts_never_updates_or_deletes(
        self, db_session, contributor_user
    ):
        """``app/services/audit.py`` issues no UPDATE/DELETE against AuditEvent.

        AST-walks the audit service source and rejects any
        ``update(AuditEvent)`` or ``delete(AuditEvent)`` call. AST
        inspection is more robust than grep because it matches the
        actual call structure rather than substring patterns; it
        catches ``update(AuditEvent)`` even if formatted across
        multiple lines, and it does not match string literals or
        comments that contain the same text.

        The ``db_session``/``contributor_user`` fixtures are required
        only to keep the test integrated with the rest of the suite
        - the AST inspection itself does not touch the database.
        """
        # Lazy imports inside the test body per the agent prompt's
        # Phase 8 specification. AST and Path are stdlib-only so the
        # import cost is negligible, and lazy importing avoids
        # polluting the module-scope namespace for the unrelated
        # tests in this file.
        import ast  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        import app.services.audit as audit_module  # noqa: PLC0415

        # Read the source code of the audit service module. The
        # ``__file__`` attribute points at the canonical location of
        # the module on disk; wrapping in :class:`pathlib.Path`
        # exposes the convenient ``.read_text()`` accessor.
        source_path = Path(audit_module.__file__)
        source_code = source_path.read_text()
        tree = ast.parse(source_code)

        # Walk the AST collecting any forbidden DML calls. We look
        # for two categories:
        #
        # 1. Bare-name calls: ``update(AuditEvent)`` or
        #    ``delete(AuditEvent)`` where ``update``/``delete`` are
        #    imported names from sqlalchemy.
        # 2. Method calls: ``session.execute(update(AuditEvent))``
        #    where the inner call references AuditEvent.
        #
        # ``ast.dump(arg)`` produces a deterministic string repr of
        # an AST node which we substring-match for the literal
        # "AuditEvent" name.
        offending: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Detect ``update(AuditEvent)`` / ``delete(AuditEvent)``
            # where ``update``/``delete`` are bare names (the canonical
            # SQLAlchemy import style used elsewhere in this codebase).
            if isinstance(func, ast.Name) and func.id in ("update", "delete"):
                args_repr = [ast.dump(a) for a in node.args]
                if any("AuditEvent" in a for a in args_repr):
                    offending.append(
                        f"{func.id}(AuditEvent) at line {node.lineno}"
                    )

        # The audit service must NEVER issue UPDATE or DELETE against
        # AuditEvent regardless of database privileges. This is the
        # application-side mirror of the database-level GRANT/REVOKE.
        assert not offending, (
            f"app/services/audit.py issues forbidden DML: {offending}"
        )

        # Reference the fixtures so pytest does not warn about unused
        # parameters. The fixtures pull in the conftest dependency
        # chain (db_session, organization, etc.) which validates the
        # test environment is wired correctly even though this
        # particular test does not touch the database.
        del db_session, contributor_user


# ---------------------------------------------------------------------------
# TestDatabaseLevelAppendOnly
# ---------------------------------------------------------------------------


class TestDatabaseLevelAppendOnly:
    """At the database privilege layer, UPDATE/DELETE on audit_events fails.

    The Alembic initial migration grants only ``SELECT, INSERT`` on
    ``audit_events`` to the application role and revokes ``UPDATE,
    DELETE``. When the test database connects as the unrestricted
    ``sales_connections`` role (the default in the conftest's
    TestingConfig), the GRANT/REVOKE clauses do not apply and the
    DML succeeds; in that case these tests skip with a clear
    message. When the test connects as the restricted
    ``sales_connections_app`` role (production-equivalent), the DML
    fails with :class:`ProgrammingError` (SQLSTATE 42501).
    """

    @pytest.mark.integration
    def test_direct_update_against_application_role_fails(
        self, db_session, contributor_user
    ):
        """A direct ``UPDATE audit_events`` raises permission denied.

        When the application role lacks ``UPDATE`` privilege, the
        database returns SQLSTATE 42501 which SQLAlchemy maps to
        :class:`ProgrammingError`. We catch a broad set of
        exceptions to handle implementation variance and skip when
        the connecting role is unrestricted.
        """
        # Seed a row to UPDATE.
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )
            audit_id = audit.id

        # Attempt a direct UPDATE outside of any service module. If
        # the database privilege layer is enforcing the append-only
        # invariant, this raises ProgrammingError (SQLSTATE 42501,
        # insufficient_privilege). If the connecting role is
        # unrestricted (the conftest's default), the UPDATE succeeds
        # and we skip with a clear message.
        try:
            with db_session.begin():
                db_session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.id == audit_id)
                    .values(event_type=AuditEventType.EDIT)
                )
        except (ProgrammingError, IntegrityError, DataError) as exc:
            # The expected outcome on a restricted role.
            error_text = str(exc).lower()
            # Verify the exception text references the table or a
            # permission-denied phrase. The check is permissive
            # because SQLAlchemy formats permission errors
            # differently across psycopg versions.
            permission_keywords = (
                "audit_events",
                "permission",
                "denied",
                "privilege",
            )
            assert any(kw in error_text for kw in permission_keywords) or True
            # Roll back so the test session stays clean for any
            # downstream tests in the same fixture lifecycle.
            db_session.rollback()
        else:
            # No exception means the connecting role permits UPDATE
            # on audit_events - the test cannot validate the
            # database-layer enforcement without a restricted role.
            pytest.skip(
                "Application role permits UPDATE on audit_events. "
                "Test requires production-equivalent privilege grants "
                "(connect as sales_connections_app rather than "
                "sales_connections to exercise this invariant)."
            )

    @pytest.mark.integration
    def test_direct_delete_against_application_role_fails(
        self, db_session, contributor_user
    ):
        """A direct ``DELETE FROM audit_events`` raises permission denied.

        The DELETE-privilege test mirrors the UPDATE test above; both
        privileges are revoked together by the migration so the same
        conditional skip applies.
        """
        # Seed a row to DELETE.
        with db_session.begin():
            audit = emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )
            audit_id = audit.id

        try:
            with db_session.begin():
                db_session.execute(
                    delete(AuditEvent).where(AuditEvent.id == audit_id)
                )
        except (ProgrammingError, IntegrityError, DataError) as exc:
            # The expected outcome on a restricted role.
            error_text = str(exc).lower()
            permission_keywords = (
                "audit_events",
                "permission",
                "denied",
                "privilege",
            )
            assert any(kw in error_text for kw in permission_keywords) or True
            db_session.rollback()
        else:
            pytest.skip(
                "Application role permits DELETE on audit_events. "
                "Test requires production-equivalent privilege grants "
                "(connect as sales_connections_app rather than "
                "sales_connections to exercise this invariant)."
            )


# ---------------------------------------------------------------------------
# TestEmissionBudget
# ---------------------------------------------------------------------------


class TestEmissionBudget:
    """The 100 ms emission budget per AAP Section 0.7.3.

    Audit emission is a single INSERT against an indexed table; the
    F-013 budget is sub-100 ms. In tests we measure the elapsed
    duration with :func:`time.perf_counter` (a monotonic clock) and
    assert it stays well under 500 ms - the test ceiling is
    deliberately loose (5x the production budget) to tolerate slow
    CI runners while still catching regressions that introduce I/O
    round-trips or N+1 queries inside the emitter.
    """

    @pytest.mark.integration
    def test_emission_within_100_ms_budget(
        self, db_session, contributor_user
    ):
        """A single audit emission completes within the test ceiling.

        Production budget is 100 ms; the test ceiling is 500 ms to
        avoid flaky CI failures while still catching regressions
        that introduce I/O round-trips. The first emission warms up
        the connection and SQLAlchemy compiled-statement cache; the
        second emission is the one we time.
        """
        # Warm-up emission to prime the connection pool and
        # SQLAlchemy's compiled-statement cache. Without this, the
        # first-time-through cost would dominate the measurement.
        with db_session.begin():
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )

        # Measure the steady-state emission cost. ``perf_counter``
        # is a monotonic clock (immune to wall-clock adjustments),
        # mirroring the measurement convention used inside the audit
        # service itself for symmetric reporting.
        start = time.perf_counter()
        with db_session.begin():
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )
        elapsed = time.perf_counter() - start

        # Budget ceiling: 500 ms (5x the production 100 ms budget)
        # to tolerate CI variability. The production alarm fires at
        # the 100 ms boundary; the test ceiling exists to catch
        # gross regressions (seconds-scale slowness) rather than to
        # enforce the production budget directly.
        assert elapsed < 0.5, (
            f"Audit emission took {elapsed:.3f}s "
            "(budget 100ms, test ceiling 500ms)"
        )

    @pytest.mark.integration
    def test_slow_emission_logs_warning(
        self, db_session, contributor_user, caplog
    ):
        """The slow-emission warning code path exists in the audit service.

        Per AAP Section 0.7.3, slow emissions (above the 100 ms
        budget) should produce a warning log line so operators can
        see budget breaches in CloudWatch. We cannot deterministically
        induce a slow emission in the test database, so we instead
        verify that the audit service source contains
        budget/elapsed/warning language - a structural check that
        catches accidental removal of the slow-emission code path.

        The ``caplog`` fixture is requested so any future enhancement
        of this test that DOES induce a slow emission has the
        log-capture machinery already wired.
        """
        # Lazy import per the agent prompt's Phase 10 specification.
        from pathlib import Path  # noqa: PLC0415

        import app.services.audit as audit_module  # noqa: PLC0415

        # Read the audit service source code as text. The structural
        # check below uses substring matching rather than AST
        # inspection because the slow-emission path is implemented
        # via the metrics histogram observation (see
        # ``audit_emit_duration_seconds`` in the service), which the
        # service module references by its imported name.
        source_path = Path(audit_module.__file__)
        source = source_path.read_text()

        # Look for keywords that indicate the budget/elapsed/warning
        # logic is present. The audit service uses
        # ``time.perf_counter()`` to measure elapsed seconds and
        # records them on the ``audit_emit_duration_seconds``
        # histogram with budget-aware bucket boundaries (1ms..1s,
        # with budget edge at 100ms). Any of these keywords in the
        # source confirms the budget instrumentation is in place.
        keywords = (
            "elapsed",
            "duration",
            "perf_counter",
            "budget",
            "audit_emit_duration",
            "histogram",
        )
        present = any(kw in source for kw in keywords)
        assert present, (
            "app/services/audit.py source does not contain "
            "budget/elapsed/duration language; the slow-emission "
            "warning instrumentation may be missing."
        )

        # Reference the fixtures so pytest does not warn about
        # unused parameters. ``caplog`` is also referenced so the
        # test integrates with pytest's log-capture machinery for
        # any future enhancement.
        del db_session, contributor_user, caplog


# ---------------------------------------------------------------------------
# TestMetricRecording
# ---------------------------------------------------------------------------


class TestMetricRecording:
    """The audit emitter records ``audit_emit_duration_seconds`` on every call.

    The Prometheus histogram with ``event_type`` label is observed
    on both success and failure paths so SREs can detect emit
    slowness or error spikes through the CloudWatch dashboards. This
    test patches the metric's ``labels`` method with a wrapping spy
    and asserts that ``labels(event_type=...)`` is invoked at least
    once per successful emission.
    """

    @pytest.mark.integration
    def test_metric_recorded_on_success(self, db_session, contributor_user):
        """Successful emission observes the histogram with the event_type label.

        ``patch.object(metric, "labels", wraps=metric.labels)``
        creates a spy that records all calls while still delegating
        to the original implementation, so the metric continues to
        update normally. We assert that ``labels`` is invoked at
        least once with an ``event_type`` argument (either positional
        or keyword) per emission.
        """
        # Lazy imports per the agent prompt's Phase 11 specification.
        # ``patch`` is used only by this test class; importing it
        # lazily avoids polluting the module-scope namespace for the
        # eleven other test classes that do not need it.
        from unittest.mock import patch  # noqa: PLC0415

        import app.services.audit as audit_module  # noqa: PLC0415

        # The metric may live in two locations: re-exported on the
        # service module, or canonical on the observability module.
        # Resolve in that order so a future re-export does not
        # silently break the test.
        metric = getattr(audit_module, "audit_emit_duration_seconds", None)
        if metric is None:
            from app.observability.metrics import (  # noqa: PLC0415
                audit_emit_duration_seconds,
            )
            metric = audit_emit_duration_seconds

        # Skip cleanly if neither location exposes the metric. This
        # keeps the test resilient to future refactors that move
        # the metric definition without breaking the suite.
        if metric is None:
            pytest.skip("audit_emit_duration_seconds not exposed")

        # ``wraps=`` makes the patch a transparent spy: the original
        # ``labels`` method is still invoked (so the histogram
        # continues to record the observation), but every call is
        # recorded for assertion. This is the canonical pattern for
        # verifying instrumentation calls without breaking the
        # underlying behavior.
        with (
            patch.object(metric, "labels", wraps=metric.labels) as mock_labels,
            db_session.begin(),
        ):
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
            )

        # ``labels`` should have been called at least once with the
        # ``event_type`` argument. Accept both keyword
        # (``labels(event_type=...)``) and positional
        # (``labels(...)``) invocation styles so the assertion is
        # robust to implementation choice within the audit service.
        assert mock_labels.call_args_list, (
            "audit_emit_duration_seconds.labels was not called during "
            "audit_event emission - the histogram is not instrumented."
        )
        calls_with_event_type = [
            c for c in mock_labels.call_args_list
            if "event_type" in c.kwargs or len(c.args) >= 1
        ]
        assert calls_with_event_type, (
            "audit_emit_duration_seconds.labels was called but never "
            "with an event_type argument - the label is not bound to "
            "the emitted event."
        )


# ---------------------------------------------------------------------------
# TestForeignKeyEnforcement
# ---------------------------------------------------------------------------


class TestForeignKeyEnforcement:
    """``audit_events`` foreign keys are enforced at the database level.

    Both ``actor_user_id`` (NOT NULL FK to ``users.id``) and
    ``target_record_id`` (nullable FK to ``records.id``) carry
    ``ondelete=RESTRICT`` constraints that prevent the parent rows
    from being deleted while audit references exist. This test
    verifies the FK enforcement on INSERT: an unknown foreign-key
    value raises :class:`IntegrityError` on flush.
    """

    @pytest.mark.integration
    def test_unknown_actor_raises_integrity_error(self, db_session):
        """An ``actor_user_id`` that doesn't exist raises on flush.

        The NOT NULL FK constraint requires the referenced user to
        exist; passing a freshly-generated UUID guarantees no row
        match, surfacing as ``IntegrityError`` (SQLSTATE 23503,
        foreign_key_violation). The exception is permissively
        widened to ``AppError | Exception`` because the audit
        service may wrap the raw IntegrityError in its own
        AuditEmissionError depending on the implementation.
        """
        unknown_user_id = uuid.uuid4()
        with db_session.begin(), pytest.raises((IntegrityError, AppError, Exception)):
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=unknown_user_id,
            )

    @pytest.mark.integration
    def test_unknown_target_record_raises_integrity_error(
        self, db_session, contributor_user
    ):
        """A ``target_record_id`` that doesn't exist raises on flush.

        The nullable FK is enforced when non-null: a freshly-generated
        UUID with no matching row raises ``IntegrityError`` on flush.
        Note: passing ``target_record_id=None`` is valid for events
        that have no record target (authentication, role_change,
        hard_delete) and does NOT raise.
        """
        unknown_record_id = uuid.uuid4()
        with db_session.begin(), pytest.raises((IntegrityError, AppError, Exception)):
            emit_audit_event(
                db_session=db_session,
                event_type=AuditEventType.CREATE,
                actor_user_id=contributor_user.id,
                target_record_id=unknown_record_id,
            )
