"""API blueprint test subpackage.

Each ``test_<module>.py`` in this directory mirrors a Flask blueprint
under ``app/api/`` and exercises the full request lifecycle: route
resolution, authentication middleware, RBAC gating, pydantic schema
validation, service-layer invocation, audit emission, and JSON
response envelopes.

Test fixtures (clients, db_session, audit_assertion, mocks) are
inherited from ``backend/tests/conftest.py``; data factories live in
``backend/tests/factories.py``.
"""
