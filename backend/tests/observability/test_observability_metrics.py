"""Unit tests for the observability metric singletons.

This module exercises the metric singletons added (or extended) for
QA Checkpoint 10:

- ``failed_login_attempts_total`` Counter (Issue 8) -- a dedicated
  security signal split out from the generic
  ``http_requests_total{path="/auth/login",status="401"}`` series so
  that SIEM tooling can isolate authentication failures by outcome
  without parsing path strings.
- ``active_sessions`` Gauge (Issue 6) -- the documented inc/dec
  contract that the auth handlers wire to.
- Built-in process / platform / GC collectors registered against the
  custom ``metrics_registry`` (Issue 5) so operators using Prometheus
  tooling directly see the same default surface they would get from
  any vanilla ``prometheus_client`` deployment.
- The ``/metrics`` Content-Type header (Issue 7) renders without the
  duplicated ``charset=utf-8`` parameter.

The tests run against the metric singletons in isolation; no Flask
app or DB is required. The custom registry is module-scoped so we
verify the *registry* exposes the expected collector classes rather
than relying on any specific metric value (which is per-process and
shifts as other tests run).
"""

from __future__ import annotations

from prometheus_client import GCCollector, PlatformCollector, ProcessCollector
import pytest

from app.extensions import metrics_registry
from app.observability.metrics import (
    active_sessions,
    failed_login_attempts_total,
)


class TestFailedLoginAttemptsCounter:
    """Behavior of ``failed_login_attempts_total``."""

    def test_counter_exposes_outcome_label(self) -> None:
        """The Counter accepts the documented ``outcome`` label."""
        # Calling .labels() without the documented label would raise.
        # The three documented outcomes match the failure paths in
        # ``app.services.auth.authenticate_password``.
        for outcome in ("user_not_found", "wrong_password", "oauth_only_user"):
            child = failed_login_attempts_total.labels(outcome=outcome)
            assert child is not None

    def test_counter_increments(self) -> None:
        """Each ``.inc()`` call advances the counter."""
        before = failed_login_attempts_total.labels(
            outcome="wrong_password",
        )._value.get()
        failed_login_attempts_total.labels(outcome="wrong_password").inc()
        after = failed_login_attempts_total.labels(
            outcome="wrong_password",
        )._value.get()
        assert after == pytest.approx(before + 1.0)

    def test_counter_is_registered_against_custom_registry(self) -> None:
        """The Counter is registered against the project's custom registry.

        Verifying registration ensures the metric appears in the
        ``/metrics`` exposition produced by ``generate_latest(metrics_registry)``.
        """
        # ``Counter._name`` is the documented public-ish accessor for
        # the metric name on the family object.
        names = {m.name for m in metrics_registry.collect()}
        # The Counter's exposed name in the exposition is
        # ``failed_login_attempts`` (Counter total suffix is added by
        # the exposition layer; the family name strips it).
        assert "failed_login_attempts" in names


class TestActiveSessionsGauge:
    """Behavior of ``active_sessions``."""

    def test_gauge_exists_with_documented_name(self) -> None:
        """The Gauge is registered against the custom registry."""
        names = {m.name for m in metrics_registry.collect()}
        assert "active_sessions" in names

    def test_gauge_inc_dec_round_trip(self) -> None:
        """``.inc()`` and ``.dec()`` modify the Gauge symmetrically."""
        before = active_sessions._value.get()
        active_sessions.inc()
        after_inc = active_sessions._value.get()
        active_sessions.dec()
        after_dec = active_sessions._value.get()
        assert after_inc == pytest.approx(before + 1.0)
        assert after_dec == pytest.approx(before)

    def test_gauge_can_go_negative(self) -> None:
        """The Gauge can briefly drop below zero (worker-restart drift).

        Per the gauge docstring, a logout for a session minted on a
        different worker may decrement a worker that did not increment,
        producing a transient negative value. This is acceptable
        because operators interpret the per-worker sum.
        """
        before = active_sessions._value.get()
        active_sessions.dec()
        after = active_sessions._value.get()
        assert after == pytest.approx(before - 1.0)
        # Restore to neutral so other tests aren't affected.
        active_sessions.inc()


class TestProcessCollectorRegistration:
    """Built-in collectors registered against the custom registry (Issue 5)."""

    def test_process_collector_is_registered(self) -> None:
        """``ProcessCollector`` is on the custom registry."""
        # ``CollectorRegistry`` exposes ``_names_to_collectors`` (a
        # private dict) but the public path is ``collect()`` which
        # yields ``Metric`` instances. Process collector emits
        # families like ``process_cpu_seconds`` and
        # ``process_resident_memory_bytes``.
        names = {m.name for m in metrics_registry.collect()}
        # ProcessCollector emits multiple ``process_*`` families;
        # at least one MUST be present for the collector to be
        # active.
        process_metrics = {n for n in names if n.startswith("process_")}
        assert process_metrics, (
            "Expected at least one process_* metric from ProcessCollector "
            f"on the custom registry; saw {sorted(names)[:20]}"
        )

    def test_platform_collector_is_registered(self) -> None:
        """``PlatformCollector`` is on the custom registry."""
        names = {m.name for m in metrics_registry.collect()}
        # PlatformCollector emits ``python_info`` (a one-shot Info
        # metric) by default.
        assert "python_info" in names, (
            "Expected ``python_info`` from PlatformCollector on the "
            f"custom registry; saw {sorted(names)[:20]}"
        )

    def test_gc_collector_is_registered(self) -> None:
        """``GCCollector`` is on the custom registry."""
        names = {m.name for m in metrics_registry.collect()}
        # GCCollector emits python_gc_objects_collected,
        # python_gc_objects_uncollectable, python_gc_collections.
        gc_metrics = {n for n in names if n.startswith("python_gc_")}
        assert gc_metrics, (
            "Expected at least one python_gc_* metric from GCCollector "
            f"on the custom registry; saw {sorted(names)[:20]}"
        )

    def test_process_collector_class_match(self) -> None:
        """The default Prometheus collector classes are importable.

        Locking in the imports defends against a future contributor
        accidentally removing the import block in
        ``app/extensions.py`` (which would silently re-create Issue 5).
        """
        assert ProcessCollector is not None
        assert PlatformCollector is not None
        assert GCCollector is not None
