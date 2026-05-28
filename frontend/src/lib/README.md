# frontend/src/lib

## 1. Purpose

`frontend/src/lib` hosts the cross-cutting browser utilities shared across the Sales-Connections SPA: a per-page-load correlation ID generator (`correlationId.ts`), the singleton TanStack Query 5.62.16 client (`queryClient.ts`), and a legacy `localStorage`-backed connection/tag store (`localStore.ts`). The correlation ID utility is the F-002-relevant primitive because it enables `X-Correlation-Id` end-to-end propagation from browser → Flask middleware → structlog → CloudWatch Logs. The TanStack Query singleton hosts the cache used by every TanStack Query hook in the SPA, including `useGenerateNotesMutation`. The legacy `localStore.ts` is preserved for backward compatibility and is not part of the AI-notes flow.

**Path note**: The AI-notes mutation hook and HTTP client wrapper are implemented at [`frontend/src/api/notes.ts`](../api/notes.ts) and [`frontend/src/api/client.ts`](../api/client.ts) — not in this folder. This README documents the cross-cutting browser utilities this folder hosts AND cross-references the API client as the AI-notes integration surface per DL-0061 in [`docs/decision-log.md`](../../../docs/decision-log.md).

## 2. Business Context

> "Sales-Connections is expected to be used during weekly sales pipeline review meetings. Sales leaders will add connection ideas before or during the meeting, and SDRs will use the generated notes as 'meeting-ready context' to decide which warm leads to pursue that week."

> The AI-generated note is not just convenience text; it is a prioritization aid that helps the team quickly answer:
>
> - Why is this person worth contacting now?
> - How should the submitter be referenced?
> - Should the submitter make a warm intro, be mentioned softly, or stay uninvolved?
> - What first outbound angle should an SDR use?

In business-value terms, the correlation ID utility documented here is what links each click of the "Generate AI Notes" button to its backend processing trace — making latency analysis, debugging, and incident response possible across the SPA → API → AI provider chain that delivers those meeting-ready prioritization aids.

## 3. Key Files

| File | Purpose |
|------|---------|
| `correlationId.ts` | `getCorrelationId()`, `resetCorrelationId()` — lazily mints `sc-fe-<uuid>` per page-load via `crypto.randomUUID()`; module-level cache |
| `queryClient.ts` | TanStack Query 5.62.16 singleton; defaults (60s staleTime, 5min gcTime, refetchOnWindowFocus=false), custom retry predicate excluding 4xx |
| `localStore.ts` | Legacy localStorage helpers for offline-style connection/tag persistence (kept for backward compatibility; not used by the AI-notes flow) |

### Cross-reference — AI-notes API surface (NOT in this folder)

The mutation hook and the HTTP transport that backs it live in `frontend/src/api/`, not in this folder. See DL-0061 in [`docs/decision-log.md`](../../../docs/decision-log.md) for the rationale.

- [`frontend/src/api/notes.ts:L233`](../api/notes.ts) — `useGenerateNotesMutation` (TanStack Query mutation)
- [`frontend/src/api/client.ts:L572`](../api/client.ts) — `apiPost` (the HTTP wrapper)

The correlation ID minted by `getCorrelationId()` is attached to every AI-notes call by `apiPost` via the `X-Correlation-Id` header (see [`frontend/src/api/client.ts:L1-L54`](../api/client.ts) header docstring).

## 4. Data Flow

Diagram D6 below shows how the correlation ID flows from the browser through the SPA's HTTP wrapper to the Flask backend's structlog context and into CloudWatch Logs.

```mermaid
%% Diagram: F-002 Correlation ID Lifecycle — sc-fe Prefix From Browser to Backend Logs
sequenceDiagram
    participant Browser as "User browser"
    participant Corr as "lib/correlationId.ts"
    participant Client as "api/client.ts (apiPost)"
    participant Route as "Flask /api/notes/generate"
    participant Mid as "Flask correlation middleware"
    participant Slog as "structlog context"
    participant CW as "CloudWatch Logs"

    Browser->>Corr: "First call to getCorrelationId()"
    Corr->>Corr: "Mint sc-fe-<uuid> via crypto.randomUUID()"
    Corr-->>Browser: "Return cached ID"
    Note over Corr: "Module-level cache; reset on logout via resetCorrelationId()"
    Browser->>Client: "POST /api/notes/generate"
    Client->>Route: "X-Correlation-Id: sc-fe-..."
    Route->>Mid: "Extract header"
    Mid->>Slog: "bind(correlation_id=...)"
    Slog-->>CW: "Every log line carries correlation_id"
```

**Legend**:
- `->>` solid arrow: synchronous call between layers
- `-->>` dashed arrow: return value
- The `sc-fe-` prefix is configured via `VITE_CORRELATION_ID_PREFIX` env var

Diagram D6 shows the end-to-end correlation ID flow. The browser-side `getCorrelationId()` mints the ID once per page-load. The HTTP wrapper attaches it as the `X-Correlation-Id` header. The Flask correlation middleware binds it to structlog context, propagating it to every log line for that request — enabling a single grep against CloudWatch Logs to retrieve the full trace.

## 5. Public Interfaces

- **`getCorrelationId(): string`** at [`frontend/src/lib/correlationId.ts:L101`](./correlationId.ts) — returns the cached `sc-fe-*` ID; lazily mints on first call via `crypto.randomUUID()`.
- **`resetCorrelationId(): string`** at [`frontend/src/lib/correlationId.ts:L121`](./correlationId.ts) — forces generation of a new ID; called on login success and logout (see `frontend/src/auth/AuthProvider.tsx`).
- **`queryClient: QueryClient`** singleton at [`frontend/src/lib/queryClient.ts:L177`](./queryClient.ts) — TanStack Query 5.62.16 client with project defaults; consumed by `<QueryClientProvider>` in `frontend/src/main.tsx` and `.clear()`-ed on logout in `frontend/src/auth/AuthProvider.tsx`.
- **Legacy `localStore` exports** at [`frontend/src/lib/localStore.ts`](./localStore.ts) — `listConnections`, `getConnection`, `createConnection`, `updateConnection`, etc. (not used by AI-notes flow; preserved for backward compatibility).

## 6. Error Handling

- `queryClient.ts` retry predicate (see `shouldRetryQuery` at [`frontend/src/lib/queryClient.ts:L142`](./queryClient.ts)): skip retries on 4xx — validation, auth, and RBAC errors should NOT be retried.
- `client.ts` 401 redirect behavior: on a 401 response, the wrapper redirects to `/login?next=<encoded-pathname>` (HttpOnly cookie expired); cross-link to [`frontend/src/api/client.ts:L182`](../api/client.ts) `ApiError` for the error shape carrying `status`, `code`, `message`, `correlationId`, `fields`.
- `getCorrelationId()` does NOT throw on missing Web Crypto — modern browsers (per browserslist in `frontend/package.json`) always have it; crashing loudly is preferred over silent fallback to weaker entropy.

## 7. Security and Privacy Notes

- Correlation IDs are **non-secret session-scoped UUIDs** (no PII; format: `sc-fe-<uuid-v4>`).
- HttpOnly session cookie is included on every request via `credentials: "include"` — JavaScript code CANNOT read the cookie via `document.cookie`.
- The correlation ID lives in **module-level memory only** (not localStorage, not sessionStorage, not cookies) — it resets naturally on hard reload and on logical session boundaries (login/logout).
- `localStore.ts` data is local-only and never transmitted (offline-style emulation layer).
- See [`docs/security.md`](../../../docs/security.md) for the full security model.

## 8. Operational Notes

- End-to-end correlation across SPA → Flask middleware → CloudWatch Logs uses the `sc-fe-` prefix; the backend's `correlation_id_prefix=sc-be-` differentiates server-minted IDs (e.g., for cron jobs without a frontend caller).
- Each log line in CloudWatch includes the same `correlation_id` field, enabling traces with a single grep.
- **Reused observability stack** per AAP § 0.11.4 (Observability rule) — no new wiring added by F-002 documentation work; the existing structlog/Prometheus/OpenTelemetry surfaces handle correlation transparently.
- Local-dev verification: `curl -i -H "X-Correlation-Id: sc-fe-test" http://localhost:8000/healthz` echoes the correlation ID in the response and logs.

## 9. Examples

**Example 1 — Reading the correlation ID**:

```ts
import { getCorrelationId } from "@/lib/correlationId";
const id = getCorrelationId();  // "sc-fe-3f50bd51-2d0a-4b3a-9f4f-1e25c6fdc5cf"
```

**Example 2 — Using the singleton query client**:

```ts
import { queryClient } from "@/lib/queryClient";
queryClient.invalidateQueries({ queryKey: ["connections"] });
```

## 10. Related Modules

- [`frontend/src/api/notes.ts`](../api/notes.ts) — `useGenerateNotesMutation` (per DL-0061, the actual AI-notes API surface).
- [`frontend/src/api/client.ts`](../api/client.ts) — `apiPost`, `ApiError` (per DL-0061, the shared HTTP transport).
- [`frontend/src/components/README.md`](../components/README.md) — design-system primitives the form composes.
- [`backend/app/middleware/correlation.py`](../../../backend/app/middleware/correlation.py) — server-side correlation extraction.
- [`docs/ai-note-generation-workflow.md`](../../../docs/ai-note-generation-workflow.md) § 8 — observability surfaces deep-dive.
- [`docs/decision-log.md`](../../../docs/decision-log.md) — DL-0061 (README placement deviation rationale).
