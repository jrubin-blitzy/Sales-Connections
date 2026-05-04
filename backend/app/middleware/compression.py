"""HTTP response body compression middleware.

This module wires the third-party ``flask-compress`` extension into the
Sales-Connections application factory via a ``register_compression_middleware``
function that mirrors the convention established by the other middleware
modules in :mod:`app.middleware` (``correlation``, ``cors``,
``security_headers``, ``auth``, ``rbac``, ``error_handlers``).

Why this middleware exists
==========================

Per AAP Section 0.4.6, the production deployment topology routes
``/api/*`` and ``/auth/*`` directly from the AWS Application Load
Balancer to the Flask backend running on ECS Fargate; nginx is NOT
interposed on those paths. The frontend nginx server (which gzips
SPA static assets via the ``gzip on`` block in
``frontend/nginx.conf`` lines 58-68) handles ONLY the SPA static-
asset path served by the separate frontend container. Without this
middleware, production backend responses ship uncompressed over the
public Internet, and the QA Checkpoint 9 review flagged this as the
only remaining MINOR-severity gap (see ``docs/decision-log.md`` row
``DL-0046``). At the 10K-record scale ceiling the default feed
endpoint emits roughly 19 KB of JSON per page; gzip typically
reduces that body by 70-90 percent, materially improving perceived
performance for SPA users on slow networks while keeping the AAP
Section 0.4.6 deployment topology unchanged (no ALB attribute flip,
no per-environment Terraform reshape).

Algorithm selection: gzip and deflate only
==========================================

The ``COMPRESS_ALGORITHM`` configuration is fixed at
``["gzip", "deflate"]``. The other two algorithms supported by
``flask-compress`` (``br`` brotli, ``zstd`` zstandard) are
deliberately NOT enabled even though their codec wheels are pinned
in ``backend/requirements.txt``:

* gzip is universally supported by every browser, every HTTP client
  library shipped in the last fifteen years, every CDN, and every
  reverse proxy. The QA Checkpoint 9 reproduction step uses
  ``Accept-Encoding: gzip`` as its single algorithm; the AAP
  Section 0.4.6 deployment topology does not require richer
  negotiation than gzip on the backend hop.
* deflate is essentially gzip without the gzip framing header;
  including it as a fallback adds a few bytes of compatibility for
  legacy clients with no operational cost.
* brotli (``br``) yields better ratios than gzip on JSON payloads
  but the wire savings on API responses (typically already
  small-to-medium JSON) are marginal once gzip is engaged. Browser
  support is universal in Chrome and Firefox but the algorithm is
  more CPU-intensive and could push the form-submit P95 over the
  AAP Section 0.7.3 budget under sustained load. The frontend
  nginx config does NOT enable brotli either (``frontend/nginx.conf``
  uses only ``gzip``), so leaving brotli disabled keeps the wire
  behavior consistent across the SPA static-asset path and the
  backend JSON path.
* zstd compression is excellent and fast but browser support is
  still emerging (Chrome 123+, Firefox 126+ as of mid-2024) and
  not all corporate proxies handle it gracefully. Enabling zstd
  before mass-market support is mature would risk a small fraction
  of clients receiving bodies they cannot decode. zstd is
  intentionally deferred until either browser baseline catches up
  or a documented performance need surfaces.

The brotli and backports.zstd codec wheels are still installed
because ``flask-compress`` imports them unconditionally at module
load time (see the ``try: import brotlicffi as brotli`` block at
the top of ``flask_compress.flask_compress`` and the
``from backports import zstd`` import in
``flask_compress.compat``). Restricting ``COMPRESS_ALGORITHM`` to
gzip and deflate keeps the wire behavior simple even though the
codecs sit unused in the worker process.

Compression thresholds
======================

* ``COMPRESS_MIN_SIZE = 500`` (bytes). Bodies smaller than 500 bytes
  are NOT compressed because the gzip framing overhead approaches
  the savings on tiny payloads. The figure mirrors the
  ``flask-compress`` default and aligns with the
  ``frontend/nginx.conf`` ``gzip_min_length 1024`` order of
  magnitude (the frontend serves larger HTML/JS/CSS bundles, so a
  larger threshold there is appropriate; the backend serves smaller
  JSON snippets, so a smaller threshold catches more bodies).
* ``COMPRESS_LEVEL = 6``. The standard gzip default; balances
  compression ratio against CPU cost. Most JSON payloads see 70-90
  percent reduction at level 6.
* ``COMPRESS_MIMETYPES`` retains the ``flask-compress`` library
  default which already includes ``application/json``,
  ``application/vnd.api+json``, ``application/manifest+json``,
  ``application/xml``, plus the various text/* and font/* types.
  All of those are appropriate to compress; binary types
  (``image/png``, ``application/pdf``, etc.) are absent from the
  list and therefore left uncompressed.

Wiring order
============

The middleware is registered FIRST in ``app/__init__.py``, BEFORE
``register_correlation_middleware``. Per the empirical Flask
contract verified in ``backend/tests/middleware/test_compression.py``,
``after_request`` hooks fire in REVERSE registration order (LIFO),
so registering compression first means it executes LAST in the
after_request chain, after every other hook has finished modifying
headers. The execution order is::

    1. security_headers_after_request   (X-Content-Type-Options,
                                         X-Frame-Options, Referrer-
                                         Policy, Permissions-Policy,
                                         Cache-Control on /api/auth)
    2. cors_after_request                (Access-Control-Allow-Origin
                                         and -Allow-Credentials for
                                         allowlisted origins)
    3. correlation_after_request         (echo X-Correlation-Id back
                                         to the caller)
    4. compression_after_request         (this module; reads the
                                         final body + headers and
                                         emits Vary plus
                                         Content-Encoding plus the
                                         gzipped body)

Running compression LAST is correct for two reasons:

* The Flask-Compress ``after_request`` hook reads the final response
  body and Content-Length, computes the compressed body, then
  rewrites those values plus the Vary and Content-Encoding headers.
  Any other after_request hook that runs AFTER compression would see
  the compressed body, which is opaque, and would have no useful
  way to interact with it. Running upstream hooks first lets them
  finish all header attachment (correlation, CORS, security headers)
  before compression takes the body off the wire.
* If a future after_request hook is introduced that explicitly
  needs to operate on the uncompressed body (for example, an
  auditing interceptor that hashes the response), it can be
  registered AFTER ``register_compression_middleware`` so it
  executes BEFORE compression in the LIFO chain.

Idempotency
===========

The Flask-Compress hook is itself idempotent against pre-compressed
responses: it short-circuits the compression branch when
``response.headers.get("Content-Encoding")`` is already populated
(see the ``or "Content-Encoding" in response.headers`` clause inside
``flask_compress.Compress.after_request``). A future endpoint that
returns a pre-gzipped body and sets ``Content-Encoding`` itself is
preserved verbatim; this matches the convention used by the
``security_headers`` and ``correlation`` middlewares.

What the middleware does NOT compress
=====================================

Per the Flask-Compress library's built-in conditions, the following
responses are left uncompressed even when registered:

* Status codes outside ``200 <= status < 300`` (4xx and 5xx error
  envelopes are left uncompressed; their bodies are short and
  compression overhead would dominate any savings).
* Mimetypes outside ``COMPRESS_MIMETYPES`` (binary types like
  ``image/png`` are left untouched).
* Bodies smaller than ``COMPRESS_MIN_SIZE`` (less than 500 bytes).
* Responses that already carry a ``Content-Encoding`` header (no
  double-compression).
* Streaming responses when ``COMPRESS_STREAMS`` is False (we keep
  the library default ``True`` so the streaming endpoints
  registered for static-asset delivery, none in MVP, would work
  out of the box if added later).

Browser caching contract
========================

Flask-Compress automatically appends ``Accept-Encoding`` to the
``Vary`` response header so caches do not serve a gzipped response
to a client that did not request gzip. This is RFC 9110 compliant
and required for correct CDN behavior; the contract is exercised by
``backend/tests/middleware/test_compression.py::TestVaryHeader``.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` is imported under the alias ``_stdlib_logging`` to
# match the convention established by ``app/middleware/cors.py`` and
# ``app/middleware/security_headers.py`` and to avoid the namespace
# conflict with ``app.observability.logging`` (the project's
# structlog configuration module). The alias makes call sites
# explicit: this is the stdlib logger, not structlog.
import logging as _stdlib_logging
from typing import TYPE_CHECKING, Final

# Third-party runtime imports.
#
# Flask-Compress is the canonical extension for response-body
# compression in the Flask ecosystem. It registers a single
# ``after_request`` hook on the Flask app that inspects the response
# Content-Type, the request Accept-Encoding header, and the response
# body length, then conditionally compresses the body and attaches
# the Content-Encoding plus Vary headers.
from flask_compress import Compress

# Type-only imports gated under ``TYPE_CHECKING`` so the runtime
# surface stays minimal. ``Flask`` is the application instance type
# accepted by :func:`register_compression_middleware`.
if TYPE_CHECKING:
    from flask import Flask

# Module-level stdlib logger used only at registration time. Per-
# request logging is intentionally absent because the hook fires on
# every response and per-request logging would generate meaningless
# noise; correlation_id binding is preserved through the correlation
# middleware's contextvars in any case.
_logger: Final[_stdlib_logging.Logger] = _stdlib_logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------
__all__ = [
    "register_compression_middleware",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Compression algorithms enabled at the application layer. gzip is the
# universal baseline; deflate is included as a legacy-client fallback.
# br (brotli) and zstd are deliberately omitted; see the module
# docstring section "Algorithm selection" for the full rationale.
# Stored as a tuple to signal immutability at the type level; the
# value is converted to a list before assignment to
# ``app.config["COMPRESS_ALGORITHM"]`` because Flask-Compress stores
# the configuration value verbatim and tuple identity is not
# meaningful here.
_COMPRESS_ALGORITHM: Final[tuple[str, ...]] = ("gzip", "deflate")

# Body-length threshold below which compression is skipped. Bodies
# smaller than this size do not benefit from compression because the
# gzip header and trailer overhead approach the byte savings.
# 500 bytes mirrors the Flask-Compress default and is sized for the
# JSON payload shapes emitted by this backend (the smallest payloads
# are HTTP 401/403 envelopes around 200 bytes; the smallest
# legitimately-compressible payloads are single-record reads around
# 700-900 bytes).
_COMPRESS_MIN_SIZE: Final[int] = 500

# Compression level for gzip and deflate. Level 6 is the canonical
# zlib default and provides a good ratio/CPU tradeoff for typical
# JSON payloads (most observed bodies see 70-90 percent reduction).
# Level 9 (maximum compression) is rejected because the marginal
# savings on JSON are small while CPU cost grows materially; level 1
# (fastest compression) is rejected because the time saved per
# request is below the noise floor of network latency on a real-
# world client.
_COMPRESS_LEVEL: Final[int] = 6


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_compression_middleware(app: Flask) -> None:
    """Register the Flask-Compress ``after_request`` hook on the app.

    Configures the Flask-Compress extension with project-specific
    defaults (algorithms, level, min-size) and binds it to the Flask
    application via ``Compress(app)``. Flask-Compress installs
    exactly one Flask hook:

        * ``after_request`` -> ``Compress.after_request``

    Per AAP Section 0.5.2 Layer 0, this MUST be registered FIRST in
    the middleware sequence (BEFORE
    ``register_correlation_middleware``) so that the after_request
    chain runs in the correct order. ``after_request`` hooks fire in
    REVERSE registration order (LIFO) per the empirical Flask
    contract verified in
    ``backend/tests/middleware/test_compression.py``; registering
    compression first means it executes LAST in the chain, after
    every other hook has finished attaching headers and the response
    body is final. The canonical sequence is::

        compression -> correlation -> cors -> security_headers ->
        auth -> rbac -> error_handlers

    Configuration applied (each value uses ``setdefault`` semantics
    so a higher-priority override in ``app.config`` is preserved
    verbatim, matching the convention used by Flask-Compress
    itself):

        * ``COMPRESS_ALGORITHM`` = ``["gzip", "deflate"]`` -- restrict
          to widely-supported algorithms; brotli and zstd are
          deliberately disabled (see module docstring "Algorithm
          selection").
        * ``COMPRESS_MIN_SIZE`` = ``500`` (bytes) -- skip compression
          for tiny payloads where header overhead dominates.
        * ``COMPRESS_LEVEL`` = ``6`` -- canonical zlib default;
          ratio/CPU tradeoff suitable for typical JSON payloads.

    The Flask-Compress library's other defaults are inherited as-is:

        * ``COMPRESS_MIMETYPES`` -- the library default already
          includes ``application/json`` and the various text/*
          types relevant to this backend.
        * ``COMPRESS_REGISTER`` = ``True`` -- triggers the
          ``app.after_request`` registration. We rely on this rather
          than calling ``app.after_request`` ourselves because
          Flask-Compress encapsulates the conditional-compression
          logic internally.
        * ``COMPRESS_STREAMS`` = ``True`` -- preserve streaming
          response support for any future endpoint that uses it.

    Idempotent against double-registration: Flask-Compress short-
    circuits the compression branch when ``Content-Encoding`` is
    already set on the response, and a second ``Compress(app)``
    call would re-register the same after_request function which
    Flask deduplicates by function identity.

    Args:
        app: The Flask application instance produced by
            :func:`app.create_app`. Configuration is written to
            ``app.config`` and the after_request hook is installed
            on the application-wide function list, so it fires for
            every blueprint and every request (including the health
            probes and the metrics scrape, both of which are
            uncompressed by virtue of returning small payloads
            and/or non-compressible content types).

    Returns:
        None. Side effects:

            * Mutates ``app.config`` to install the
              ``COMPRESS_ALGORITHM``, ``COMPRESS_MIN_SIZE``, and
              ``COMPRESS_LEVEL`` defaults (each via
              ``setdefault`` so a developer-supplied override is
              preserved).
            * Mutates ``app.after_request_funcs`` to include the
              Flask-Compress ``after_request`` hook.
            * Emits a single ``compression_middleware_registered``
              info log line via the stdlib logger so operators can
              confirm wiring at startup. The log line includes the
              effective algorithms list, the minimum size threshold,
              and the compression level.
    """
    # Apply project-specific defaults before binding the extension.
    # Flask-Compress's ``init_app`` reads these via
    # ``app.config["..."]`` after ``app.config.setdefault`` writes
    # would have otherwise been overwritten by the library's own
    # defaults. Using ``setdefault`` here preserves any value an
    # operator sets via environment variable (Flask config classes
    # in this project read environment-driven overrides via
    # ``BaseConfig`` subclasses; an operator who wants brotli can
    # set ``COMPRESS_ALGORITHM = "br,gzip,deflate"`` in a future
    # config class without modifying this module).
    app.config.setdefault("COMPRESS_ALGORITHM", list(_COMPRESS_ALGORITHM))
    app.config.setdefault("COMPRESS_MIN_SIZE", _COMPRESS_MIN_SIZE)
    app.config.setdefault("COMPRESS_LEVEL", _COMPRESS_LEVEL)

    # Bind the extension to the app. ``Compress(app)`` calls
    # ``Compress.init_app(app)`` which (a) populates the remaining
    # ``COMPRESS_*`` defaults via the library's own ``setdefault``
    # calls and (b) registers the ``after_request`` hook on the
    # application-wide function list.
    Compress(app)

    _logger.info(
        "compression_middleware_registered",
        extra={
            "algorithms": list(_COMPRESS_ALGORITHM),
            "min_size_bytes": _COMPRESS_MIN_SIZE,
            "level": _COMPRESS_LEVEL,
        },
    )
