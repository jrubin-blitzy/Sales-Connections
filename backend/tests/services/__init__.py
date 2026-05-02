"""Pytest tests for ``app.services.*`` (Sales-Connections backend).

This package contains tests for every service-layer module under
``app.services``:

- ``test_audit.py`` exercises ``app.services.audit`` (F-013).
- ``test_duplicate_detection.py`` exercises
  ``app.services.duplicate_detection`` (F-010).
- ``test_ai_orchestration.py`` exercises ``app.services.ai_orchestration``
  (F-002).
- ``test_auth_service.py`` exercises ``app.services.auth`` (F-012).
- ``test_rbac_service.py`` exercises the cross-service RBAC permission
  matrix (F-009).
- ``test_connections_service.py`` exercises ``app.services.connections``
  (F-001 / F-004 / F-005 / F-007 / F-010 / F-011).
- ``test_admin_service.py`` exercises ``app.services.admin``
  (F-014 + F-007 hard delete).

See ``backend/tests/conftest.py`` for shared fixtures and
``backend/tests/factories.py`` for factory-boy factories used by every
test in this package.
"""
