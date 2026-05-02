/**
 * DuplicateWarning.tsx - F-010 Inline Non-Blocking Duplicate Warning Banner.
 *
 * Renders an amber-toned banner when the LinkedIn URL the user is
 * entering matches an existing record in the same organization.
 *
 * Critical UX constraint per AAP Sec 0.7.6 (Business Rules):
 *   "Duplicate detection is a warning, not a block. The user is
 *    informed but may proceed."
 *
 * The banner:
 *   - Surfaces the existing record's owner and submission date so the
 *     user can decide whether to ping the existing owner or proceed
 *     anyway.
 *   - Provides a link to view the existing record in a new tab so the
 *     form's state is not lost when the user investigates the prior
 *     submission.
 *   - Does NOT prevent form submission. The parent form
 *     (AddEditConnectionForm) ignores this warning when wiring up the
 *     submit handler; the warning only informs.
 *
 * Server-side duplicate detection per AAP Sec 0.4.7 lives in
 * `GET /api/connections/duplicate-check`, which queries the unique
 * partial index on `(org_id, normalized_linkedin_url) WHERE
 * deleted_at IS NULL`. If the URL is normalized to match a prior
 * record, that record's `id`, `owner_display_name`, and
 * `submission_date` come back via ConnectionDuplicateCheckResponse
 * as the FLAT fields `existing_record_id`,
 * `existing_owner_display_name`, and `existing_submission_date`.
 *
 * Schema fidelity:
 *   The shape consumed here MUST match
 *   `frontend/src/schemas/connection.ts::ConnectionDuplicateCheckResponseSchema`
 *   exactly. That schema in turn mirrors backend
 *   `app.schemas.connection.ConnectionDuplicateCheckResponse` per
 *   AAP Sec 0.5.3 ("Pydantic and Zod schemas mirror each other").
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit JSX.Element | null return.
 *   - Lucide-React for icons (AlertTriangle, ExternalLink).
 *   - clsx for conditional class composition.
 *   - <Link> from react-router-dom for SPA navigation (NOT native <a>).
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false).
 *   - Snake_case for backend-mirrored fields
 *     (`duplicate_found`, `existing_record_id`, etc.).
 *
 * Coordinates with:
 *   - frontend/src/schemas/connection.ts - source of the
 *     ConnectionDuplicateCheckResponse type.
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx -
 *     the consumer; renders this banner between the linkedin_url
 *     field and the relationship_context section. Passes the result
 *     of useDuplicateCheckQuery directly to the `result` prop.
 *   - frontend/tests/features/connections/DuplicateWarning.test.tsx -
 *     verifies render-when-duplicate, render-null-when-no-duplicate,
 *     defensive fallbacks, and link target attributes.
 */

import { Link } from "react-router-dom";
import { AlertTriangle, ExternalLink } from "lucide-react";
import clsx from "clsx";
import type { JSX } from "react";

import type { ConnectionDuplicateCheckResponse } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Public props for the DuplicateWarning component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers. This convention is
 * shared with the UI primitives (Button, Badge, Modal, Toast).
 */
export interface DuplicateWarningProps {
  /**
   * The duplicate-check response from the
   * `GET /api/connections/duplicate-check` endpoint, typically
   * provided by `useDuplicateCheckQuery` in `@/api/connections`.
   *
   * When `result.duplicate_found === false`, this component renders
   * `null` (no banner). When `true`, the banner renders using the
   * sibling `existing_*` fields.
   */
  readonly result: ConnectionDuplicateCheckResponse;

  /**
   * Optional additional className merged onto the outer <aside>.
   * Allows the parent (AddEditConnectionForm) to layer margin or
   * width utilities for layout integration without forking the
   * primitive. Conflicting Tailwind classes are resolved by source
   * order (later classes win in the cascade).
   */
  readonly className?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format an ISO 8601 datetime string as a short, locale-aware date
 * (e.g., "Apr 23, 2026"). Used to render the submission date of the
 * existing duplicate record.
 *
 * Defensive against:
 *   - null / undefined input -> empty string (so the surrounding text
 *     reads naturally without "on undefined").
 *   - Invalid date strings -> empty string (Date constructor returns
 *     a NaN-time Date when given an unparseable input).
 *   - Exceptions from toLocaleDateString in unusual environments ->
 *     empty string (defensive try/catch; in practice modern browsers
 *     do not throw here).
 *
 * Locale: passes `undefined` to use the user agent's preferred locale
 * (the standard idiom for Intl.DateTimeFormat APIs). This matches
 * other date renders elsewhere in the SPA so format is consistent.
 *
 * Note: ConnectionDuplicateCheckResponseSchema validates this field
 * as `z.string().datetime({ offset: true }).nullable()` so in the
 * happy path the value is always a valid ISO 8601 string with a UTC
 * offset; the defensive paths guard against schema-bypass scenarios
 * (e.g., rendering with a stub object in a Storybook entry) and
 * preserve a graceful degradation rather than rendering "Invalid
 * Date" or crashing with TypeError.
 */
function formatSubmissionDate(iso: string | null | undefined): string {
  if (!iso) {
    return "";
  }
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return date.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// DuplicateWarning component
// ---------------------------------------------------------------------------

/**
 * Inline non-blocking warning banner.
 *
 * Renders an amber `<aside>` containing:
 *   - A leading AlertTriangle icon (decorative; aria-hidden).
 *   - A bold lead sentence stating the duplicate was found.
 *   - A descriptive line naming the original owner and the
 *     submission date.
 *   - An in-app link to the existing record (opens in a new tab to
 *     preserve the user's in-progress form state).
 *
 * Renders `null` when `duplicate_found` is `false` so the parent can
 * mount this component unconditionally and let the response value
 * drive visibility (defense-in-depth; the parent typically also
 * gates rendering on the query state).
 *
 * Accessibility:
 *   - `<aside>`: HTML5 semantic for tangentially related content;
 *     correct fit for an inline warning supplementing the primary
 *     form flow without being part of it (WCAG 1.3.1).
 *   - `role="status"` + `aria-live="polite"`: screen readers
 *     announce the banner when it appears mid-form, but politely
 *     queue the announcement so the user is not interrupted while
 *     typing the LinkedIn URL (WCAG 4.1.3 Status Messages). The
 *     gentler `polite` (vs `assertive`) is correct here because the
 *     duplicate is informational, not blocking.
 *   - The leading AlertTriangle icon is `aria-hidden="true"`; the
 *     surrounding text is the accessible name for the banner. The
 *     icon is purely decorative reinforcement.
 *   - The "View existing connection" link uses `<Link>` from
 *     react-router-dom for SPA-correct navigation; `target="_blank"`
 *     opens it in a new tab to preserve form state, and
 *     `rel="noopener noreferrer"` prevents the new tab from
 *     accessing `window.opener` (security best practice always
 *     required with `target="_blank"`).
 *   - The link receives a visible :focus-visible outline ring so
 *     keyboard navigation has a clear focus indicator that matches
 *     the amber palette of the surrounding banner.
 *
 * Defensive rendering:
 *   - When `existing_owner_display_name` is missing (null/empty), the
 *     copy falls back to "a teammate" so the banner still reads
 *     naturally.
 *   - When `existing_submission_date` is missing or unparseable, the
 *     "on <date>" clause is omitted entirely (no orphan "on" word).
 *   - When `existing_record_id` is missing, the "View existing
 *     connection" link is omitted entirely (no broken link to
 *     /connections/null).
 *
 * @example Inside AddEditConnectionForm.tsx
 *   const dup = useDuplicateCheckQuery(linkedinUrl);
 *   {dup.data ? <DuplicateWarning result={dup.data} className="mt-2" /> : null}
 */
export function DuplicateWarning({ result, className }: DuplicateWarningProps): JSX.Element | null {
  // Defense-in-depth: when the parent forgets to gate this on
  // duplicate_found, the early null-return ensures we render
  // nothing rather than an empty banner.
  if (!result.duplicate_found) {
    return null;
  }

  // Resolve display fields with defensive fallbacks. The schema
  // declares each existing_* field as nullable; in practice the
  // server populates them whenever duplicate_found is true, but the
  // SPA must not crash if the contract drifts.
  const ownerName: string =
    result.existing_owner_display_name && result.existing_owner_display_name.length > 0
      ? result.existing_owner_display_name
      : "a teammate";
  const formattedDate: string = formatSubmissionDate(result.existing_submission_date);
  const recordId: string | null = result.existing_record_id;

  return (
    <aside
      role="status"
      aria-live="polite"
      className={clsx(
        // Layout: leading icon + body column.
        "flex items-start gap-3",
        // Surface: amber-tinted card with rounded corners and a
        // subtle border. Mirrors Toast.tsx warning variant so
        // visual language across warning surfaces is consistent.
        "rounded-md border border-amber-200 bg-amber-50",
        // Spacing + typography.
        "px-4 py-3 text-sm text-amber-900",
        // Consumer-supplied class overrides come last so they win
        // when conflicting Tailwind utilities are passed in.
        className,
      )}
      data-testid="duplicate-warning"
    >
      <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
      <div className="min-w-0 flex-1">
        <p className="font-medium leading-5">This LinkedIn profile is already in the team feed.</p>
        <p className="mt-1 text-xs leading-5 text-amber-800">
          <span className="font-medium">{ownerName}</span> added this connection
          {formattedDate ? ` on ${formattedDate}` : ""}. You can still submit, but consider
          coordinating with {ownerName} first.
        </p>
        {recordId ? (
          <p className="mt-2">
            <Link
              to={`/connections/${recordId}`}
              target="_blank"
              rel="noopener noreferrer"
              className={clsx(
                // Inline-flex so the trailing ExternalLink icon sits
                // on the same baseline as the link text.
                "inline-flex items-center gap-1",
                // Typography: small, medium-weight, amber to match
                // the banner palette.
                "text-xs font-medium text-amber-900",
                // Underline reveal on hover keeps the link
                // discoverable without competing with the banner
                // copy at rest.
                "underline-offset-2 hover:underline",
                // Keyboard-only focus ring (matches Toast and other
                // primitives' :focus-visible convention).
                "focus-visible:outline focus-visible:outline-2",
                "focus-visible:outline-offset-2 focus-visible:outline-amber-700",
                // Rounded so the focus ring traces a softer shape
                // around the inline link.
                "rounded-sm",
              )}
              data-testid="duplicate-warning-link"
            >
              View existing connection
              <ExternalLink aria-hidden="true" className="h-3 w-3 shrink-0" />
            </Link>
          </p>
        ) : null}
      </div>
    </aside>
  );
}
