/**
 * DuplicateWarning.test.tsx - Vitest tests for the F-010 Duplicate
 * LinkedIn URL non-blocking warning banner.
 *
 * Targets `frontend/src/features/connections/DuplicateWarning.tsx` -
 * a pure presentational component that renders an amber `<aside>`
 * advisory when the LinkedIn URL the contributor is entering matches
 * an existing record in the same organization. The banner does NOT
 * block submission; it informs the contributor so they can decide
 * whether to coordinate with the existing owner or proceed anyway.
 *
 * The component's authoritative source-of-truth shape is the
 * `ConnectionDuplicateCheckResponse` type from
 * `@/schemas/connection`, which has FLAT optional fields
 * (`existing_record_id`, `existing_owner_display_name`,
 * `existing_submission_date`) at the top level of the response - NOT
 * nested under an `existing_record` object. The
 * `makeDuplicateCheckResponse` factory in `tests/mocks/data.ts`
 * mirrors that flat shape; tests below construct mock responses via
 * the factory so they remain locked to the schema.
 *
 * Coverage goals (>= 85% line, 80% branch, 85% function, 85%
 * statement per AAP Sec 0.7.7 / vite.config.ts thresholds):
 *
 *   1. Conditional rendering
 *      - Returns `null` when `result.duplicate_found === false`.
 *      - Renders the banner when `result.duplicate_found === true`.
 *
 *   2. Banner content
 *      - Headline copy "already in the team feed".
 *      - Owner display name surfaced verbatim.
 *      - "coordinating with <name> first" closing copy.
 *
 *   3. View existing link
 *      - Rendered when `existing_record_id` is non-null; omitted
 *        when null.
 *      - href points to `/connections/<id>`.
 *      - target="_blank" + rel includes "noopener" and "noreferrer"
 *        (security best practice for any external-tab link).
 *
 *   4. Defensive fallbacks
 *      - Missing `existing_owner_display_name` falls back to
 *        "a teammate" so the copy reads naturally.
 *      - Missing `existing_submission_date` omits the "on <date>"
 *        clause entirely (no orphan "on").
 *      - Empty-string `existing_owner_display_name` also falls back
 *        (defense-in-depth: schema permits null but a server with a
 *        coding error could send "").
 *
 *   5. Date formatting
 *      - Renders ISO-8601 dates via `toLocaleDateString` with the
 *        short month / numeric day / numeric year format.
 *      - Gracefully drops the clause for invalid date strings
 *        (formatSubmissionDate returns "").
 *
 *   6. ARIA semantics
 *      - Wrapping element is an <aside> with role="status" and
 *        aria-live="polite" (WCAG 4.1.3 Status Messages).
 *      - The leading AlertTriangle icon carries aria-hidden="true"
 *        (decorative; the surrounding text is the accessible name).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; eslint allows `any` in tests but the
 *     project still avoids it.
 *   - Double quotes; trailing commas; 2-space indent; line length
 *     <= 100.
 *   - No emoji; no console.log; no async/await (the component is
 *     fully synchronous and has no data-fetching effects of its
 *     own).
 *   - jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`,
 *     `toHaveAttribute`, `toContainElement`) are globally registered
 *     via tests/setup.ts.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/DuplicateWarning.tsx
 *     (system under test - the only feature import).
 *   - frontend/tests/mocks/data.ts::makeDuplicateCheckResponse
 *     (factory used to build every `result` prop).
 *   - frontend/tests/test-utils.tsx::renderWithProviders, screen
 *     (provider-stack wrapper plus accessibility-tree query API;
 *     renderWithProviders supplies the MemoryRouter that the
 *     component's <Link> from react-router-dom requires).
 *   - frontend/tests/setup.ts (jest-dom matcher registration).
 *   - frontend/vite.config.ts (declares test.globals: true and the
 *     `@/` path alias).
 */

import { describe, it, expect } from "vitest";

import { DuplicateWarning } from "@/features/connections/DuplicateWarning";
import { makeDuplicateCheckResponse } from "../../mocks/data";
import { renderWithProviders, screen } from "../../test-utils";

// ---------------------------------------------------------------------------
// Module-scoped fixtures
// ---------------------------------------------------------------------------

/**
 * Stable UUID-shaped record ID used as the `existing_record_id`
 * value across every duplicate-found test that needs an in-app link.
 * The literal string is what the component will splice into the
 * `to` prop of the react-router-dom <Link>, producing
 * `/connections/<EXISTING_RECORD_ID>` as the rendered href.
 *
 * Declared module-scoped so the same value is asserted in multiple
 * tests without risk of typos drifting one of them.
 */
const EXISTING_RECORD_ID = "11111111-1111-1111-1111-111111111111";

/**
 * Stable owner display name used in banner-content assertions. The
 * value is intentionally distinct enough that a substring match on
 * "Alice Admin" cannot collide with the static banner copy.
 */
const EXISTING_OWNER = "Alice Admin";

/**
 * Stable ISO-8601 datetime with explicit UTC offset. Mid-month so
 * tests can assert that the rendered locale-date contains "Apr",
 * "15", and "2026" without ambiguity (start-of-month dates can
 * straddle the prior month in some locales because of the offset).
 */
const EXISTING_SUBMISSION_DATE = "2026-04-15T10:00:00+00:00";

/**
 * Stable normalized LinkedIn URL value. The component does not
 * render this string (only the owner name and the date are surfaced
 * in the banner copy), but the schema requires it on every response
 * so the factory still includes it.
 */
const NORMALIZED_LINKEDIN_URL = "https://www.linkedin.com/in/jane-doe";

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<DuplicateWarning />", () => {
  // -------------------------------------------------------------------------
  // 1. Conditional rendering
  // -------------------------------------------------------------------------
  //
  // The component's first responsibility is the early-return null
  // branch when the duplicate-check call returned a "no match"
  // result. Without the early return, the parent form would have to
  // gate rendering itself; the early return lets the parent mount
  // <DuplicateWarning /> unconditionally and trust the response to
  // drive visibility. Both paths are exercised here.

  describe("Conditional rendering", () => {
    it("returns null when duplicate_found is false", () => {
      // The factory's defaults already represent the "no duplicate"
      // case (duplicate_found: false, all existing_* fields null).
      // Passing the explicit override makes the intent obvious to
      // human readers without changing the factory output.
      const result = makeDuplicateCheckResponse({ duplicate_found: false });

      // The container reference is the document fragment React
      // mounted into. When DuplicateWarning returns null, React
      // mounts no DOM nodes - container.firstChild is therefore
      // null. This is the canonical RTL pattern for asserting
      // "rendered nothing".
      const { container } = renderWithProviders(<DuplicateWarning result={result} />);

      expect(container.firstChild).toBeNull();
      // Cross-check: the data-testid on the banner aside is also
      // not present. queryByTestId returns null (rather than
      // throwing) when no match is found, which is exactly what we
      // want for a "should not exist" assertion.
      expect(screen.queryByTestId("duplicate-warning")).toBeNull();
    });

    it("renders the banner when duplicate_found is true", () => {
      // Match-case: the duplicate-check call found an existing
      // record. The component must mount its <aside> with the
      // structural data-testid and accessible role so screen
      // readers announce the new content.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 2. Banner content
  // -------------------------------------------------------------------------
  //
  // Once the banner mounts, it must surface enough information for
  // the contributor to act: the static lead sentence (so the user
  // immediately understands the warning), the owner's display name
  // (so the user knows whom to coordinate with), and the closing
  // call-to-action copy (so the user knows the warning is
  // non-blocking and what to do next). All three are asserted via
  // case-insensitive regexes so minor copy edits do not require
  // test churn beyond the literal text.

  describe("Banner content", () => {
    it("displays the static lead sentence", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // The component renders "This LinkedIn profile is already in
      // the team feed." - a regex on the most stable phrase
      // ("already in the team feed") avoids brittleness if the
      // surrounding boilerplate is reworded in a future copy edit.
      expect(screen.getByText(/already in the team feed/i)).toBeInTheDocument();
    });

    it("surfaces the existing owner's display name", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // The owner name appears at least twice in the banner: once
      // bolded as the actor of the prior submission, and again in
      // the closing "coordinating with <ownerName> first" copy. We
      // assert via the banner's combined textContent rather than a
      // single getByText() to avoid the "multiple matches" failure
      // mode getByText would surface.
      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toHaveTextContent(EXISTING_OWNER);
    });

    it("includes the 'coordinating with' call-to-action copy", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // The closing copy is "You can still submit, but consider
      // coordinating with <ownerName> first." We assert on the
      // distinctive verb phrase to confirm the non-blocking
      // affordance is communicated.
      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toHaveTextContent(/you can still submit/i);
      expect(banner).toHaveTextContent(/coordinating with/i);
    });
  });

  // -------------------------------------------------------------------------
  // 3. View existing link
  // -------------------------------------------------------------------------
  //
  // The "View existing connection" link gives the contributor a
  // safe inspection path: it opens in a NEW tab so the in-progress
  // form state is preserved, it carries the standard
  // rel="noopener noreferrer" pair so the new tab cannot reach back
  // through window.opener, and it routes via react-router-dom's
  // <Link> so SPA navigation rules apply rather than a full-page
  // reload. When the existing record id is missing - a defensive
  // server contract drift - the link is omitted entirely so we do
  // not generate a /connections/null href.

  describe("View existing link", () => {
    it("renders an in-app link when existing_record_id is non-null", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const link = screen.getByTestId("duplicate-warning-link");
      expect(link).toBeInTheDocument();
      // react-router-dom's <Link to="/connections/<id>"> renders an
      // <a href="/connections/<id>">. We assert against the rendered
      // href rather than the React prop so we exercise the actual
      // anchor consumed by the browser's accessibility tree.
      expect(link).toHaveAttribute("href", `/connections/${EXISTING_RECORD_ID}`);
    });

    it("opens the link in a new tab with target=_blank", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const link = screen.getByTestId("duplicate-warning-link");
      expect(link).toHaveAttribute("target", "_blank");
    });

    it("declares rel=noopener noreferrer for the new tab", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const link = screen.getByTestId("duplicate-warning-link");
      // The HTML spec allows a space-separated list of link types,
      // and react-router-dom may forward exactly the string we set.
      // Asserting on substring presence rather than full equality
      // makes the test robust against re-orderings of the values.
      const rel = link.getAttribute("rel") ?? "";
      expect(rel).toContain("noopener");
      expect(rel).toContain("noreferrer");
    });

    it("displays the 'View existing connection' anchor text", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const link = screen.getByTestId("duplicate-warning-link");
      // toHaveTextContent matches the visible text of the link.
      // The trailing ExternalLink lucide icon is aria-hidden so it
      // contributes nothing to the text content; the assertion
      // therefore focuses on the human-readable label.
      expect(link).toHaveTextContent(/view existing connection/i);
    });

    it("omits the link when existing_record_id is null", () => {
      // Server contract drift defense: even though the API normally
      // returns a populated existing_record_id when duplicate_found
      // is true, the schema permits null. The component must NOT
      // render a <Link to="/connections/null"> in that case.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: null,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // The banner itself still mounts (the warning copy is still
      // useful even without the link)...
      expect(screen.getByTestId("duplicate-warning")).toBeInTheDocument();
      // ...but the link is conspicuously absent.
      expect(screen.queryByTestId("duplicate-warning-link")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 4. Defensive fallbacks
  // -------------------------------------------------------------------------
  //
  // Per AAP Sec 0.7.4 ("Server-side validation is authoritative")
  // the SPA must NEVER crash when a server response is incomplete
  // or technically out-of-spec. The DuplicateWarning component
  // resolves missing / empty values to graceful display strings:
  //   - Missing or empty owner -> "a teammate".
  //   - Missing or invalid date -> the "on <date>" clause is
  //     omitted entirely so the surrounding sentence still reads.
  //   - Missing record id -> the link is omitted (covered above).

  describe("Defensive fallbacks", () => {
    it("falls back to 'a teammate' when existing_owner_display_name is null", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: null,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // The fallback string MUST appear...
      expect(banner).toHaveTextContent(/a teammate/i);
      // ...and the original-owner placeholder MUST NOT leak. (Not
      // asserting on the literal "Alice Admin" because that test
      // sets the value to null; we instead assert that the
      // fallback dominates.) Use a precision check that the
      // fallback text appears in BOTH the bold actor span and the
      // closing 'coordinating with' clause.
      const bannerText = banner.textContent ?? "";
      // The phrase "a teammate" appears twice in the rendered
      // copy: once after "<ownerName> added this connection" and
      // once after "coordinating with". Counting occurrences asserts
      // that BOTH sites use the fallback (catches a regression
      // where only one branch was patched).
      const occurrences = bannerText.match(/a teammate/gi) ?? [];
      expect(occurrences.length).toBeGreaterThanOrEqual(2);
    });

    it("falls back to 'a teammate' when existing_owner_display_name is an empty string", () => {
      // Defense-in-depth: the schema permits null OR a non-empty
      // string, but a server with a coding error could send "".
      // The component's check `name.length > 0` guards both cases
      // and this test pins that behaviour.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: "",
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toHaveTextContent(/a teammate/i);
    });

    it("renders without crashing when ALL optional fields are null", () => {
      // The most defensive case: duplicate_found is true but every
      // existing_* field is null. The banner should still mount,
      // the fallback string for owner should appear, no link, and
      // no date clause.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // Banner mounts.
      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toBeInTheDocument();
      // Owner fallback applies.
      expect(banner).toHaveTextContent(/a teammate/i);
      // No link (record id is null).
      expect(screen.queryByTestId("duplicate-warning-link")).toBeNull();
      // No date clause - asserted via NEGATIVE check on " on "
      // surrounded by space delimiters. The component renders
      // " on <date>" only when a valid date exists; absent the
      // date the surrounding sentence reads "<owner> added this
      // connection. You can still submit..." (note the immediate
      // period after "connection"). A sloppy fallback would leave
      // an orphan " on ." or " on undefined."
      const bannerText = banner.textContent ?? "";
      expect(bannerText).not.toMatch(/ on undefined/i);
      expect(bannerText).not.toMatch(/ on null/i);
      expect(bannerText).not.toMatch(/ on \./);
    });
  });

  // -------------------------------------------------------------------------
  // 5. Date formatting
  // -------------------------------------------------------------------------
  //
  // The submission date is rendered via toLocaleDateString with the
  // short month / numeric day / numeric year format. The exact
  // output depends on the test environment's locale, so assertions
  // use locale-flexible regexes that match the universal
  // components (year digits, three-letter month abbreviation, day
  // number). The component's formatSubmissionDate also defends
  // against null and unparseable strings - both are exercised.

  describe("Date formatting", () => {
    it("renders the submission date in a locale-aware short format", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        // Mid-month so the rendered locale-date contains the
        // expected month, day, and year regardless of UTC->local
        // shift quirks. 2026-04-15 in any reasonable locale will
        // render Apr 15, 2026 (en-US), 15 Apr 2026 (en-GB), etc.
        existing_submission_date: "2026-04-15T10:00:00+00:00",
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // Year 2026 is the most stable assertion (no locale will
      // alter the digit string).
      expect(banner).toHaveTextContent(/2026/);
      // Day 15 is also stable across locales.
      expect(banner).toHaveTextContent(/\b15\b/);
      // Month abbreviation: "Apr" appears in en-US, en-GB, and
      // most C-style locales. Using a non-anchored regex makes
      // the assertion robust to surrounding punctuation.
      expect(banner).toHaveTextContent(/Apr/i);
    });

    it("includes the 'on <date>' clause when the date is valid", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: "2026-04-15T10:00:00+00:00",
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // The literal "on " preposition is what binds the date to
      // the sentence. Asserting on its presence (in lower case;
      // the source uses lower-case "on") confirms the conditional
      // clause was emitted.
      expect(banner).toHaveTextContent(/added this connection on /i);
    });

    it("renders without crashing when existing_submission_date is null", () => {
      // formatSubmissionDate returns "" for null input; the
      // surrounding template-literal collapses to an empty string
      // and the "on <date>" clause is omitted entirely.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: null,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toBeInTheDocument();
      // Confirms the "on " preposition is NOT present (no orphan
      // word). The owner name still surfaces, which we use as the
      // smoke-check that the rest of the banner mounted.
      expect(banner).toHaveTextContent(EXISTING_OWNER);
      const bannerText = banner.textContent ?? "";
      expect(bannerText).not.toMatch(/added this connection on /i);
    });

    it("renders without crashing when existing_submission_date is unparseable", () => {
      // The formatSubmissionDate helper catches the NaN-time Date
      // case (`new Date('not-a-real-date').getTime()` is NaN) and
      // returns "". The banner must still mount and the rest of
      // the copy must read naturally.
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: "not-a-real-date",
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      expect(banner).toBeInTheDocument();
      // The literal "Invalid Date" string MUST NOT leak into the
      // rendered copy. (toLocaleDateString returns this string in
      // some browsers when the underlying Date is NaN-time; the
      // formatSubmissionDate helper suppresses it.)
      const bannerText = banner.textContent ?? "";
      expect(bannerText).not.toMatch(/invalid date/i);
      expect(bannerText).not.toMatch(/nan/i);
    });
  });

  // -------------------------------------------------------------------------
  // 6. ARIA semantics
  // -------------------------------------------------------------------------
  //
  // The banner is a status message: not blocking, not assertive,
  // but worth announcing. The component uses the
  // <aside role="status" aria-live="polite"> pattern so screen
  // readers queue the announcement politely after the user finishes
  // typing. The leading AlertTriangle icon is decorative and
  // therefore aria-hidden so it does not compete with the textual
  // accessible name.

  describe("ARIA semantics", () => {
    it("wraps the banner in an <aside> element", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // The DOM element name is uppercase in jsdom; the assertion
      // pins the HTML5 semantic tag chosen by the source.
      expect(banner.tagName).toBe("ASIDE");
    });

    it("declares role=status on the banner", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      // getByRole resolves the implicit role tree (an <aside> with
      // an explicit role="status" is selectable as both
      // 'complementary' and 'status'). We use the explicit role
      // here because that is what assistive technologies will
      // honour.
      const statusEl = screen.getByRole("status");
      expect(statusEl).toBe(screen.getByTestId("duplicate-warning"));
    });

    it("declares aria-live=polite on the banner", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // 'polite' (vs 'assertive') is correct here because the
      // duplicate is informational, not an error. Pinning this
      // behaviour prevents an over-eager future change to
      // 'assertive' that would interrupt the user typing the
      // LinkedIn URL.
      expect(banner).toHaveAttribute("aria-live", "polite");
    });

    it("marks the leading AlertTriangle icon as aria-hidden", () => {
      const result = makeDuplicateCheckResponse({
        duplicate_found: true,
        existing_record_id: EXISTING_RECORD_ID,
        existing_owner_display_name: EXISTING_OWNER,
        existing_submission_date: EXISTING_SUBMISSION_DATE,
        normalized_linkedin_url: NORMALIZED_LINKEDIN_URL,
      });

      renderWithProviders(<DuplicateWarning result={result} />);

      const banner = screen.getByTestId("duplicate-warning");
      // Lucide icons render as inline SVG elements. The first SVG
      // descendant of the banner is the AlertTriangle. We assert
      // it carries aria-hidden="true" so screen readers do not
      // double-announce the icon's implicit name (the surrounding
      // text already provides the accessible name).
      const firstSvg = banner.querySelector("svg");
      expect(firstSvg).not.toBeNull();
      expect(firstSvg).toHaveAttribute("aria-hidden", "true");
    });
  });
});
