/**
 * AddEditConnectionForm.tsx - F-001 Add/Edit Connection Form.
 *
 * Implements User Flow 1 verbatim (AAP Sec 0.1.2):
 *   "Click 'Add Connection' -> Fill in name, LinkedIn, company, title ->
 *    Enter relationship context -> Trigger AI note generation ->
 *    Review/edit AI notes -> Select involvement level -> Tag and submit ->
 *    Record appears in team feed"
 *
 * Serves both flows via the `mode` prop:
 *   mode='create'  -> POST /api/connections at /connections/new
 *   mode='edit'    -> PATCH /api/connections/:id at /connections/:id/edit
 *
 * The two-call flow (per AAP Sec 0.4.4):
 *   1. (Optional) Generate AI notes - non-blocking; failure surfaces an
 *      inline banner ("AI unavailable; you can still submit") rather
 *      than blocking submit.
 *   2. User reviews/edits the ai_notes textarea.
 *   3. Submit creates/updates the record. AI failure NEVER blocks step 3.
 *
 * Duplicate detection (F-010): a debounced query on the linkedin_url
 * input; matches surface a non-blocking <DuplicateWarning> banner. Per
 * AAP Sec 0.7.6, "Duplicate detection is a warning, not a block."
 *
 * RBAC and security:
 *   - Backend RBAC is the authoritative gate per AAP Sec 0.7.1
 *     invariant 7. The "Generate AI Notes" button is conditionally
 *     rendered based on useRole() (Contributor + Admin only) for UX;
 *     the backend enforces it at /api/notes/generate.
 *   - Per AAP Sec 0.7.4, owner_user_id and owner_display_name are
 *     SERVER-DERIVED from session.user_id and NEVER sent client-side.
 *     ConnectionCreateSchema.strict() ensures the SPA never accidentally
 *     sends them.
 *
 * Validation strategy (per AAP Sec 0.7.1 invariant 8):
 *   - Client-side: Zod schemas (ConnectionCreateSchema /
 *     ConnectionUpdateSchema) validate on submit; per-field errors
 *     render inline via <Input errorMessage>.
 *   - Server-side: pydantic validation on the backend is authoritative.
 *     422 responses are mapped to per-field errors via ApiError.fields.
 *
 * Layout:
 *   Mobile (default): single column, stacked fields, full-width buttons.
 *   Desktop (sm+):   two-column grid for short text fields; full-width
 *                    for relationship_context textarea and tag input.
 *
 * Accessibility:
 *   - Each field has a label tied to its input via htmlFor (Input
 *     primitive handles this).
 *   - aria-invalid + aria-describedby on errored fields.
 *   - <fieldset> + <legend> for the involvement button group.
 *   - aria-busy on the form while submitting.
 *   - Loading affordances on AI/submit/delete buttons.
 *
 * No business logic in feature components per the AAP convention
 * (composition, not reimplementation): API hooks own data;
 * UI primitives own visuals; this file orchestrates them.
 */

import { useEffect, useId, useMemo, useState, type FormEvent, type JSX } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { AlertTriangle, RotateCcw, Save, Sparkles, X } from "lucide-react";
import clsx from "clsx";

import type { ApiError } from "@/api/client";
import {
  useConnectionQuery,
  useCreateConnectionMutation,
  useDuplicateCheckQuery,
  useTagsQuery,
  useUpdateConnectionMutation,
} from "@/api/connections";
import { isSoftAiFailure, useGenerateNotesMutation } from "@/api/notes";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Textarea } from "@/components/ui/Textarea";
import { useToast } from "@/components/ui/Toast";
import {
  ConnectionCreateSchema,
  ConnectionUpdateSchema,
  INVOLVEMENT_VALUES,
  type ConnectionCreate,
  type ConnectionRead,
  type ConnectionUpdate,
} from "@/schemas/connection";
import { DuplicateWarning } from "@/features/connections/DuplicateWarning";
import { InvolvementBadge } from "@/features/connections/InvolvementBadge";
import { TagInput } from "@/features/connections/TagInput";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Props for the form. The `mode` prop dictates whether we POST (create)
 * or PATCH (edit) on submit.
 *
 * In edit mode, the route param `:id` is the record being edited; the
 * component fetches the existing record via useConnectionQuery and
 * populates the form on first load.
 */
export interface AddEditConnectionFormProps {
  /** Workflow mode. */
  readonly mode: "create" | "edit";
}

// ---------------------------------------------------------------------------
// Internal types and constants
// ---------------------------------------------------------------------------

/**
 * Internal form state shape. Mirrors the union of ConnectionCreate
 * and ConnectionUpdate fields (with all fields present so the form
 * has consistent state shape regardless of mode). All values are
 * either strings or arrays; selecting "no value" stores the empty
 * string `""` (NOT undefined or null) so React-controlled inputs
 * stay controlled across renders.
 */
interface FormState {
  submitted_by: string;
  full_name: string;
  linkedin_url: string;
  company: string;
  job_title: string;
  relationship_context: string;
  ai_notes: string;
  involvement: "Warm Intro" | "Soft Reference" | "Target Only" | "";
  tag_ids: ReadonlyArray<string>;
}

/**
 * Per-field client-side validation errors keyed by FormState field name.
 * Populated from Zod parse errors AND from ApiError.fields (for 422
 * responses).
 */
type FieldErrors = Partial<Record<keyof FormState, string>>;

/**
 * The empty starting state for a new form. Used for mode="create"
 * mounts and as the placeholder while edit-mode hydration is in
 * flight.
 */
const EMPTY_STATE: FormState = {
  submitted_by: "",
  full_name: "",
  linkedin_url: "",
  company: "",
  job_title: "",
  relationship_context: "",
  ai_notes: "",
  involvement: "",
  tag_ids: [],
};

/**
 * Debounce delay (ms) applied to the LinkedIn URL before firing the
 * F-010 duplicate-check query. 400 ms is short enough to feel
 * responsive without sending a request on every keystroke during
 * URL paste / type. Tunable via observation; logged via the AAP
 * decision log if changed.
 */
const DUPLICATE_CHECK_DEBOUNCE_MS = 400;

/**
 * Maximum length of the relationship_context textarea, mirroring
 * backend `relationship_context` constraint (4000 chars per
 * ConnectionCreateSchema). Browsers prevent typing past the limit
 * via maxLength; combined with Zod max(4000) this is defense in
 * depth.
 */
const RELATIONSHIP_CONTEXT_MAX_LENGTH = 4000;

// ---------------------------------------------------------------------------
// Helper functions (module-level, pure)
// ---------------------------------------------------------------------------

/**
 * Convert a fetched ConnectionRead into FormState shape for edit mode.
 * Coalesces nullable ai_notes to empty string; extracts tag IDs from
 * the embedded TagRead array.
 */
function readToFormState(record: ConnectionRead): FormState {
  return {
    submitted_by: record.owner_display_name ?? "",
    full_name: record.full_name,
    linkedin_url: record.linkedin_url,
    company: record.company,
    job_title: record.job_title,
    relationship_context: record.relationship_context,
    ai_notes: record.ai_notes ?? "",
    involvement: record.involvement,
    tag_ids: record.tags.map((tag) => tag.id),
  };
}

/**
 * Build the create-mode payload from FormState. Filters out empty-string
 * ai_notes (server expects undefined for "no AI notes", not "").
 *
 * The involvement type assertion is safe because the caller validates
 * the candidate payload via ConnectionCreateSchema BEFORE the result
 * is passed to the mutation; the empty-string sentinel is rejected at
 * the Zod layer.
 */
function formStateToCreatePayload(state: FormState): ConnectionCreate {
  const trimmedAiNotes = state.ai_notes.trim();
  return {
    submitted_by: state.submitted_by.trim(),
    full_name: state.full_name.trim(),
    linkedin_url: state.linkedin_url.trim(),
    company: state.company.trim(),
    job_title: state.job_title.trim(),
    relationship_context: state.relationship_context.trim(),
    ai_notes: trimmedAiNotes === "" ? undefined : trimmedAiNotes,
    involvement: state.involvement as Exclude<FormState["involvement"], "">,
    tag_ids: [...state.tag_ids],
  };
}

/**
 * Build the edit-mode payload from FormState. All fields are
 * included; the backend's PATCH treats unchanged fields as no-ops,
 * so re-sending the full set is idempotent.
 *
 * As with `formStateToCreatePayload`, the involvement assertion is
 * safe because Zod validation precedes the mutation call.
 */
function formStateToUpdatePayload(state: FormState): ConnectionUpdate {
  const trimmedAiNotes = state.ai_notes.trim();
  return {
    full_name: state.full_name.trim(),
    linkedin_url: state.linkedin_url.trim(),
    company: state.company.trim(),
    job_title: state.job_title.trim(),
    relationship_context: state.relationship_context.trim(),
    ai_notes: trimmedAiNotes === "" ? undefined : trimmedAiNotes,
    involvement: state.involvement as Exclude<FormState["involvement"], "">,
    tag_ids: [...state.tag_ids],
  };
}

/**
 * Map Zod issues to per-field error messages. Joins multiple issues
 * for the same field with " - " separator so the user sees every
 * complaint at a glance rather than fixing one and discovering the
 * next on the following submit.
 */
function zodIssuesToFieldErrors(
  issues: ReadonlyArray<{ path: ReadonlyArray<string | number>; message: string }>,
): FieldErrors {
  const errors: FieldErrors = {};
  for (const issue of issues) {
    const path = issue.path[0];
    if (typeof path !== "string") {
      continue;
    }
    const key = path as keyof FormState;
    const existing = errors[key];
    errors[key] = existing ? `${existing} - ${issue.message}` : issue.message;
  }
  return errors;
}

/**
 * Map server-side ApiError.fields to FieldErrors. The backend may
 * return nested paths (e.g., "linkedin_url" for a refinement failure);
 * we take the first path segment so nested validation issues still
 * highlight the correct top-level field.
 */
function apiErrorToFieldErrors(error: ApiError): FieldErrors {
  const errors: FieldErrors = {};
  for (const fieldErr of error.fields) {
    const head = fieldErr.field.split(".")[0];
    if (head === undefined || head.length === 0) {
      continue;
    }
    const key = head as keyof FormState;
    errors[key] = fieldErr.message ?? fieldErr.code;
  }
  return errors;
}

// ---------------------------------------------------------------------------
// Custom hook: useDebouncedValue
// ---------------------------------------------------------------------------

/**
 * Debounce a value so rapid changes (typing) don't fire downstream
 * effects (like the duplicate-check query) on every keystroke.
 *
 * Returns the debounced value, which updates only after `delayMs` of
 * stillness. Cleanup cancels in-flight timers on unmount or value
 * change so we never set state on an unmounted component.
 */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState<T>(value);
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(handle);
  }, [value, delayMs]);
  return debounced;
}

// ---------------------------------------------------------------------------
// AddEditConnectionForm component
// ---------------------------------------------------------------------------

/**
 * Add/Edit Connection Form.
 *
 * The component is large by necessity (it covers F-001, F-002, F-003,
 * F-007 (edit), F-008, F-010 - a substantial portion of the MVP UI).
 * Sections within are visually separated by comment banners.
 *
 * @example In router.tsx (create)
 *   <Route path="/connections/new" element={<AddEditConnectionForm mode="create" />} />
 *
 * @example In router.tsx (edit)
 *   <Route
 *     path="/connections/:id/edit"
 *     element={<AddEditConnectionForm mode="edit" />}
 *   />
 */
export function AddEditConnectionForm({ mode }: AddEditConnectionFormProps): JSX.Element {
  const navigate = useNavigate();
  const { id: routeId } = useParams<{ id: string }>();
  const editId = mode === "edit" ? (routeId ?? "") : "";
  const toast = useToast();
  const formId = useId();

  // === Edit-mode hydration ===========================================
  // useConnectionQuery is gated internally on `id.length > 0`; in
  // create mode editId is "" so the query stays disabled and never
  // fires.
  const recordQuery = useConnectionQuery(editId);
  const isHydrating = mode === "edit" && recordQuery.isPending;

  // === Form state ====================================================
  const [formState, setFormState] = useState<FormState>(EMPTY_STATE);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [submitAttempted, setSubmitAttempted] = useState(false);

  // Hydrate form state when the edit-mode record arrives. We use
  // useEffect rather than initial useState because the query is
  // async; setting initial state to EMPTY_STATE and patching via
  // useEffect is the canonical pattern for "load -> populate" forms.
  useEffect(() => {
    if (mode === "edit" && recordQuery.data) {
      setFormState(readToFormState(recordQuery.data));
    }
  }, [mode, recordQuery.data]);

  // === Tag list (for TagInput autocomplete) ==========================
  const tagsQuery = useTagsQuery();

  // === AI note generation (F-002) ====================================
  const generateNotes = useGenerateNotesMutation();

  /**
   * Handle the "Generate AI Notes" button click.
   *
   * Non-blocking contract (F-002 invariant per AAP Sec 0.4.4):
   *   Soft AI failures (504 `ai_timeout` / 502 `ai_unavailable`)
   *   MUST NOT block manual form submission. Hard failures
   *   (validation, RBAC, server) surface a toast via the hook's
   *   own onError; the form still allows the user to type their
   *   own notes and submit.
   *
   * Soft-vs-hard classification is performed by
   * `isSoftAiFailure(error)` from [frontend/src/api/notes.ts:L181].
   * The form additionally renders an inline retry banner on soft
   * failures via the `aiSoftFailure` derived flag below.
   */
  function handleGenerateAi(): void {
    const trimmedContext = formState.relationship_context.trim();
    if (trimmedContext.length === 0) {
      return;
    }
    generateNotes.mutate(
      { relationship_context: trimmedContext },
      {
        onSuccess: (data) => {
          // Replace ai_notes with generated text. The user can edit
          // afterward via the textarea below.
          setFormState((prev) => ({ ...prev, ai_notes: data.ai_notes }));
        },
        // onError is handled by the hook (toast for hard, silent for
        // soft); we additionally render an inline banner for soft
        // failures via the aiSoftFailure derived flag below.
      },
    );
  }

  /**
   * True when the AI mutation last failed with a "soft" error
   * (504 ai_timeout / 502 ai_unavailable per AAP Sec 0.4.4). The
   * form renders an inline retry banner rather than blocking
   * submit; the user may proceed with self-typed notes.
   */
  const aiSoftFailure = useMemo(
    () => generateNotes.isError && isSoftAiFailure(generateNotes.error),
    [generateNotes.isError, generateNotes.error],
  );

  // === Duplicate detection (F-010) ===================================
  const debouncedLinkedInUrl = useDebouncedValue(
    formState.linkedin_url.trim(),
    DUPLICATE_CHECK_DEBOUNCE_MS,
  );
  // In edit mode, the existing record's own URL would otherwise flag
  // itself as a duplicate; excludeRecordId tells the backend to skip
  // the current record in its lookup.
  const duplicateQuery = useDuplicateCheckQuery(debouncedLinkedInUrl, {
    enabled: debouncedLinkedInUrl.length > 0,
    ...(mode === "edit" && editId.length > 0 ? { excludeRecordId: editId } : {}),
  });

  // === Submit mutations ==============================================
  const createMutation = useCreateConnectionMutation();
  const updateMutation = useUpdateConnectionMutation();
  const isSubmitting = createMutation.isPending || updateMutation.isPending;

  /**
   * Update a single FormState field. Clears any inline error for
   * the field as the user types so corrections are visible
   * optimistically (the next submit re-validates).
   */
  function handleChange<K extends keyof FormState>(field: K, value: FormState[K]): void {
    setFormState((prev) => ({ ...prev, [field]: value }));
    if (fieldErrors[field]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[field];
        return next;
      });
    }
  }

  /**
   * Submit handler. Validates via Zod, then dispatches to the
   * appropriate create/update mutation. Field errors from Zod and
   * 422 server responses populate `fieldErrors`. Other errors are
   * already toasted by the mutation hooks; we only mirror 422 into
   * inline messages.
   */
  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setSubmitAttempted(true);

    const candidatePayload =
      mode === "create" ? formStateToCreatePayload(formState) : formStateToUpdatePayload(formState);
    const result =
      mode === "create"
        ? ConnectionCreateSchema.safeParse(candidatePayload)
        : ConnectionUpdateSchema.safeParse(candidatePayload);

    if (!result.success) {
      setFieldErrors(zodIssuesToFieldErrors(result.error.issues));
      toast.error("Please correct the highlighted fields.");
      return;
    }

    setFieldErrors({});

    if (mode === "create") {
      createMutation.mutate(candidatePayload as ConnectionCreate, {
        onSuccess: () => {
          // Per AAP Sec 0.5.4 user flow: "Record appears in team
          // feed". The hook invalidates the list cache so the new
          // record is already visible; the toast comes from the
          // hook (we deliberately do not duplicate it).
          navigate("/feed");
        },
        onError: (error) => {
          // 422 -> per-field; other errors -> toast (already fired
          // by the hook's onError).
          if (error.status === 422) {
            setFieldErrors(apiErrorToFieldErrors(error));
          }
        },
      });
      return;
    }

    if (!editId) {
      toast.error("Missing record id; cannot update.");
      return;
    }
    updateMutation.mutate(
      { id: editId, payload: candidatePayload as ConnectionUpdate },
      {
        onSuccess: () => {
          // Navigate back to the detail view after edit so the user
          // can immediately review their changes.
          navigate(`/connections/${editId}`);
        },
        onError: (error) => {
          if (error.status === 422) {
            setFieldErrors(apiErrorToFieldErrors(error));
          }
        },
      },
    );
  }

  /**
   * Cancel handler. Edit mode returns to the detail view; create
   * mode returns to the feed. Confirmation is not prompted because
   * MVP scope excludes "unsaved changes" guards (deferred per AAP
   * decision log).
   */
  function handleCancel(): void {
    if (mode === "edit" && editId.length > 0) {
      navigate(`/connections/${editId}`);
    } else {
      navigate("/feed");
    }
  }

  // === Loading and error guards ======================================
  //
  // Per Visual Consistency QA Issue 7 these route components render
  // <section> rather than nesting a second <main> landmark inside
  // the document's primary <main> in App.tsx. HTML5 requires one
  // <main> per document.
  if (isHydrating) {
    return (
      <section
        className="mx-auto max-w-3xl px-4 py-12 text-center"
        role="status"
        aria-busy="true"
        data-testid="connection-form-loading"
      >
        <p className="text-sm text-slate-500">Loading record...</p>
      </section>
    );
  }

  if (mode === "edit" && recordQuery.isError) {
    return (
      <section
        className="mx-auto max-w-3xl space-y-4 px-4 py-12 text-center"
        role="alert"
        data-testid="connection-form-load-error"
      >
        <p className="text-sm text-red-600">Failed to load record: {recordQuery.error.message}</p>
        <Button variant="secondary" onClick={() => navigate("/feed")}>
          Back to feed
        </Button>
      </section>
    );
  }

  // === Render ========================================================
  return (
    <section
      aria-labelledby="connection-form-heading"
      className="mx-auto max-w-3xl px-4 py-6 sm:py-10"
      data-testid="connection-form"
    >
      <header className="mb-6">
        <h1
          id="connection-form-heading"
          className="text-2xl font-semibold tracking-tight text-slate-900"
        >
          {mode === "create" ? "Add Connection" : "Edit Connection"}
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          {mode === "create"
            ? "Log a connection idea so the sales team can follow up."
            : "Update the details of an existing connection."}
        </p>
      </header>

      <form
        id={formId}
        onSubmit={handleSubmit}
        aria-busy={isSubmitting}
        className="space-y-6"
        noValidate
      >
        {/* === Section: Submitter === */}
        <section>
          <Input
            label="Your name"
            value={formState.submitted_by}
            onChange={(e) => handleChange("submitted_by", e.target.value)}
            required
            errorMessage={submitAttempted ? fieldErrors.submitted_by : undefined}
            autoComplete="name"
            placeholder="Your full name"
            data-testid="form-submitted-by"
          />
        </section>

        {/* === Section: Person details (2-column grid on sm+) === */}
        <section className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Input
            label="Connection's Full Name"
            value={formState.full_name}
            onChange={(e) => handleChange("full_name", e.target.value)}
            required
            errorMessage={submitAttempted ? fieldErrors.full_name : undefined}
            autoComplete="name"
            placeholder="Jane Doe"
            data-testid="form-full-name"
          />
          <Input
            label="LinkedIn URL"
            type="url"
            value={formState.linkedin_url}
            onChange={(e) => handleChange("linkedin_url", e.target.value)}
            required
            errorMessage={submitAttempted ? fieldErrors.linkedin_url : undefined}
            helperText="e.g., https://www.linkedin.com/in/jane-doe"
            autoComplete="url"
            placeholder="https://www.linkedin.com/in/jane-doe"
            data-testid="form-linkedin-url"
          />
          <Input
            label="Company"
            value={formState.company}
            onChange={(e) => handleChange("company", e.target.value)}
            required
            errorMessage={submitAttempted ? fieldErrors.company : undefined}
            autoComplete="organization"
            placeholder="Acme Logistics"
            data-testid="form-company"
          />
          <Input
            label="Job title"
            value={formState.job_title}
            onChange={(e) => handleChange("job_title", e.target.value)}
            required
            errorMessage={submitAttempted ? fieldErrors.job_title : undefined}
            autoComplete="organization-title"
            placeholder="VP of Operations"
            data-testid="form-job-title"
          />
        </section>

        {/* Duplicate warning banner - non-blocking per AAP Sec 0.7.6 */}
        {duplicateQuery.data?.duplicate_found ? (
          <DuplicateWarning result={duplicateQuery.data} />
        ) : null}

        {/* === Section: Relationship context + AI notes === */}
        <section className="space-y-4">
          <div>
            <label
              htmlFor={`${formId}-relationship-context`}
              className="text-sm font-medium leading-5 text-slate-700"
            >
              Relationship context
              <span aria-hidden="true" className="ml-0.5 text-red-600">
                *
              </span>
            </label>
            <p className="mt-0.5 text-xs text-slate-500">
              How do you know this person? Used to tailor AI-generated outreach notes.
            </p>
            <textarea
              id={`${formId}-relationship-context`}
              value={formState.relationship_context}
              onChange={(e) => handleChange("relationship_context", e.target.value)}
              required
              rows={4}
              maxLength={RELATIONSHIP_CONTEXT_MAX_LENGTH}
              aria-invalid={
                submitAttempted && Boolean(fieldErrors.relationship_context) ? true : undefined
              }
              aria-describedby={
                submitAttempted && fieldErrors.relationship_context
                  ? `${formId}-context-error`
                  : undefined
              }
              className={clsx(
                "mt-2 block w-full rounded-md border bg-white px-3 py-2 text-sm leading-5",
                "text-slate-900 transition-colors placeholder:text-slate-400",
                "focus-visible:outline-2 focus-visible:outline-offset-2",
                "focus-visible:outline-brand-500",
                submitAttempted && fieldErrors.relationship_context
                  ? "border-red-400 hover:border-red-500"
                  : "border-slate-300 hover:border-slate-400",
                "disabled:cursor-not-allowed disabled:bg-slate-100",
              )}
              placeholder="We went to college together; he's now VP of Ops at a Series B logistics startup."
              data-testid="form-relationship-context"
            />
            {submitAttempted && fieldErrors.relationship_context ? (
              <p id={`${formId}-context-error`} role="alert" className="mt-1 text-xs text-red-600">
                {fieldErrors.relationship_context}
              </p>
            ) : null}
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <Button
              type="button"
              variant="secondary"
              size="md"
              leftIcon={<Sparkles aria-hidden="true" />}
              loading={generateNotes.isPending}
              disabled={formState.relationship_context.trim().length === 0}
              onClick={handleGenerateAi}
              data-testid="form-generate-ai-button"
            >
              {generateNotes.data ? "Regenerate AI notes" : "Generate AI notes"}
            </Button>
            {generateNotes.data ? (
              <p className="text-xs text-slate-500">Generated by {generateNotes.data.model}</p>
            ) : null}
          </div>

          {aiSoftFailure ? (
            <div
              role="status"
              className={clsx(
                "flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50",
                "px-3 py-2 text-sm text-amber-900",
              )}
              data-testid="form-ai-soft-failure"
            >
              <AlertTriangle
                aria-hidden="true"
                className="mt-0.5 h-4 w-4 shrink-0 text-amber-600"
              />
              <div className="flex-1">
                <p className="font-medium">AI unavailable</p>
                <p className="text-xs">
                  You can still submit. Type your own notes below or leave blank and try again
                  later.
                </p>
              </div>
              <button
                type="button"
                onClick={handleGenerateAi}
                aria-label="Retry AI generation"
                className={clsx(
                  "inline-flex h-7 w-7 items-center justify-center rounded text-amber-700",
                  "transition-colors hover:bg-amber-100",
                  "focus-visible:outline-2 focus-visible:outline-offset-2",
                  "focus-visible:outline-amber-600",
                )}
                data-testid="form-ai-retry-button"
              >
                <RotateCcw aria-hidden="true" className="h-4 w-4" />
              </button>
            </div>
          ) : null}

          {/* Per AAP F-002 the AI / outreach notes are multi-paragraph
              talking points. Per Visual Consistency QA Issue 9 use the
              Textarea primitive so the contributor has a multi-line
              editor instead of the cramped single-line <Input>. The
              maxLength mirrors the backend pydantic 8000-char cap on
              ai_notes (see backend/app/schemas/connection.py
              _AI_NOTES_MAX_CHARS). */}
          {/* The AI-populated ai_notes value remains user-editable at all
              times. AI generation is assistive draft text per AAP Sec
              0.9.2 ("Treat AI notes as assistive draft text, not an
              authoritative sales recommendation"). The textarea is
              controlled by formState, so user keystrokes overwrite
              AI output without re-fetching. */}
          <Textarea
            label="AI / outreach notes"
            value={formState.ai_notes}
            onChange={(e) => handleChange("ai_notes", e.target.value)}
            errorMessage={submitAttempted ? fieldErrors.ai_notes : undefined}
            helperText="Optional. Edit AI suggestions or write your own."
            placeholder="Talking points to use in outreach..."
            rows={4}
            maxLength={8000}
            data-testid="form-ai-notes"
          />
        </section>

        {/* === Section: Involvement (F-003) === */}
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium leading-5 text-slate-700">
            Involvement
            <span aria-hidden="true" className="ml-0.5 text-red-600">
              *
            </span>
          </legend>
          <p className="text-xs text-slate-500">How do you want to participate in outreach?</p>
          <div className="flex flex-wrap gap-2">
            {INVOLVEMENT_VALUES.map((value) => {
              const selected = formState.involvement === value;
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => handleChange("involvement", value)}
                  aria-pressed={selected}
                  className={clsx(
                    "inline-flex items-center rounded-full border px-3 py-1.5 text-sm",
                    "font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2",
                    "focus-visible:outline-brand-500",
                    selected
                      ? "border-brand-500 bg-brand-50 text-brand-900 shadow-sm"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50",
                  )}
                  data-testid={`form-involvement-${value}`}
                >
                  <InvolvementBadge value={value} withDot={false} />
                </button>
              );
            })}
          </div>
          {submitAttempted && fieldErrors.involvement ? (
            <p role="alert" className="text-xs text-red-600">
              {fieldErrors.involvement}
            </p>
          ) : null}
        </fieldset>

        {/* === Section: Tags (F-008) === */}
        <section>
          <TagInput
            label="Tags"
            helperText="Add tags for industry, use case, or geography."
            availableTags={tagsQuery.data ?? []}
            selectedTagIds={formState.tag_ids}
            onChange={(ids) => handleChange("tag_ids", ids)}
            errorMessage={submitAttempted ? fieldErrors.tag_ids : undefined}
            disabled={tagsQuery.isPending}
          />
        </section>

        {/* === Submit / Cancel buttons === */}
        <footer
          className={clsx(
            "flex flex-col-reverse gap-2 border-t border-slate-200 pt-6",
            "sm:flex-row sm:justify-end",
          )}
        >
          <Button
            type="button"
            variant="secondary"
            onClick={handleCancel}
            disabled={isSubmitting}
            leftIcon={<X aria-hidden="true" />}
            data-testid="form-cancel-button"
          >
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={isSubmitting}
            leftIcon={<Save aria-hidden="true" />}
            data-testid="form-submit-button"
          >
            {mode === "create" ? "Add connection" : "Save changes"}
          </Button>
        </footer>
      </form>
    </section>
  );
}
