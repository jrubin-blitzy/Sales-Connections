/**
 * client.ts - Cross-cutting fetch wrapper for the Sales-Connections SPA.
 *
 * THIS FILE IS THE ONLY PLACE IN THE FRONTEND THAT INVOKES THE BROWSER
 * FETCH API. Every other module reaches HTTP through the apiGet, apiPost,
 * apiPatch, apiPut, apiDelete helpers exported here. Centralizing
 * fetch ensures uniform behavior:
 *
 *   - URL construction:        Absolute paths from VITE_API_BASE_URL
 *                              when configured; relative for the
 *                              same-origin Vite dev proxy when the
 *                              variable is empty or just "/".
 *   - Credentials:             `credentials: "include"` on EVERY request
 *                              so the HttpOnly session cookie is sent
 *                              and any Set-Cookie response is honored.
 *   - Correlation propagation: X-Correlation-Id header attached from
 *                              @/lib/correlationId on every request.
 *                              The backend's correlation middleware
 *                              reads this header and binds it to the
 *                              structlog context per AAP Sec 0.4.3.
 *   - Auth interception:       401 responses redirect the browser to
 *                              /login?next=<encoded-current-pathname>
 *                              so post-login the user lands back where
 *                              they started. Bypassed via the
 *                              `skipAuthRedirect: true` option for the
 *                              session-introspection endpoint
 *                              (GET /api/me) which needs to handle 401
 *                              as the unauthenticated state.
 *   - Error parsing:           The backend's uniform error envelope
 *                              { error: { code, message, correlation_id,
 *                                fields } } is parsed and rethrown as
 *                              typed ApiError instances.
 *
 * Per AAP Sec 0.7.7: "No direct fetch in the frontend. All HTTP traffic
 * goes through frontend/src/api/client.ts."
 *
 * Per AAP Sec 0.4.3: this file is the SINGLE point of fetch invocation.
 *
 * Per AAP Sec 0.7.4 (Security Invariants): the JWT lives in the
 * HttpOnly cookie set by the server's Set-Cookie header. The wrapper
 * does NOT read or set cookies via document.cookie.
 *
 * Per AAP Sec 0.5.3 (frontend feature components compose UI primitives):
 *   "Feature components do not call fetch directly."
 *
 * Coordination touchpoints:
 *   - @/lib/correlationId    -- getCorrelationId(): string
 *   - @/vite-env (ambient)   -- import.meta.env.VITE_API_BASE_URL: string
 *   - @/api/connections      -- Connection CRUD hooks (consumer)
 *   - @/api/notes            -- AI note generation (consumer)
 *   - @/api/auth             -- Authentication hooks; uses skipAuthRedirect
 *                               for GET /api/me (consumer)
 *   - @/api/admin            -- Admin endpoints (consumer)
 */

import { getCorrelationId } from "@/lib/correlationId";

// ---------------------------------------------------------------------------
// Module-level constants
// ---------------------------------------------------------------------------

/**
 * Base URL for the Flask backend. Resolved from
 * `import.meta.env.VITE_API_BASE_URL` (declared in vite-env.d.ts).
 *
 * Default value "" (empty string) lets the Vite dev proxy intercept
 * `/api/*` and `/auth/*` and forward them to the Flask backend at
 * http://localhost:5000 (configured in frontend/vite.config.ts). In
 * production the variable is set to the absolute HTTPS URL of the
 * ALB-fronted backend (e.g., "https://api.sales-connections.example.com").
 *
 * The base URL is JOINED with the path argument by `buildUrl`. When the
 * base URL is empty or just "/", the path is returned unchanged so the
 * dev proxy forwards same-origin requests transparently.
 */
const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || "";

/**
 * The login route that 401 responses redirect to. The current
 * pathname is preserved as the `next` query parameter so post-login
 * the user lands back where they started.
 */
const LOGIN_PATH: string = "/login";

/**
 * Reasonable safety bound on response body parsing. Defends against
 * accidentally streaming unbounded responses (e.g., a misconfigured
 * endpoint streaming GB of logs). The value is well above the largest
 * legitimate API response (paginated record list at max page_size=100
 * carries on the order of 100 KB; well under this 5 MB ceiling).
 *
 * Documented for future enforcement; current implementation trusts
 * `response.json()` to handle large bodies via stream backpressure
 * which is sufficient for the documented endpoint shapes per AAP
 * Sec 0.7.3 (10K records / org scale ceiling).
 */
const RESPONSE_BODY_MAX_BYTES: number = 5 * 1024 * 1024; // 5 MB

// Reference the constant once so the linter doesn't flag it as unused.
// The constant is documented for future enforcement (see JSDoc above).
void RESPONSE_BODY_MAX_BYTES;

// ---------------------------------------------------------------------------
// Error envelope types
// ---------------------------------------------------------------------------

/**
 * Per-field validation error entry within the error envelope.
 *
 * Pydantic's ValidationError is mapped by
 * `app.middleware.error_handlers.ValidationFailedError.from_pydantic`
 * into an array of these objects, one per failed field.
 */
export interface ApiErrorField {
  /** Field path (dot-notation; "_root" for body-level errors). */
  field: string;
  /** Machine-readable error code (e.g., "missing", "invalid_email"). */
  code: string;
  /** Optional human-readable message; may be omitted by the backend. */
  message?: string;
  /** The offending value, if applicable (e.g., for invalid_enum). */
  value?: unknown;
}

/**
 * The shape of the backend's uniform error envelope:
 *
 *   {
 *     "error": {
 *       "code": "validation_failed",
 *       "message": "Request body must be a JSON object.",
 *       "correlation_id": "sc-fe-...",
 *       "fields": [
 *         { "field": "email", "code": "missing" }
 *       ]
 *     }
 *   }
 *
 * Per AAP Sec 0.4.3, this envelope is uniform across every endpoint.
 */
interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    correlation_id?: string;
    fields?: ApiErrorField[];
  };
}

// ---------------------------------------------------------------------------
// ApiError class
// ---------------------------------------------------------------------------

/**
 * Typed error class thrown by all api/* helpers on non-2xx responses.
 *
 * Carries the parsed error envelope plus the HTTP status code so
 * consumers can branch on:
 *
 *   - `status`         HTTP status (401 for unauth, 403 for RBAC, 422
 *                      for validation, 504 for AI timeout, etc.)
 *   - `code`           Machine-readable backend error code
 *                      ("validation_failed", "forbidden", "ai_timeout").
 *   - `message`        Human-readable description.
 *   - `correlationId`  Backend's correlation ID (matches the SPA's
 *                      X-Correlation-Id request header) for log
 *                      correlation across browser, ALB, and backend.
 *   - `fields`         Per-field validation errors (for 422 responses).
 *
 * Consumers (TanStack Query mutation onError callbacks, form components)
 * use these fields to render appropriate UI feedback.
 *
 * The `cause` property is set to the underlying `Response` object (when
 * available) for advanced debugging; consumers normally don't access it.
 *
 * NOTE on instanceof: TypeScript's emitted class transpilation can break
 * the prototype chain in some configurations (especially when Error is
 * subclassed across realms). The explicit `Object.setPrototypeOf` call
 * in the constructor is a defensive measure ensuring `instanceof
 * ApiError` works reliably in production builds.
 */
export class ApiError extends Error {
  /** HTTP status code (e.g., 401, 422, 504; 0 for network errors). */
  public readonly status: number;
  /** Backend error code from the envelope (e.g., "ai_timeout"). */
  public readonly code: string;
  /** Backend's correlation ID; matches the request's X-Correlation-Id. */
  public readonly correlationId: string | undefined;
  /** Per-field validation errors (typically populated for 422 responses). */
  public readonly fields: ReadonlyArray<ApiErrorField>;

  constructor(
    status: number,
    code: string,
    message: string,
    options: {
      correlationId?: string;
      fields?: ApiErrorField[];
      cause?: unknown;
    } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = options.correlationId;
    this.fields = options.fields ?? [];
    if (options.cause !== undefined) {
      // Use the standard Error.cause field (ES2022).
      this.cause = options.cause;
    }
    // Restore prototype chain for instanceof checks across realms.
    Object.setPrototypeOf(this, ApiError.prototype);
  }

  /**
   * Find a per-field error entry by field path. Returns undefined if
   * no entry exists for the given field.
   *
   * Useful for form components rendering inline field-level errors:
   *   const emailError = error.fieldError("email");
   *   if (emailError) <span>{emailError.message ?? emailError.code}</span>
   *
   * @param field - The field path to look up (dot-notation).
   * @returns       The matching ApiErrorField, or undefined.
   */
  fieldError(field: string): ApiErrorField | undefined {
    return this.fields.find((f) => f.field === field);
  }
}

// ---------------------------------------------------------------------------
// Public request options
// ---------------------------------------------------------------------------

/**
 * Optional second argument to api{Get,Post,Patch,Put,Delete} that allows
 * per-call overrides of wrapper behavior.
 */
export interface ApiRequestOptions {
  /**
   * When true, do NOT auto-redirect to /login on 401 responses.
   *
   * Used for the session-introspection endpoint (GET /api/me) which
   * needs to handle 401 as "not yet logged in" without triggering an
   * infinite redirect loop on cold pages.
   *
   * Default: false (redirect on 401).
   */
  skipAuthRedirect?: boolean;

  /**
   * Optional AbortSignal for cancellable requests. TanStack Query 5.x
   * passes its own signal automatically when the consumer uses the v5
   * `queryFn: ({ signal }) => apiGet(path, { signal })` pattern. Manual
   * use is for advanced cases (e.g., debounced inputs canceling
   * in-flight requests).
   */
  signal?: AbortSignal;

  /**
   * Optional additional headers. Merged with the default headers
   * (Accept, Content-Type when applicable, X-Correlation-Id). Values
   * supplied here override the defaults if there is a key collision.
   */
  headers?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Combine `API_BASE_URL` with the given path. If the path is absolute
 * (starts with "http:" or "https:"), it is returned unchanged. If
 * API_BASE_URL is empty or just "/", the path is returned unchanged so
 * the Vite dev proxy can intercept same-origin requests.
 *
 * @param path - URL path or absolute URL.
 * @returns      Resolved URL ready for the fetch invocation.
 */
function buildUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  if (!API_BASE_URL || API_BASE_URL === "/") {
    return path;
  }
  // Strip trailing slash from base, ensure leading slash on path, join.
  const base = API_BASE_URL.replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${suffix}`;
}

/**
 * Map common HTTP statuses to default error codes when the body
 * doesn't carry the uniform envelope (e.g., HTML 502 from a
 * misconfigured proxy, plain-text 504 from ALB).
 *
 * @param status - HTTP status code.
 * @returns        Machine-readable error code suitable for ApiError.code.
 */
function defaultCodeForStatus(status: number): string {
  switch (status) {
    case 400:
      return "bad_request";
    case 401:
      return "auth_required";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 422:
      return "validation_failed";
    case 429:
      return "rate_limited";
    case 502:
      return "bad_gateway";
    case 503:
      return "service_unavailable";
    case 504:
      return "gateway_timeout";
    default:
      if (status >= 500) return "server_error";
      if (status >= 400) return "client_error";
      return "unknown_error";
  }
}

/**
 * Map common HTTP statuses to a default human-readable message used
 * when the response body does not carry the uniform error envelope.
 *
 * @param status - HTTP status code.
 * @returns        User-facing message suitable for ApiError.message.
 */
function defaultMessageForStatus(status: number): string {
  switch (status) {
    case 401:
      return "Authentication required.";
    case 403:
      return "You do not have permission to perform this action.";
    case 404:
      return "The requested resource was not found.";
    case 409:
      return "A conflict occurred while processing the request.";
    case 422:
      return "The request contains invalid data.";
    case 504:
      return "The server took too long to respond.";
    default:
      return `Request failed with status ${status}.`;
  }
}

/**
 * Parse the response body into the uniform error envelope. Returns
 * a default-shaped envelope if parsing fails (so consumers always get
 * a usable ApiError even on malformed responses).
 *
 * @param response - The non-2xx Response object.
 * @returns          The parsed error envelope (always a usable shape).
 */
async function parseErrorEnvelope(response: Response): Promise<ErrorEnvelope["error"]> {
  try {
    const body = (await response.json()) as unknown;
    if (
      body !== null &&
      typeof body === "object" &&
      "error" in body &&
      typeof (body as ErrorEnvelope).error === "object" &&
      (body as ErrorEnvelope).error !== null
    ) {
      const env = (body as ErrorEnvelope).error;
      return {
        code: typeof env.code === "string" ? env.code : "unknown_error",
        message: typeof env.message === "string" ? env.message : `HTTP ${response.status}`,
        correlation_id: typeof env.correlation_id === "string" ? env.correlation_id : undefined,
        fields: Array.isArray(env.fields) ? env.fields : [],
      };
    }
  } catch {
    // Body wasn't JSON, or JSON parse failed. Fall through to default.
  }
  // Default envelope for non-JSON / unenveloped errors (e.g., HTML
  // 502 from a misconfigured proxy, plain-text 504 from ALB).
  return {
    code: defaultCodeForStatus(response.status),
    message: defaultMessageForStatus(response.status),
    correlation_id: response.headers.get("X-Correlation-Id") ?? undefined,
    fields: [],
  };
}

/**
 * Navigate the browser to /login, preserving the current pathname and
 * query string as the `next` query parameter so post-login the user is
 * redirected back to where they started.
 *
 * Uses `window.location.replace` so the back button doesn't leave the
 * user stuck on a forbidden page.
 *
 * In a non-browser environment (SSR, tests without jsdom), this is a
 * no-op so calling code doesn't crash.
 */
function redirectToLogin(): void {
  if (typeof window === "undefined" || !window.location) {
    return;
  }
  // Avoid redirecting if we're already on the login page (no infinite loop).
  if (window.location.pathname === LOGIN_PATH) {
    return;
  }
  const current = window.location.pathname + window.location.search;
  const next = encodeURIComponent(current);
  window.location.replace(`${LOGIN_PATH}?next=${next}`);
}

// ---------------------------------------------------------------------------
// Core request function (the SINGLE fetch invocation site)
// ---------------------------------------------------------------------------

/**
 * Internal core: perform a fetch, parse the response, and throw
 * `ApiError` on non-2xx. Returns the parsed JSON body typed as `T`.
 *
 * Generic argument `T` is the expected response shape. Caller is
 * responsible for ensuring this type matches the actual server
 * response (no runtime validation in MVP - per AAP scope discipline).
 *
 * Special handling:
 *   - 204 No Content: returns `undefined as T`. Callers requesting
 *     `void` (e.g., `apiDelete<void>`) get correct typing.
 *   - 401 Unauthorized: redirects to /login (unless skipAuthRedirect)
 *     AND throws ApiError so the caller's promise chain breaks.
 *   - Other 4xx/5xx: parses error envelope, throws ApiError.
 *   - Network errors: throws ApiError with status=0, code="network_error".
 *   - AbortError: re-throws the original DOMException so TanStack
 *     Query distinguishes cancellation from genuine errors.
 *
 * @internal
 */
async function request<T>(
  method: "GET" | "POST" | "PATCH" | "DELETE" | "PUT",
  path: string,
  body: unknown | undefined,
  options: ApiRequestOptions = {},
): Promise<T> {
  const url = buildUrl(path);

  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Correlation-Id": getCorrelationId(),
    ...(options.headers ?? {}),
  };

  // Build RequestInit. Only set Content-Type when there's a JSON body;
  // GET / DELETE without body skips Content-Type so simple-CORS
  // requests don't trigger an unnecessary preflight.
  const init: RequestInit = {
    method,
    credentials: "include",
    headers,
    signal: options.signal,
  };

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response: Response;
  try {
    // THIS IS THE ONE AND ONLY fetch INVOCATION IN THE ENTIRE SPA.
    // See AAP Sec 0.7.7 ("No direct fetch in the frontend").
    response = await fetch(url, init);
  } catch (cause) {
    // Re-throw AbortError verbatim so TanStack Query / consumer
    // recognizes it as deliberate cancellation, not a logical error.
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    // Network error, CORS rejection, DNS failure, etc.
    throw new ApiError(
      0,
      "network_error",
      cause instanceof Error ? cause.message : "Network request failed.",
      { cause },
    );
  }

  // 204 No Content: no body to parse. Returning `undefined as T` lets
  // callers type the response as `void` for endpoints like the admin
  // hard-delete that intentionally have no body.
  if (response.status === 204) {
    return undefined as T;
  }

  // Non-2xx: parse error envelope and throw.
  if (!response.ok) {
    const envelope = await parseErrorEnvelope(response);

    // 401 handling: redirect by default, bypass only on opt-out.
    // The redirect runs BEFORE the throw so the navigation starts
    // immediately while the caller's promise chain breaks.
    if (response.status === 401 && !options.skipAuthRedirect) {
      redirectToLogin();
    }

    throw new ApiError(response.status, envelope.code, envelope.message, {
      correlationId: envelope.correlation_id,
      fields: envelope.fields,
      cause: response,
    });
  }

  // 2xx with JSON body. We trust `response.json()` to handle large
  // bodies via stream backpressure; the documented endpoint shapes
  // (max page_size=100 connection rows) stay well under 5 MB.
  try {
    return (await response.json()) as T;
  } catch (cause) {
    throw new ApiError(
      response.status,
      "invalid_response_body",
      "Server returned a non-JSON response body.",
      { cause },
    );
  }
}

// ---------------------------------------------------------------------------
// Public helpers (the SPA's HTTP entry points)
// ---------------------------------------------------------------------------

/**
 * Issue a GET request and return the parsed JSON body typed as `T`.
 *
 * Example:
 *   const records = await apiGet<PaginatedConnections>("/api/connections");
 *
 * @param path    - URL path (e.g., "/api/connections") or absolute URL.
 * @param options - Optional per-call overrides.
 * @returns         The parsed JSON response body typed as `T`.
 */
export function apiGet<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  return request<T>("GET", path, undefined, options);
}

/**
 * Issue a POST request with a JSON body. Returns the parsed JSON
 * response body typed as `TResp`. The body is typed as `TBody` for
 * compile-time safety on the call site.
 *
 * For endpoints with no request body (rare; logout uses {}), pass an
 * empty object `{}`. For endpoints with no response body, type
 * `TResp` as `void` and the helper returns undefined for 204.
 *
 * Example:
 *   const created = await apiPost<ConnectionRead, ConnectionCreate>(
 *     "/api/connections",
 *     payload,
 *   );
 *
 * @param path    - URL path.
 * @param body    - Request body (JSON-stringified internally).
 * @param options - Optional per-call overrides.
 * @returns         The parsed JSON response body typed as `TResp`.
 */
export function apiPost<TResp, TBody = unknown>(
  path: string,
  body: TBody,
  options?: ApiRequestOptions,
): Promise<TResp> {
  return request<TResp>("POST", path, body, options);
}

/**
 * Issue a PATCH request with a JSON body. Same shape as `apiPost`.
 *
 * @param path    - URL path.
 * @param body    - Request body (JSON-stringified internally).
 * @param options - Optional per-call overrides.
 * @returns         The parsed JSON response body typed as `TResp`.
 */
export function apiPatch<TResp, TBody = unknown>(
  path: string,
  body: TBody,
  options?: ApiRequestOptions,
): Promise<TResp> {
  return request<TResp>("PATCH", path, body, options);
}

/**
 * Issue a PUT request with a JSON body. Included for completeness;
 * the MVP backend does not currently use PUT endpoints (PATCH is
 * preferred for partial updates), but PUT support keeps the wrapper
 * future-proof.
 *
 * @param path    - URL path.
 * @param body    - Request body (JSON-stringified internally).
 * @param options - Optional per-call overrides.
 * @returns         The parsed JSON response body typed as `TResp`.
 */
export function apiPut<TResp, TBody = unknown>(
  path: string,
  body: TBody,
  options?: ApiRequestOptions,
): Promise<TResp> {
  return request<TResp>("PUT", path, body, options);
}

/**
 * Issue a DELETE request and return the parsed JSON body typed as `T`.
 *
 * For endpoints that return 204 No Content (e.g., admin hard delete),
 * type `T` as `void`; the helper returns `undefined as void`.
 *
 * For endpoints that return a body (e.g., the soft-delete endpoint
 * returning the deleted record), type `T` as the response shape.
 *
 * Example (204 No Content):
 *   await apiDelete<void>("/api/admin/records/abc");
 *
 * Example (response body):
 *   const deleted = await apiDelete<ConnectionRead>("/api/connections/xyz");
 *
 * @param path    - URL path.
 * @param options - Optional per-call overrides.
 * @returns         The parsed JSON response body typed as `T`,
 *                  or `undefined as T` for 204 responses.
 */
export function apiDelete<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  return request<T>("DELETE", path, undefined, options);
}
