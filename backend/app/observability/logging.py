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

- ``*_key``        (e.g., ``api_key``, ``signing_key``, ``ANTHROPIC_API_KEY``)
- ``*_secret``     (e.g., ``client_secret``)
- ``password``     (case-insensitive)
- ``token``        (case-insensitive; ``access_token``, ``refresh_token``)
- ``authorization`` (HTTP header naming convention)

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
# ``re.fullmatch`` (whole-string match) and are intentionally permissive
# (false-positive redaction is far less harmful than a false-negative
# secret leak). They cover the OWASP-recommended common naming
# patterns: ``password``, ``authorization``, ``token``, ``*_token``
# (e.g., ``access_token``, ``refresh_token``, ``id_token``), ``*_key``
# (e.g., ``api_key``, ``ANTHROPIC_API_KEY``, ``signing_key``),
# ``*_secret`` (e.g., ``client_secret``), and any name containing
# ``api_key``, ``api-key``, or ``apikey`` (e.g., ``stripe_api_key``,
# ``my-api-key``, ``my_apikey``).
_SECRET_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:"
    r"password"
    r"|authorization"
    r"|token"
    r"|.*_token"
    r"|.*_key"
    r"|.*_secret"
    r"|.*api[_-]?key.*"
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
    "configure_structlog",
    "redact_secrets_processor",
]


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

    Per AAP Section 0.7.4, keys matching any of the following patterns
    are redacted (case-insensitive):

    - ``password``
    - ``authorization``
    - ``token`` and ``*_token``
    - ``*_key``  (e.g., ``api_key``, ``ANTHROPIC_API_KEY``)
    - ``*_secret``
    - any key containing ``api_key``, ``api-key``, ``apikey``

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

    1. ``filter_by_level``      Drop records below the configured level.
    2. ``merge_contextvars``    Surface bound contextvars
       (``correlation_id``, ``user_id``, ``org_id``).
    3. ``add_logger_name``      Add ``logger`` name field.
    4. ``add_log_level``        Add ``level`` field.
    5. ``TimeStamper``          ISO-8601 UTC timestamp.
    6. ``add_open_telemetry_context``  Add ``trace_id``/``span_id`` when
       a span is active.
    7. ``StackInfoRenderer``    Format stack info if requested.
    8. ``format_exc_info``      Render traceback to ``exception`` field.
    9. ``UnicodeDecoder``       Decode any bytes in event_dict.
    10. ``redact_secrets_processor``  Replace secret-named values.
    11. ``ProcessorFormatter.wrap_for_formatter``  Hand off to the
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
