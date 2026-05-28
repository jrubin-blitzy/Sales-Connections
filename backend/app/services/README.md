# backend/app/services

## 1. Purpose

The `backend/app/services` package is the business-logic layer that sits between the HTTP view layer (`backend/app/api/`) and the persistence and external-integration layers. It collects the canonical service entry points that own transaction boundaries, audit emission, authentication, admin workflows, AI orchestration, duplicate detection, and connection-record lifecycle behavior. The AI orchestrator (`ai_orchestration.py`) is the **sole AI integration boundary** for the backend — no other module in the codebase imports `anthropic` or `langchain_anthropic` (provider-replaceability invariant per AAP § 0.7.7); cite `[backend/app/services/ai_orchestration.py:L1-L57]`. The orchestrator owns three responsibilities: input sanitization at the AI boundary, a two-layer timeout watchdog (SDK timeout + `concurrent.futures` future cancellation), and outcome telemetry (Prometheus histogram plus structlog events). Sibling services in this package own auth, audit, admin, connection, and duplicate-detection responsibilities — they are listed for inventory in § 3 but are out of scope for this README's F-002 focus; cite `[backend/app/services/__init__.py:L184-L205]` for the package's complete public surface.

## 2. Business Context

The F-002 AI orchestration service exists because of the following business reality, captured verbatim from the user-supplied product framing in AAP § 0.2.3:

> "Sales-Connections is expected to be used during weekly sales pipeline review meetings. Sales leaders will add connection ideas before or during the meeting, and SDRs will use the generated notes as 'meeting-ready context' to decide which warm leads to pursue that week."

The AI-generated note is not just convenience text; it is a prioritization aid that helps the team quickly answer:

- Why is this person worth contacting now?
- How should the submitter be referenced?
- Should the submitter make a warm intro, be mentioned softly, or stay uninvolved?
- What first outbound angle should an SDR use?

Documentation of the AI workflow therefore explains the package in business-value terms:

- **Speed-to-action** — generated draft notes save SDRs from manually researching every warm lead before each weekly meeting.
- **Reduced ambiguity for SDRs** — every connection record arrives at the meeting with a consistent meeting-ready brief, regardless of which contributor entered it.
- **Preservation of relationship trust** — by surfacing soft warm-intro signals (e.g., "should the submitter be referenced?"), the system avoids cold outreach that could damage the contributor's network.
- **Faster conversion of leadership networks into outbound pipeline** — contributors can add ideas during the meeting and get AI-assisted context by the end of it.

## 3. Key Files

The inventory below lists every first-order child of the `backend/app/services` package. The AI orchestrator is the centerpiece of this README; sibling services are named for orientation only.

| File | Purpose | Status for F-002 |
|------|---------|------------------|
| `ai_orchestration.py` | AI orchestrator; sole AI integration boundary; sanitization, two-layer watchdog, telemetry | **Primary focus of this README** |
| `admin.py` | Admin service surface (users, records, moderation, analytics); enforces org scoping | Sibling — listed by name only |
| `audit.py` | Centralized append-only audit-event writer; exposes `emit_audit_event` and `AuditEmissionError` | Sibling — listed by name only |
| `auth.py` | Authentication service layer (password hashing/verification, JWT, OAuth upsert, audit events) | Sibling — listed by name only |
| `connections.py` | Connection/record domain service; canonical owner of state-changing CRUD | Sibling — listed by name only |
| `duplicate_detection.py` | LinkedIn URL duplicate-check service; soft-delete aware | Sibling — listed by name only |
| `__init__.py` | Package façade; re-exports public symbols via `__all__` (20 alphabetically-sorted names across all six service modules) | Reference |

This README's deep coverage targets `ai_orchestration.py` per the F-002 documentation scope (AAP § 0.4.1). Sibling services retain their own implementation-level documentation in their respective module docstrings. The AI orchestrator's module-level docstring at `[backend/app/services/ai_orchestration.py:L1-L57]` carries the comprehensive in-source rationale; the package façade's public surface is declared at `[backend/app/services/__init__.py:L184-L205]`.

## 4. Data Flow

Diagram D2 below traces the complete F-002 request lifecycle from the "Generate AI Notes" button click in the SPA through to the mutation resolution that populates the editable `ai_notes` textarea. The diagram highlights the two-layer watchdog mechanic in the service layer and the timeout-failure branch that exercises the graceful-degradation contract.

```mermaid
%% Diagram: F-002 Request Lifecycle — From Submit Click to Mutation Resolution
sequenceDiagram
    participant User as User
    participant Form as AddEditConnectionForm
    participant Hook as useGenerateNotesMutation
    participant Transport as apiPost (client.ts)
    participant MW as Middleware (correlation + RBAC)
    participant View as generate() in notes.py
    participant Orch as generate_outreach_notes()
    participant Watchdog as _invoke_with_timeout()
    participant Chat as _call_chat_anthropic()
    participant Anthropic as Anthropic API

    User->>Form: Click "Generate AI Notes"
    Form->>Hook: mutate({ relationship_context })
    Hook->>Transport: apiPost("/api/notes/generate", body)
    Transport->>MW: HTTP POST + X-Correlation-Id + HttpOnly cookie
    MW->>View: dispatch (Contributor/Admin)
    View->>View: NoteGenerationRequest.model_validate(...)
    View->>View: structlog.info("ai_note_generation_requested", user_id, org_id, context_chars)
    View->>Orch: generate_outreach_notes(request)
    Orch->>Orch: sanitize_for_ai_prompt(...)
    Orch->>Orch: structlog.info("ai_request_start")
    Orch->>Watchdog: _invoke_with_timeout(...)
    Watchdog->>Chat: _call_chat_anthropic(...) [submitted to ThreadPoolExecutor]
    Chat->>Anthropic: ChatAnthropic.invoke(prompt)
    Anthropic-->>Chat: BaseMessage(content=...)
    Chat-->>Watchdog: response text
    Watchdog-->>Orch: response text
    alt Watchdog cancellation (timeout)
        Watchdog->>Watchdog: future.cancel() after timeout_s + 0.5s grace
        Watchdog-->>Orch: raise FuturesTimeoutError
        Orch->>Orch: structlog.warning("ai_request_timeout", elapsed_seconds)
        Orch->>Orch: observe ai_request_duration_seconds{outcome="timeout"}
        Orch-->>View: raise AIServiceUnavailableError(code="ai_timeout", status_code=504)
        View-->>Transport: HTTP 504 + error envelope {code: "ai_timeout", correlation_id, ...}
        Transport-->>Hook: throw ApiError(504, "ai_timeout", ...)
        Hook-->>Form: onError(apiError)
        Form->>Form: isSoftAiFailure(error) -> true
        Form-->>User: Show non-blocking toast; ai_notes textarea remains editable; form submission NOT blocked
    end
    Orch->>Orch: observe ai_request_duration_seconds{outcome="success"}
    Orch->>Orch: structlog.info("ai_request_success", elapsed_seconds, response_chars)
    Orch-->>View: NoteGenerationResponse(ai_notes, model, generated_at)
    View-->>Transport: HTTP 200 + response.model_dump(mode="json")
    Transport-->>Hook: GenerateNotesResponse
    Hook-->>Form: onSuccess(response)
    Form->>Form: setFieldValue("ai_notes", response.ai_notes)
    Form-->>User: Editable textarea populated
```

**Legend (Diagram D2):**

- Solid arrow (`->>`) — synchronous request or call
- Dashed arrow (`-->>`) — synchronous response or awaited resolution
- Self-call arrow — internal helper invocation within the same participant
- Participant box — represents a role in the workflow (UI / hook / transport / middleware / view / service / external SDK / external API)
- `alt` block — alternative execution path; the timeout branch demonstrates the graceful-degradation contract
- All participant aliases match real symbol names: `AddEditConnectionForm`, `useGenerateNotesMutation`, `apiPost`, `generate`, `generate_outreach_notes`, `_invoke_with_timeout`, `_call_chat_anthropic`

## 5. Public Interfaces

The package exposes a deliberately minimal AI surface — exactly one entry-point function and one exception class. Both are listed in the AI orchestrator's `__all__` at `[backend/app/services/ai_orchestration.py:L246-L249]` and `generate_outreach_notes` is re-exported through the package façade at `[backend/app/services/__init__.py:L184-L205]`.

### Sole public AI API: `generate_outreach_notes`

```python
def generate_outreach_notes(
    request: NoteGenerationRequest,
    *,
    model: str | None = None,
) -> NoteGenerationResponse:
```

Cite `[backend/app/services/ai_orchestration.py:L478]`.

- **Args:**
  - `request: NoteGenerationRequest` — validated Pydantic request body containing `relationship_context` (1-4000 characters after `str_strip_whitespace`); cite `[backend/app/schemas/note_generation.py:L108]`.
  - `model: str | None` (keyword-only) — optional override for the `ANTHROPIC_MODEL` Flask config; pass `None` (default) to use the configured value.
- **Returns:** `NoteGenerationResponse` with fields:
  - `ai_notes: str` — generated text, capped at 8000 characters; cite `[backend/app/schemas/note_generation.py:L203-L212]`.
  - `model: str` — resolved model identifier echoed back to the caller (1-128 characters); cite `[backend/app/schemas/note_generation.py:L213-L224]`.
  - `generated_at: datetime` — timezone-aware UTC value via the `AwareDatetime` annotation; cite `[backend/app/schemas/note_generation.py:L225]`.
- **Raises:**
  - `ValidationFailedError(code="ai_invalid_context", status_code=422)` — sanitized context is empty (input was only control chars, bidi marks, zero-width chars, or whitespace); cite `[backend/app/services/ai_orchestration.py:L573-L578]`.
  - `AIServiceUnavailableError(code="ai_not_configured", status_code=503)` — `ANTHROPIC_API_KEY` is missing or empty; raised inside `_get_anthropic_api_key()` at `[backend/app/services/ai_orchestration.py:L446]`.
  - `AIServiceUnavailableError(code="ai_timeout", status_code=504)` — watchdog cancellation when the `timeout_s + 0.5s` budget is exceeded by `_invoke_with_timeout`; cite `[backend/app/services/ai_orchestration.py:L621]`.
  - `AIServiceUnavailableError(code="ai_unavailable", status_code=502)` — any other provider exception (network failure, malformed response, etc.); cite `[backend/app/services/ai_orchestration.py:L636-L642]`.

### Sole public AI exception: `AIServiceUnavailableError`

`AIServiceUnavailableError` extends `AppError` from `app.middleware.error_handlers` and carries **mutable per-instance** `status_code` and `error_code` attributes — this allows a single exception class to represent three distinct HTTP statuses without subclassing. Cite `[backend/app/services/ai_orchestration.py:L257]` for the class definition and the class docstring at L258-L303.

| `error_code` | HTTP Status | Trigger Condition |
|--------------|-------------|-------------------|
| `ai_timeout` | 504 | Watchdog cancellation: `timeout_s + 0.5s` grace budget exceeded by `_invoke_with_timeout` |
| `ai_unavailable` | 502 | Provider exception: ChatAnthropic raised, network failure, or malformed response shape |
| `ai_not_configured` | 503 | Missing or empty `ANTHROPIC_API_KEY` Flask config value |

Note: the 4000-character context cap is enforced by the `NoteGenerationRequest` schema's `max_length=4000` constraint AND by `sanitize_for_ai_prompt(max_chars=...)` truncation. Both layers fail safely without a dedicated exception class — oversized input is rejected at the schema boundary (HTTP 422 `validation_failed`) and any residual character beyond the cap is truncated at the orchestrator boundary before prompt construction.

### Internal helpers (private; do not import directly)

- `_invoke_with_timeout` at `[backend/app/services/ai_orchestration.py:L687]` — Watchdog primitive combining the SDK's own timeout with a `concurrent.futures` future cancellation; **never call directly**.
- `_call_chat_anthropic` at `[backend/app/services/ai_orchestration.py:L779]` — SDK isolation point; performs the LOCAL function-scope imports of `ChatAnthropic`, `HumanMessage`, and `SystemMessage` per the provider-replaceability invariant (AAP § 0.7.7); cite `[backend/app/services/ai_orchestration.py:L834-L838]` for the local imports.
- `_build_user_prompt` at `[backend/app/services/ai_orchestration.py:L906]` — Composes the user-message body from the sanitized context using a stable template.
- `_get_ai_timeout_seconds`, `_get_ai_max_tokens`, `_get_ai_model`, `_get_ai_prompt_context_max_chars`, `_get_anthropic_api_key` — Flask config readers at `[backend/app/services/ai_orchestration.py:L379-L470]`; each has a full PEP 257 docstring.

## 6. Error Handling

1. **The orchestrator converts ALL provider-layer failures into `AIServiceUnavailableError` with appropriate `error_code` and `status_code` BEFORE propagating.** This means the view layer (`backend/app/api/notes.py:generate`) does NOT need to catch provider exceptions — they bubble through to the central error handler registered for `AppError` instances. The mapping is implemented at three call sites: timeout at `[backend/app/services/ai_orchestration.py:L617-L621]`, pass-through of pre-existing `AIServiceUnavailableError` at `[backend/app/services/ai_orchestration.py:L627-L634]`, and catch-all at `[backend/app/services/ai_orchestration.py:L636-L642]`.

2. **The frontend distinguishes "soft" vs "hard" failures via `isSoftAiFailure` in `frontend/src/api/notes.ts`.** Soft failures (`ai_timeout`, `ai_unavailable`) display a non-blocking toast and allow the user to submit the form with manually-typed notes. Hard failures (`ai_not_configured`, `validation_failed`) display a more prominent error since they typically indicate an operational or input problem the user can act on. The classification is the F-002 graceful-degradation contract per AAP § 0.4.4.

3. **Graceful degradation contract — AI failure does NOT block manual form submission.** The `<Textarea>` for `ai_notes` remains editable in all failure modes. The user is never trapped: they can either retry the AI generation, type their own notes manually, or leave the field blank. This is the F-002 non-blocking invariant and is the primary reason the AI orchestrator emphasizes fast-fail semantics (5-second P95 budget; aggressive watchdog) over retry semantics.

Cite `[backend/app/services/ai_orchestration.py:L257]` (class docstring covers the per-instance attribute mechanism) and `[backend/app/middleware/error_handlers.py:L208]` (the `AppError` parent class).

## 7. Security and Privacy Notes

- **`ANTHROPIC_API_KEY` resolution chain** — Resolved by `_get_anthropic_api_key()` at `[backend/app/services/ai_orchestration.py:L446]`. The Flask config value originates from `.env` in development and from AWS Secrets Manager in production (via the `SECRETS_MANAGER_ANTHROPIC_KEY_ID` indirection at `[backend/.env.example:L183]`). The key is NEVER bundled in the frontend; the SPA only talks to `/api/notes/generate` — it never sees the Anthropic endpoint directly. Missing or empty values produce `AIServiceUnavailableError(code="ai_not_configured", status_code=503)`.

- **Sanitization invariant** — `sanitize_for_ai_prompt(...)` (from `[backend/app/utils/sanitization.py]`) is applied at the orchestrator boundary BEFORE prompt construction at `[backend/app/services/ai_orchestration.py:L560]`. A comprehensive rationale comment block at L555-L559 cites AAP § 0.7.4. The `max_chars` parameter is `AI_PROMPT_CONTEXT_MAX_CHARS` (default `4000` from the constant at `[backend/app/services/ai_orchestration.py:L173]`). This defends against prompt-injection control sequences, bidi marks, zero-width characters, and oversized inputs.

- **Empty-input guard** — If sanitization produces an empty string (input was entirely control characters, bidi marks, zero-width characters, or whitespace), the orchestrator raises `ValidationFailedError(code="ai_invalid_context", status_code=422)` BEFORE making a billable Claude round-trip; cite `[backend/app/services/ai_orchestration.py:L573-L578]`. This guard is intentional: the round-trip would be both useless (Claude has nothing to summarize) and incur cost.

- **Never-log raw context** — The bound structlog logger at `[backend/app/services/ai_orchestration.py:L601-L606]` carries `ai_model`, `prompt_chars` (integer length only), `timeout_seconds`, and `caller_thread`. None of the four AI-orchestrator structlog events (`ai_request_start`, `ai_request_timeout`, `ai_request_error`, `ai_request_success`) emit the raw `relationship_context` or the raw `ai_notes` text at any level. The view layer (`backend/app/api/notes.py`) similarly logs only `user_id`, `org_id`, and `context_chars` (length). This is the AAP § 0.7.4 PII invariant.

Cross-link: see `[docs/security.md]` for the canonical security policy.


## 8. Operational Notes

This section enumerates the live operational surfaces emitted by the AI orchestrator and the configuration knobs that govern its behavior. All entries are observable in local development and in production.

### Reused vs Added (per the user-specified Observability rule)

Per the user-specified Observability rule, this section explicitly distinguishes operational surfaces that were already present in the codebase from any that this documentation deliverable adds. **This documentation deliverable adds no new observability tooling.** Every metric, log event, alarm, and health-check enumerated below is REUSED from the pre-existing F-002 implementation.

| Observability Surface | Status | Source File |
|-----------------------|--------|-------------|
| Prometheus histogram `ai_request_duration_seconds{outcome}` | Reused (Pre-existing) | `[backend/app/observability/metrics.py:L263-L272]` |
| structlog event `ai_request_start` | Reused (Pre-existing) | `[backend/app/services/ai_orchestration.py:L607]` |
| structlog event `ai_request_success` | Reused (Pre-existing) | `[backend/app/services/ai_orchestration.py:L662]` |
| structlog event `ai_request_timeout` | Reused (Pre-existing) | `[backend/app/services/ai_orchestration.py:L621]` |
| structlog event `ai_request_error` | Reused (Pre-existing) | `[backend/app/services/ai_orchestration.py:L645]` |
| CloudWatch alarm `ai_latency_p95` | Reused (Pre-existing) | `[infra/terraform/modules/observability/main.tf:L446]` |
| Health/readiness probes `/healthz`, `/readyz` | Reused (Pre-existing) | `[backend/app/api/health.py]` (not specific to AI; mentioned for completeness) |
| Bound structlog context (`ai_model`, `prompt_chars`, `timeout_seconds`, `caller_thread`) | Reused (Pre-existing) | `[backend/app/services/ai_orchestration.py:L601-L606]` |
| **Added by This Deliverable** | (none) | — |

**Verification:** every entry above can be exercised in local development via the commands documented in § 8.5 ("Local-dev verification") below; no new instrumentation was required, and the documentation deliverable did not modify `[backend/app/observability/metrics.py]`, `[backend/app/services/ai_orchestration.py]` (telemetry call sites unchanged), or any Terraform alarm definition.

### Prometheus histogram

- **Name:** `ai_request_duration_seconds`
- **Registered in:** `[backend/app/observability/metrics.py:L263-L272]`
- **Help text:** "End-to-end Anthropic Claude AI request duration in seconds. Per AAP F-002, P95 must remain under 5 seconds."
- **Label:** `outcome` (single label)
- **Observed from:** `[backend/app/services/ai_orchestration.py]` at validation L574, timeout L620, error L644, and success L661
- **Outcome label values** (exactly four, defined at `[backend/app/services/ai_orchestration.py:L232-L235]`):
  - `success` — happy-path completion
  - `timeout` — watchdog cancellation
  - `error` — provider exception or other catch-all
  - `validation` — pre-flight empty-input guard rejection

### structlog events emitted by the AI orchestrator

| Event | Level | Line | Additional fields beyond bound context |
|-------|-------|------|---------------------------------------|
| `ai_request_start` | info | L607 | (none; uses bound fields only) |
| `ai_request_success` | info | L662 | `elapsed_seconds`, `response_chars` |
| `ai_request_timeout` | warning | L621 | `elapsed_seconds` |
| `ai_request_error` | error | L645 | `elapsed_seconds`, `error_class` |

The bound logger established at `[backend/app/services/ai_orchestration.py:L601-L606]` attaches these context keys to every event: `ai_model`, `prompt_chars`, `timeout_seconds`, `caller_thread`.

There is also an upstream view-layer event: `ai_note_generation_requested` emitted at `[backend/app/api/notes.py:L447-L454]` with `user_id`, `org_id`, and `context_chars` (length only). The raw `relationship_context` is NEVER logged.

The structured-log field name `context_chars` (integer character count of `relationship_context`) is retained per the field-naming convention captured in `[docs/decision-log.md:DL-0062]`; it matches the AI orchestrator's bound `prompt_chars` and the `response_chars` field on `ai_request_success`, and preserves wire-compatibility with existing downstream log-aggregation queries that pivot on this key.

### CloudWatch alarm: `ai_latency_p95`

| Property | Value |
|----------|-------|
| Resource | `aws_cloudwatch_metric_alarm.ai_latency_p95` |
| Source | `[infra/terraform/modules/observability/main.tf:L446]` |
| Alarm name | `${var.name_prefix}-ai-latency-p95` |
| Metric | `AICallDurationMs` (derived from `ai_request_duration_seconds` histogram) |
| Statistic | `extended_statistic = "p95"` |
| Threshold | `var.alarm_threshold_p95_ms_ai_call` |
| Comparison | `GreaterThanThreshold` |
| Evaluation | 3 of 5 datapoints (`evaluation_periods=5`, `datapoints_to_alarm=3`) |
| Period | 300 seconds (5 minutes per datapoint) |
| Missing data | `notBreaching` |

Cross-link: see `[docs/operations.md]` for the canonical alarm runbook.

### Configuration knobs

| Variable | Default | Source |
|----------|---------|--------|
| `ANTHROPIC_API_KEY` | (empty / required for runtime) | `[backend/.env.example:L127]` |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | `[backend/.env.example:L131]` |
| `ANTHROPIC_MAX_TOKENS` | `512` | `[backend/.env.example:L135]` |
| `AI_REQUEST_TIMEOUT_SECONDS` | `5` | `[backend/.env.example:L138]` |
| `AI_PROMPT_CONTEXT_MAX_CHARS` | `4000` | `[backend/app/services/ai_orchestration.py:L173]` |
| `SECRETS_MANAGER_ANTHROPIC_KEY_ID` | (empty / optional) | `[backend/.env.example:L183]` |

### Pinned dependencies (from `backend/requirements.txt`)

- `anthropic==0.97.0`
- `langchain==0.3.27`
- `langchain-core==0.3.78`
- `langchain-anthropic==0.3.21`
- `structlog==24.4.0`
- `prometheus-client==0.21.1`
- `opentelemetry-instrumentation-httpx==0.50b0`

**Dependency security advisory note.** Public security advisories affect the currently pinned `langchain==0.3.27` and `langchain-core==0.3.78` versions. The upgrade is a dependency-management change that is **out of scope** for this documentation-only deliverable per the AAP Minimal Change Clause (§ 0.2.1) and the AAP § 0.7.3 dependency-update budget ("Added: 0, Removed: 0, Updated: 0"). The advisory awareness, the recommended target versions, and the deferral rationale are captured in decision-log entry DL-0061 so a future security-hardening epic can pick up the upgrade alongside its own integration testing. Cross-reference: `[docs/decision-log.md:DL-0061]`.

### Local-dev verification

```bash
cd backend
FLASK_APP=app:create_app flask shell -c "from app.services import generate_outreach_notes; from app.schemas import NoteGenerationRequest; print(generate_outreach_notes(NoteGenerationRequest(relationship_context='VP Eng at Acme; met at SaaStr')))"
```

Requires `ANTHROPIC_API_KEY` set in `.env`. Without it, the call raises `AIServiceUnavailableError(code='ai_not_configured', status_code=503)` — which is the expected fail-fast behavior.

## 9. Examples

### Example 1 — Python call pattern

```python
from app.services import generate_outreach_notes
from app.schemas import NoteGenerationRequest

request = NoteGenerationRequest(relationship_context="VP Eng at Acme; met at SaaStr")
response = generate_outreach_notes(request)
```

Returns a `NoteGenerationResponse` containing `ai_notes`, `model`, and a timezone-aware `generated_at`.

### Example 2 — Catching the error envelope

```python
from app.services.ai_orchestration import AIServiceUnavailableError

try:
    response = generate_outreach_notes(request)
except AIServiceUnavailableError as exc:
    # exc.status_code is one of 504, 503, 502;
    # exc.error_code is one of "ai_timeout", "ai_not_configured", "ai_unavailable".
    handle_ai_failure(exc.error_code, exc.status_code)
```

The frontend uses `isSoftAiFailure(error_code)` to decide between non-blocking toast (soft) and prominent error (hard) — see `[frontend/src/api/notes.ts]`.

## 10. Related Modules

- `[backend/app/api/README.md]` — View-layer documentation; the Flask blueprint that invokes this service via `POST /api/notes/generate`.
- `[backend/app/schemas/note_generation.py]` — `NoteGenerationRequest` and `NoteGenerationResponse` Pydantic models (the file is named `note_generation.py`, not `notes.py`).
- `[backend/app/utils/sanitization.py]` — `sanitize_for_ai_prompt` utility enforcing the AAP § 0.7.4 input-sanitization invariant.
- `[backend/app/middleware/error_handlers.py]` — `AppError` hierarchy, `ValidationFailedError`, and the central error-envelope handler that converts these exceptions to HTTP responses.
- `[backend/app/observability/metrics.py]` — `ai_request_duration_seconds` histogram registration.
- `[docs/ai-note-generation-workflow.md]` — Cross-cutting workflow deep-dive covering SPA → API → service → provider in one document.
- `[docs/architecture.md]` — System architecture context (F-002 architectural fit).
- `[docs/security.md]` — Canonical credential-handling and sanitization policy.
- `[docs/operations.md]` — Observability runbook including the `ai_latency_p95` alarm response procedure.
- `[docs/decision-log.md]` — Design decisions for this documentation deliverable (file-path deviations, README placement rationale, plain-Markdown choice).
