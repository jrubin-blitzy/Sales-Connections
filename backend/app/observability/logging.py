"""Structured logging configuration for the Sales-Connections backend.

This module exposes a single public function, ``configure_structlog``,
which is the very first call inside the Flask application factory in
``app.__init__``. It must run before any other initialization step so
that every subsequent log line emitted during startup is JSON-formatted
and carries the project's canonical context fields.

Context fields included on every log line (when bound by
``app.middleware.correlation`` and ``app.middleware.auth``):

- ``correlation_id``  Per-request UUID for cross-service correlation.
- ``user_id``         Authenticated user identifier (when present).
- ``org_id``          Organization identifier (when present).
- ``trace_id``        OpenTelemetry trace ID (when a span is active).
- ``span_id``         OpenTelemetry span ID (when a span is active).

Secret redaction (per AAP Section 0.7.4)
----------------------------------------
Values for keys matching any of these patterns are replaced with the
literal string ``***REDACTED***`` BEFORE the renderer runs:

- ``*password*``   (e.g., ``password``, ``db_password``, ``user_password``,
                    ``password_hash``)
- ``*secret*``     (e.g., ``client_secret``, ``aws_secret_access_key``,
                    ``shared_secret``)
- ``token``        (case-insensitive; ``access_token``, ``refresh_token``)
- ``authorization`` (HTTP header naming convention)
- ``bearer``       (HTTP ``Authorization: Bearer ...`` credential)
- ``cookie`` / ``set_cookie`` / ``set-cookie`` (raw HTTP cookie values)
- ``*api_key*``    (e.g., ``api_key``, ``ANTHROPIC_API_KEY``, ``stripe_apikey``)
- specific known-sensitive ``*_key`` variants: ``signing_key``,
  ``secret_key``, ``private_key``, ``encryption_key``, ``master_key``,
  ``session_key``. Generic ``*_key`` is INTENTIONALLY NOT redacted to
  avoid false positives on benign debug context like ``sort_key``,
  ``cache_key``, ``partition_key``, ``cursor_key`` (per QA Issue 13).

The redactor walks dictionaries recursively but does not descend into
arbitrary objects, by design, to avoid expensive reflection on every
log line.

Stdlib logging integration
--------------------------
Records emitted by SQLAlchemy, Authlib, Werkzeug, boto3, urllib3, and
any other stdlib-``logging``-using library are routed through the same
structlog processor chain so the JSON output is uniform across the
process. See ``_configure_stdlib_logging`` for details.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: This file is itself named ``logging.py``, which collides with
# the stdlib ``logging`` module name. Toolchain configurations that
# treat ``app/observability/`` as a PEP 420 namespace package without
# ``explicit_package_bases`` (notably the project's current mypy
# configuration) misresolve a direct ``import logging`` from inside
# this file as a self-import. We work around the collision by going
# through ``importlib.import_module``: at runtime this is identical
# to the direct import, and at type-check time the ``Any`` typing
# defuses the namespace conflict without affecting the runtime
# behavior or surface area of the module.
from collections.abc import MutableMapping
import importlib
import re
import sys
from typing import TYPE_CHECKING, Any

# Import the stdlib ``logging`` module via ``importlib`` to bypass the
# same-name file-shadowing issue described above. The ``Any`` type
# annotation is intentional: it makes the wrapper opaque to mypy so
# that mypy does not attempt the (incorrect, namespace-package-quirk)
# resolution against this very file. The runtime behavior is
# identical to ``import logging as stdlib_logging``.
stdlib_logging: Any = importlib.import_module("logging")

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
# structlog is pinned in backend/requirements.txt at ==24.4.0; the
# defensive try/except below ensures this module does not crash the
# process at import time even in a degraded environment where structlog
# happens to be missing (e.g., a minimal recovery shell). In that
# pathological case ``configure_structlog`` falls back to stdlib
# ``logging.basicConfig`` so the application still produces structured
# (if not JSON-shaped) output.
try:
    import structlog
    from structlog.contextvars import merge_contextvars
    from structlog.dev import ConsoleRenderer
    from structlog.processors import (
        JSONRenderer,
        StackInfoRenderer,
        TimeStamper,
        UnicodeDecoder,
        add_log_level,
        format_exc_info,
    )
    from structlog.stdlib import (
        ProcessorFormatter,
        add_logger_name,
        filter_by_level,
    )

    _STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive; structlog is pinned in requirements.txt
    _STRUCTLOG_AVAILABLE = False


if TYPE_CHECKING:
    # Type-only imports do not add runtime cost. ``Processor``,
    # ``EventDict``, ``WrappedLogger``, and ``BoundLogger`` are only
    # referenced in type annotations within this module.
    from structlog.stdlib import BoundLogger
    from structlog.types import EventDict, Processor, WrappedLogger


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Mapping from string log-level names to their stdlib ``logging.*``
# integer values. Includes ``WARN`` as an alias for ``WARNING`` per
# Python's own historical convention.
_LOG_LEVEL_MAP: dict[str, int] = {
    "CRITICAL": stdlib_logging.CRITICAL,
    "ERROR": stdlib_logging.ERROR,
    "WARNING": stdlib_logging.WARNING,
    "WARN": stdlib_logging.WARNING,
    "INFO": stdlib_logging.INFO,
    "DEBUG": stdlib_logging.DEBUG,
    "NOTSET": stdlib_logging.NOTSET,
}

# Case-insensitive regex matching key names that must have their values
# redacted before rendering. Patterns are designed to be used with
# ``re.fullmatch`` (whole-string match) and cover the OWASP-recommended
# common naming patterns:
#
#   * ``password`` / ``passwd`` -- plain authentication credentials,
#     including AWS-style compound names ``db_password``,
#     ``user_password``, and bcrypt's natural ``password_hash``.
#   * ``authorization`` -- HTTP Authorization header values.
#   * ``token`` and ``*_token`` -- bearer/access/refresh/id tokens, etc.
#   * ``*secret*`` -- ``client_secret``, ``shared_secret``,
#     ``aws_secret_access_key``, ``secret_token``, etc.
#   * Any name containing ``api_key``/``api-key``/``apikey`` --
#     covers ``stripe_api_key``, ``ANTHROPIC_API_KEY``,
#     ``my-api-key``, ``my_apikey``, etc.
#   * Specific known-sensitive ``*_key`` variants:
#     ``signing_key``, ``secret_key``, ``private_key``,
#     ``encryption_key``, ``master_key``, ``session_key``.
#   * ``bearer`` -- HTTP "Bearer <token>" credential header naming.
#   * ``cookie`` and ``set_cookie`` / ``set-cookie`` -- raw HTTP
#     cookie payloads (the actual ``Cookie:`` header value carries
#     session credentials; cookie *names* such as
#     ``session_cookie_name`` are unaffected).
#
# Per QA Issue 13: the previous broad ``.*_key`` pattern matched
# benign debug context like ``sort_key``, ``cache_key``,
# ``partition_key``, ``cursor_key``, etc., causing operationally
# helpful values to render as ``***REDACTED***`` and noisily
# obscuring log analysis. The narrowed regex below preserves
# false-positive-tolerant matching for true credential names while
# letting application-domain ``*_key`` identifiers (sort keys,
# cache keys, range keys) flow through to the renderer.
#
# Per QA Checkpoint 10 (Issue 1): the original variants
# ``password`` / ``.*_secret`` matched only on whole-key fullmatch
# and missed compound credential names common in AWS/database
# integrations: ``aws_secret_access_key``, ``db_password``,
# ``user_password``, ``password_hash``, ``Bearer``, ``cookie``.
# The pattern is widened to substring-style matches for
# ``password`` and ``secret`` (still anchored as whole-key matches
# via ``.*<word>.*``), explicit literals for ``bearer`` / ``cookie``
# / ``set_cookie``, and the documented behavior is mirrored in
# ``docs/security.md`` so contributors and operators see the same
# story. A new test class ``TestRedactSecretsProcessor`` in
# ``backend/tests/observability/test_logging_redaction.py`` covers
# every credential variant explicitly.
#
# False-negative risk analysis: the explicit ``*_key`` allowlist
# below covers every ``_key`` variant the codebase or its
# dependencies actually use. Future contributors adding a new
# secret-bearing key MUST either choose a name covered by the
# generic patterns (e.g., ``foo_token``, ``bar_secret``,
# ``baz_api_key``) OR extend this regex; the structlog redaction
# integration test exercises the canonical names so coverage gaps
# surface in CI.
_SECRET_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:"
    r".*password.*"
    r"|passwd"
    r"|authorization"
    r"|token"
    r"|.*_token"
    r"|.*secret.*"
    r"|.*api[_-]?key.*"
    r"|signing_key"
    r"|secret_key"
    r"|private_key"
    r"|encryption_key"
    r"|master_key"
    r"|session_key"
    r"|bearer"
    r"|cookie"
    r"|set[_-]?cookie"
    r")"
)

# The literal placeholder substituted for redacted values. A short,
# unambiguous, easily-greppable token chosen to make both human review
# and automated alerting straightforward.
_REDACTED_PLACEHOLDER: str = "***REDACTED***"

# Maximum recursion depth for the secret-redaction walker. Bounds
# pathological inputs (e.g., circular dict references via shared
# objects) without sacrificing coverage of realistically-shaped log
# event dicts.
_MAX_REDACT_DEPTH: int = 4

# Idempotency guard. ``configure_structlog`` is callable multiple times
# in the same process (notably across pytest sessions) and the second
# and subsequent calls must reset cleanly without leaving duplicate
# handlers attached to the root logger.
_initialized: bool = False


__all__ = [
    "add_open_telemetry_context",
    "add_stdlib_record_extras",
    "configure_structlog",
    "redact_secrets_processor",
]


# Standard ``logging.LogRecord`` attributes that are populated by the
# stdlib logger itself. Anything in the LogRecord's ``__dict__`` that
# is NOT in this set was added by a caller via the ``extra={...}``
# kwarg on ``logger.info(...)`` or similar; that diagnostic context
# MUST be promoted into the structlog event dict so it surfaces in the
# JSON log line. Per AAP Section 0.7.5 (Observability rule), every
# state-changing event MUST log identifying context with the operator
# (e.g., ``user_id``, ``required_roles``, ``tag_id``); without this
# extraction step those diagnostics are silently dropped.
_STDLIB_LOG_RECORD_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        # Standard LogRecord attributes from CPython's
        # ``logging.LogRecord.__init__`` and downstream computed
        # attributes. Sourced from
        # https://docs.python.org/3/library/logging.html#logrecord-attributes
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        # Python 3.12+ ``taskName`` for asyncio task identification.
        "taskName",
        # structlog's own bridging signals - never propagate these.
        "_record",
        "_from_structlog",
    }
)


# ---------------------------------------------------------------------------
# Custom processors (public)
# ---------------------------------------------------------------------------


def redact_secrets_processor(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace secret-named values with a placeholder before rendering.

    Walks the top-level event_dict and recurses into nested dicts and
    lists-of-dicts up to a bounded depth. Does not descend into
    arbitrary objects (by design, to keep every log line cheap to
    render).

    Per AAP Section 0.7.4 and the QA Checkpoint 10 widening, keys
    matching any of the following patterns are redacted
    (case-insensitive, whole-key fullmatch against ``_SECRET_KEY_PATTERN``):

    - Any key containing ``password`` (matches ``password``,
      ``db_password``, ``user_password``, ``password_hash``,
      ``aws_password``, etc.)
    - ``passwd``
    - ``authorization``
    - ``token`` and ``*_token``
    - Any key containing ``secret`` (matches ``secret``,
      ``aws_secret_access_key``, ``client_secret``, ``api_secret``, etc.)
    - Any key containing ``api_key``, ``api-key``, ``apikey``
    - Six narrow ``*_key`` literals: ``signing_key``, ``secret_key``,
      ``private_key``, ``encryption_key``, ``master_key``, ``session_key``
      (the broad ``.*_key`` pattern is intentionally NOT used per
      QA Issue 13 to avoid false-positive redaction of benign
      identifiers like ``sort_key`` / ``cache_key`` / ``cursor_key``)
    - ``bearer`` (raw Bearer token values)
    - ``cookie`` and ``set_cookie`` / ``set-cookie`` (raw HTTP cookie
      header payloads)

    Args:
        logger: The wrapped logger emitting the record. Unused; the
            signature is mandated by the structlog processor protocol.
        method_name: The level-named method invoked on the logger
            (``"info"``, ``"warning"``, etc.). Unused here.
        event_dict: The mutable event dict assembled by upstream
            processors. Mutated in place; same instance is returned.

    Returns:
        The mutated event_dict.
    """
    _redact_in_place(event_dict)
    return event_dict


def add_stdlib_record_extras(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Promote stdlib ``LogRecord`` ``extra={...}`` kwargs into the event dict.

    When a Python stdlib ``logging`` call is bridged through structlog's
    :class:`structlog.stdlib.ProcessorFormatter`, the original
    :class:`logging.LogRecord` is exposed on the event dict under the
    ``_record`` key. Any attributes added to that record via the
    ``extra={...}`` kwarg on ``logger.info("event_name", extra={"key":
    "value"})`` end up as instance attributes on the LogRecord but are
    NOT, by default, copied into the structlog event dict. The result
    is that operators see ``"event_name"`` in the JSON output but the
    ``key/value`` pairs they explicitly attached as forensic context
    are silently dropped.

    Per AAP Section 0.7.5 (Observability rule), every state-changing
    event MUST log structured diagnostic context (``user_id``,
    ``required_roles``, ``tag_id``, ``handler``, etc.). This
    processor ensures that context survives the stdlib->structlog
    bridge by walking the LogRecord's ``__dict__`` and copying every
    non-standard attribute (i.e., everything not in the canonical
    LogRecord field set) into the event dict.

    Native structlog records (those produced via
    ``structlog.get_logger(...)``) do not carry a ``_record`` key, so
    this processor is a no-op for them. That is correct: native
    structlog records already include their kwargs in the event dict
    by construction; only the stdlib bridge needs the extraction.

    Args:
        logger: The wrapped logger emitting the record. Unused; the
            signature is mandated by the structlog processor protocol.
        method_name: The level-named method invoked on the logger
            (``"info"``, ``"warning"``, etc.). Unused here.
        event_dict: The mutable event dict assembled by upstream
            processors. Mutated in place; same instance is returned.

    Returns:
        The mutated event_dict. When no stdlib record is attached
        (native structlog path), the event_dict is returned unchanged.
    """
    # ``_record`` is set by ``ProcessorFormatter.format`` when a stdlib
    # record is being bridged. Native structlog records never set it.
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    # ``record.__dict__`` contains every attribute set on the LogRecord
    # at construction time (standard fields populated by the stdlib
    # logger itself) PLUS any keys supplied by the caller via the
    # ``extra={...}`` kwarg (which CPython's logging module adds as
    # individual instance attributes at LogRecord creation time, not as
    # a nested ``extra`` mapping). We promote only the non-standard
    # attributes; standard fields are already known to the renderer
    # and either already in the event_dict or intentionally elided.
    record_dict: dict[str, Any] = getattr(record, "__dict__", {})
    for key, value in record_dict.items():
        # Skip standard LogRecord attributes (would clobber renderer
        # output) and any private/dunder names defensively.
        if key in _STDLIB_LOG_RECORD_RESERVED_ATTRS or key.startswith("_"):
            continue
        # ``setdefault`` so a caller that explicitly bound a key via
        # structlog's contextvars (for the same record name) takes
        # precedence over the stdlib ``extra`` value. This is the same
        # precedence ordering used by ``add_open_telemetry_context``.
        event_dict.setdefault(key, value)
    return event_dict


def add_open_telemetry_context(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add OpenTelemetry trace_id and span_id to the event dict if available.

    No-op when OpenTelemetry is not initialized (e.g., local dev with no
    collector). Uses a try/except guard so the absence of opentelemetry
    or the lack of an active span never breaks the logging pipeline.

    The trace_id is rendered as a 32-hex-character lowercase string and
    the span_id as a 16-hex-character lowercase string, matching the
    canonical OTel spec encoding consumed by every downstream
    observability backend (Honeycomb, Datadog, Tempo, etc.).

    Args:
        logger: The wrapped logger. Unused; required by the protocol.
        method_name: The log method name. Unused.
        event_dict: The mutable event dict; ``trace_id`` and ``span_id``
            are added via ``setdefault`` so any explicitly-passed values
            from the caller take precedence.

    Returns:
        The (possibly mutated) event_dict.
    """
    try:
        # Lazy import keeps this module safe when opentelemetry-api is
        # not installed (tests, recovery shells). The package is pinned
        # in requirements.txt so the import will succeed in normal
        # operation.
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
            event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    except Exception:  # noqa: S110 # pragma: no cover - defensive
        # Never let a logging processor crash the application. A
        # broken processor would otherwise propagate up through every
        # call site in the codebase. We deliberately suppress the
        # exception silently here: re-raising or even logging would
        # itself recurse through the very logger we are configuring.
        pass
    return event_dict


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _redact_in_place(node: MutableMapping[str, Any], depth: int = 0) -> None:
    """Apply secret redaction to a mapping in place. Bounded recursion.

    Recursion is bounded at depth 4 to defend against pathological
    inputs (circular structures via shared mapping references) without
    sacrificing coverage of realistically-shaped event dicts. The depth
    bound applies separately to dict and list traversal: a nested dict
    inside a list still counts as one level of additional depth.

    Lists are walked but their non-dict items are not modified — we are
    redacting based on KEY names, and list entries do not have keys.

    Accepts ``MutableMapping[str, Any]`` rather than the more concrete
    ``dict[str, Any]`` because structlog's ``EventDict`` is typed as
    ``MutableMapping[str, Any]`` (it may be backed by a plain ``dict``
    or by a custom mapping such as ``OrderedDict``).

    Args:
        node: The mapping to mutate. Most commonly the structlog
            ``EventDict`` passed into the processor entry point, but
            can also be any nested ``MutableMapping`` reached via
            recursion.
        depth: Current recursion depth. Callers pass ``0`` (the default
            from the public processor entry point).
    """
    if depth > _MAX_REDACT_DEPTH:  # safety bound to avoid runaway recursion
        return
    # Materialize the keys before iteration: we mutate ``node`` in
    # place during the loop (replacing values). ``fullmatch`` requires
    # the WHOLE key string to match the pattern, avoiding the
    # false-positive that ``match`` would otherwise have on, e.g.,
    # ``passwordless`` or ``tokenize`` (which start with the secret
    # word but are not themselves secret).
    for key in list(node.keys()):
        if isinstance(key, str) and _SECRET_KEY_PATTERN.fullmatch(key):
            node[key] = _REDACTED_PLACEHOLDER
            continue
        value = node[key]
        if isinstance(value, MutableMapping):
            _redact_in_place(value, depth=depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, MutableMapping):
                    _redact_in_place(item, depth=depth + 1)


def _resolve_log_level(level_name: str | None) -> int:
    """Map a string log level to its ``logging.*`` integer value.

    Falls back to ``logging.INFO`` for unrecognized values. Empty
    strings, whitespace-only strings, and ``None`` are all treated as
    "not provided".

    Args:
        level_name: The configured level name, e.g. ``"DEBUG"``,
            ``"info"``, ``"warn"``. Case-insensitive.

    Returns:
        The corresponding ``logging.*`` integer; ``logging.INFO`` for
        any unrecognized value.
    """
    if not level_name:
        return _LOG_LEVEL_MAP["INFO"]
    return _LOG_LEVEL_MAP.get(level_name.upper().strip(), _LOG_LEVEL_MAP["INFO"])


def _build_processor_chain(log_format: str, log_level_int: int) -> list[Processor]:
    """Construct the processor chain for native structlog loggers.

    Order matters; the chain is documented in
    :func:`configure_structlog`'s docstring.

    The chain ends with ``ProcessorFormatter.wrap_for_formatter``
    rather than directly with a renderer (``JSONRenderer`` /
    ``ConsoleRenderer``). This is the canonical structlog idiom for
    integrating with stdlib ``logging``: both native structlog records
    AND foreign stdlib records (SQLAlchemy, Authlib, Werkzeug, boto3,
    urllib3) flow through the SAME final ``ProcessorFormatter``, which
    applies the renderer exactly once. The ``log_format`` argument is
    consumed by the formatter, not here, so the same chain is used
    regardless of format; the format dictates only the renderer
    attached to the formatter (see :func:`_configure_stdlib_logging`).

    Args:
        log_format: ``"json"`` (production) or ``"console"`` (dev).
            Reserved for future use; currently only consumed by the
            stdlib formatter.
        log_level_int: The configured log level as an integer.
            Reserved for future level-aware processors. The
            level-filtering wrapper class is configured separately by
            :func:`configure_structlog` via
            ``structlog.make_filtering_bound_logger``.

    Returns:
        The complete processor chain ending with
        ``ProcessorFormatter.wrap_for_formatter``.
    """
    # Both parameters are intentionally retained in the signature so
    # that future extensions (e.g., level-aware redaction tuning,
    # console-only enrichment) can use them without changing call
    # sites; consume them here to silence lint:
    del log_format
    del log_level_int
    processors: list[Processor] = [
        filter_by_level,
        merge_contextvars,
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True),
        # Promote stdlib ``LogRecord`` ``extra={...}`` kwargs into the
        # event dict. No-op for native structlog records (which carry
        # no ``_record`` key); critical for stdlib-bridged records so
        # forensic context (``user_id``, ``required_roles``,
        # ``tag_id``, ``handler``, ...) reaches the JSON output.
        # Placed BEFORE redaction so any secret-named ``extra`` keys
        # go through the redactor.
        add_stdlib_record_extras,
        add_open_telemetry_context,
        StackInfoRenderer(),
        format_exc_info,
        UnicodeDecoder(),
        redact_secrets_processor,
        # Hand off to the stdlib ``ProcessorFormatter`` so the final
        # renderer is applied exactly once for BOTH native structlog
        # records and foreign stdlib records.
        ProcessorFormatter.wrap_for_formatter,
    ]
    return processors


def _configure_stdlib_logging(
    shared_pre_chain: list[Processor],
    log_level_int: int,
    log_format: str,
) -> None:
    """Route stdlib logging records through the structlog processor chain.

    Every record from third-party libraries (SQLAlchemy, Authlib,
    Werkzeug, boto3, urllib3, ...) is reformatted via the same
    :class:`structlog.stdlib.ProcessorFormatter` so the JSON output is
    uniform across the process.

    Resets handlers on the root logger to avoid duplicates when the
    function is called multiple times in the same process (test mode).

    Args:
        shared_pre_chain: The processors applied to BOTH native
            structlog records AND foreign stdlib records before the
            renderer runs. Excludes the final renderer because the
            ``ProcessorFormatter`` applies its own renderer on top.
        log_level_int: The configured log level as an integer.
        log_format: ``"json"`` or ``"console"``. Any other string is
            treated as ``"json"``.
    """
    if log_format.lower() == "console":
        renderer: Processor = ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        renderer = JSONRenderer()

    formatter = ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_pre_chain,
    )

    # Stdout-only output is the project convention. The CloudWatch
    # awslogs Docker driver captures both stdout and stderr, so this
    # is purely a hygiene choice.
    handler = stdlib_logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level_int)

    root = stdlib_logging.getLogger()
    # Remove any handlers from a prior configure call (test isolation
    # and idempotency). Without this, repeated invocations would emit
    # each log line N times where N is the number of prior calls.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level_int)

    # Quiet down chatty third-party loggers in production.
    _quiet_noisy_loggers(log_level_int)


def _quiet_noisy_loggers(log_level_int: int) -> None:
    """Set sensible per-logger levels for known-chatty libraries.

    These libraries emit a lot of low-value records at DEBUG/INFO
    (notably wire-level HTTP traces from urllib3 and SQL echo from
    SQLAlchemy). We cap them at WARNING regardless of the configured
    root level so that ``LOG_LEVEL=DEBUG`` for the application does
    not also enable the third-party noise.

    SQLAlchemy SQL echo is configured separately at the engine level
    via the ``DB_ECHO`` config flag (see :class:`app.extensions.SQLAlchemy`)
    and therefore is not affected by the level cap here for normal
    ORM usage.

    Args:
        log_level_int: The configured root log level. The cap is only
            applied if the root level is finer (numerically lower)
            than WARNING; otherwise the existing level is preserved.
    """
    noisy_pairs: tuple[tuple[str, int], ...] = (
        ("urllib3", stdlib_logging.WARNING),
        ("botocore", stdlib_logging.WARNING),
        ("boto3", stdlib_logging.WARNING),
        ("s3transfer", stdlib_logging.WARNING),
        ("werkzeug", stdlib_logging.WARNING),
        ("asyncio", stdlib_logging.WARNING),
        # SQLAlchemy: WARNING in production, DEBUG only when explicitly
        # requested via DB_ECHO. Echo is configured at engine level,
        # not here.
        ("sqlalchemy.engine", stdlib_logging.WARNING),
        ("sqlalchemy.pool", stdlib_logging.WARNING),
    )
    for name, level in noisy_pairs:
        named_logger = stdlib_logging.getLogger(name)
        # Only raise the bar if the configured root level is finer
        # (lower number = more verbose). If the root level is already
        # at WARNING or higher, leave the named logger alone and let
        # it inherit normally.
        if log_level_int < level:
            named_logger.setLevel(level)


# ---------------------------------------------------------------------------
# Public function: configure_structlog
# ---------------------------------------------------------------------------


def configure_structlog(
    log_level: str = "INFO",
    log_format: str = "json",
) -> None:
    """Configure the structlog processor chain and bridge stdlib logging.

    This MUST be the very first observability call in
    :func:`app.create_app` so that every subsequent log line emitted
    during startup is JSON-formatted and carries the project's
    canonical context fields.

    Processor chain (production, ``log_format="json"``):

    1.  ``filter_by_level``      Drop records below the configured level.
    2.  ``merge_contextvars``    Surface bound contextvars
        (``correlation_id``, ``user_id``, ``org_id``).
    3.  ``add_logger_name``      Add ``logger`` name field.
    4.  ``add_log_level``        Add ``level`` field.
    5.  ``TimeStamper``          ISO-8601 UTC timestamp.
    6.  ``add_stdlib_record_extras``  Promote stdlib LogRecord
        ``extra={...}`` kwargs into the event dict (no-op for native
        structlog records; critical for stdlib-bridged records).
    7.  ``add_open_telemetry_context``  Add ``trace_id``/``span_id`` when
        a span is active.
    8.  ``StackInfoRenderer``    Format stack info if requested.
    9.  ``format_exc_info``      Render traceback to ``exception`` field.
    10. ``UnicodeDecoder``       Decode any bytes in event_dict.
    11. ``redact_secrets_processor``  Replace secret-named values.
    12. ``ProcessorFormatter.wrap_for_formatter``  Hand off to the
        stdlib ``ProcessorFormatter`` so the final renderer is applied
        exactly once for BOTH native structlog records and foreign
        stdlib records.

    The final renderer (:class:`structlog.processors.JSONRenderer` for
    production, :class:`structlog.dev.ConsoleRenderer` for development
    with ANSI colours when ``sys.stdout`` is a TTY) is configured on
    the stdlib :class:`structlog.stdlib.ProcessorFormatter` (see
    :func:`_configure_stdlib_logging`).

    Args:
        log_level: One of ``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
            ``"ERROR"``, ``"CRITICAL"``. Falls back to ``"INFO"`` for
            unrecognized values. Case-insensitive.
        log_format: ``"json"`` (production default) or ``"console"``
            (human-readable development output). Case-insensitive; any
            value other than ``"console"`` is treated as ``"json"``.

    Returns:
        None. Side effects: configures structlog globally, mutates the
        stdlib root logger handlers, sets per-logger levels for known
        chatty libraries, and emits a single ``"structlog_configured"``
        log line at INFO so operators can confirm the configuration in
        process logs.

    Idempotent: safe to call multiple times in the same process (e.g.,
    between pytest sessions). The second and subsequent calls remove
    existing root-logger handlers before re-attaching, ensuring no
    duplicate output.
    """
    global _initialized  # noqa: PLW0603 - explicit idempotency guard, intentional

    if not _STRUCTLOG_AVAILABLE:
        # Best-effort: configure stdlib logging with a basic format
        # and bail out. ``force=True`` overrides any prior basicConfig
        # invocation that some imported library may have made.
        stdlib_logging.basicConfig(
            level=_resolve_log_level(log_level),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stdout,
            force=True,
        )
        return

    log_level_int = _resolve_log_level(log_level)
    processors = _build_processor_chain(log_format, log_level_int)

    # The "shared pre-chain" is the subset of processors applied to
    # BOTH native structlog records AND foreign stdlib records. We
    # exclude the final renderer because the ProcessorFormatter
    # applies its own renderer on top. We also exclude
    # ``filter_by_level`` because foreign records are filtered by the
    # stdlib handler's own level setting.
    shared_pre_chain: list[Processor] = [
        merge_contextvars,
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True),
        # Promote stdlib ``LogRecord`` ``extra={...}`` kwargs into the
        # event dict for foreign (stdlib-bridged) records. Without this
        # processor, every ``_logger.info("event", extra={...})`` call
        # site would have its diagnostic context silently dropped from
        # the JSON output. See ``add_stdlib_record_extras`` docstring.
        # Placed BEFORE ``redact_secrets_processor`` so any
        # secret-named ``extra`` keys (e.g., ``api_key``) are redacted
        # before rendering.
        add_stdlib_record_extras,
        add_open_telemetry_context,
        StackInfoRenderer(),
        format_exc_info,
        UnicodeDecoder(),
        redact_secrets_processor,
    ]

    structlog.configure(
        processors=processors,
        # ``make_filtering_bound_logger`` filters at the wrapper level
        # so we never pay to build event dicts for filtered-out
        # records. This is the modern preferred replacement for
        # invoking ``filter_by_level`` early in the chain.
        wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Cache the bound logger on first use to dramatically speed up
        # ``structlog.get_logger(name)`` calls on hot paths.
        cache_logger_on_first_use=True,
    )

    _configure_stdlib_logging(shared_pre_chain, log_level_int, log_format)

    _initialized = True

    # Emit a single configuration-confirmation line so operators can
    # verify the configuration in process logs. Using a freshly-bound
    # logger (rather than a cached module-level one) ensures the new
    # processor chain is applied even on a re-configure call from
    # tests.
    confirmation_logger: BoundLogger = structlog.get_logger("app.observability.logging")
    confirmation_logger.info(
        "structlog_configured",
        log_level=log_level.upper() if log_level else "INFO",
        log_format=log_format.lower(),
        redaction_active=True,
    )
