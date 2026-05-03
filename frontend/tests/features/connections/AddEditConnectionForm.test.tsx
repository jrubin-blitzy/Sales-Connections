/**
 * AddEditConnectionForm.test.tsx - Vitest tests for F-001/F-002/F-003/F-007/F-008/F-010.
 *
 * Targets `frontend/src/features/connections/AddEditConnectionForm.tsx`,
 * the most complex feature component in the SPA. This suite exercises:
 *
 *   - F-001 Connection Idea Form: rendering, Zod validation, server 422
 *           field-error mapping, owner-attribution permanence (no
 *           owner_* fields ever leave the SPA per AAP Sec 0.7.4).
 *   - F-002 AI Note Generation: success path populating ai_notes,
 *           soft-failure inline banner (504 ai_timeout / 503 ai_unavailable),
 *           hard-failure suppression of the banner (500/422), retry from
 *           the banner, and form submission unaffected by AI failure.
 *   - F-003 Involvement Indicator: three-button single-select with
 *           aria-pressed reflecting selection state.
 *   - F-007 Record Editing: edit-mode hydration loading/error/success,
 *           edit submit navigates to detail (NOT feed).
 *   - F-008 Tagging: tag autocomplete loading state and tag selection
 *           propagating to POST body.
 *   - F-010 Duplicate LinkedIn Detection: debounced duplicate-check
 *           query showing a non-blocking warning banner (per AAP Sec 0.7.6
 *           "Duplicate detection is a warning, not a block").
 *   - RBAC visibility of the Generate-AI button across all four role
 *           scenarios (null / Admin / Contributor / Viewer).
 *   - Cancel navigation: create-mode -> /feed; edit-mode -> /connections/:id.
 *
 * Test infrastructure:
 *   - vi.mock('react-router-dom', ...) replaces useNavigate and useParams
 *     with stable spies so navigation can be asserted via
 *     `expect(navigateMock).toHaveBeenCalledWith(...)` without the
 *     ceremony of a sibling LocationCapture component. The mock uses
 *     `vi.importActual` so MemoryRouter, Routes, Route, etc. continue to
 *     work for the edit-mode <Routes> wrapper.
 *   - renderWithMockedSession provides a synchronous role override so
 *     the canGenerateAi gate (Admin || Contributor) can be tested
 *     without waiting for /api/me to settle.
 *   - MSW handlers under `overrides.notes`, `overrides.connections`, and
 *     `overrides.duplicateCheck` swap the default-success behavior for
 *     specific failure branches per test.
 *
 * Coverage budget per AAP Sec 0.7.7: this suite must drive
 * AddEditConnectionForm.tsx to >=85% statements/lines/functions and
 * >=80% branches. The 30+ tests below traverse every conditional branch
 * in the component (canGenerateAi, isHydrating, recordQuery.isError,
 * aiSoftFailure, duplicateQuery.data?.duplicate_found, the create vs.
 * edit submit branches, the soft- vs hard-AI-failure branches, and the
 * cancel-create vs. cancel-edit branches).
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (project Prettier config); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - No emoji; no console.log.
 *   - All interactions use userEvent (NOT fireEvent).
 *   - Tests assert observable behavior (DOM, navigation, network),
 *     never implementation details.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { Routes, Route } from "react-router-dom";
import { http, HttpResponse } from "msw";

// ---------------------------------------------------------------------------
// react-router-dom partial mock (useNavigate + useParams spies)
// ---------------------------------------------------------------------------
//
// vi.mock is hoisted ABOVE all imports by Vitest, so the mock is in
// place before AddEditConnectionForm.tsx is loaded. We use
// `vi.importActual` to retain MemoryRouter/Routes/Route/Link/etc., so
// that the rest of the test infrastructure (renderWithMockedSession's
// MemoryRouter wrap, the edit-mode <Routes><Route /></Routes> harness,
// and DuplicateWarning's <Link>) continues to function normally.
//
// Per the AAP guidance, `navigateMock` is captured as a module-level
// vi.fn() and reset in beforeEach so cross-test pollution is impossible.
// `mockParams` is a mutable record set per-test BEFORE render so
// useParams() inside the component returns the seeded id.

const navigateMock = vi.fn();
let mockParams: Record<string, string | undefined> = {};

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => mockParams,
  };
});

// ---------------------------------------------------------------------------
// Test infrastructure imports (AFTER vi.mock so the mock is applied)
// ---------------------------------------------------------------------------

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeConnectionRead, makeSessionRead, makeTagRead, makeUserRead } from "../../mocks/data";
import { renderWithMockedSession, screen, waitFor, within, userEvent } from "../../test-utils";
import { AddEditConnectionForm } from "@/features/connections/AddEditConnectionForm";

// ---------------------------------------------------------------------------
// Test fixtures and helpers
// ---------------------------------------------------------------------------

/**
 * Build a session for the given role with consistent display_name and
 * email derived from the role name. Centralizes session construction
 * for the suite so role-driven tests read like prose.
 */
function sessionForRole(role: "Admin" | "Contributor" | "Viewer") {
  return makeSessionRead({
    user: makeUserRead({
      id: "session-user-1",
      role,
      display_name: `${role} User`,
      email: `${role.toLowerCase()}@example.com`,
    }),
    authenticated: true,
  });
}

/**
 * Default valid form payload used by happy-path tests. Returning a
 * fresh object per call so tests that mutate the result do not affect
 * each other.
 */
function validFormData() {
  return {
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane-doe",
    company: "Acme Corp",
    job_title: "VP Operations",
    relationship_context:
      "We went to college together; she's now VP of Ops at a Series B logistics startup.",
  };
}

/**
 * Fill all required fields with valid data and select Warm Intro.
 * Used by happy-path tests to reach the submittable state quickly.
 */
async function fillValidForm(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const data = validFormData();
  await user.type(screen.getByTestId("form-full-name"), data.full_name);
  await user.type(screen.getByTestId("form-linkedin-url"), data.linkedin_url);
  await user.type(screen.getByTestId("form-company"), data.company);
  await user.type(screen.getByTestId("form-job-title"), data.job_title);
  await user.type(screen.getByTestId("form-relationship-context"), data.relationship_context);
  await user.click(screen.getByTestId("form-involvement-Warm Intro"));
}

// ---------------------------------------------------------------------------
// Top-level test suite
// ---------------------------------------------------------------------------

describe("<AddEditConnectionForm />", () => {
  beforeEach(() => {
    // Reset the per-test mocks so navigation and route params do not
    // leak across tests.
    navigateMock.mockClear();
    mockParams = {};
  });

  // ------------------------------------------------------------------------
  // Phase 2A - Create mode render
  // ------------------------------------------------------------------------
  describe("Create mode - render", () => {
    it("renders all 9 form fields, controls, and the heading", async () => {
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );

      // The form is visible immediately because create mode never
      // hits the isHydrating guard.
      await screen.findByTestId("connection-form");

      // The four short Inputs.
      expect(screen.getByTestId("form-full-name")).toBeInTheDocument();
      expect(screen.getByTestId("form-linkedin-url")).toBeInTheDocument();
      expect(screen.getByTestId("form-company")).toBeInTheDocument();
      expect(screen.getByTestId("form-job-title")).toBeInTheDocument();

      // Relationship-context textarea + AI notes input.
      expect(screen.getByTestId("form-relationship-context")).toBeInTheDocument();
      expect(screen.getByTestId("form-ai-notes")).toBeInTheDocument();

      // Three involvement buttons.
      expect(screen.getByTestId("form-involvement-Warm Intro")).toBeInTheDocument();
      expect(screen.getByTestId("form-involvement-Soft Reference")).toBeInTheDocument();
      expect(screen.getByTestId("form-involvement-Target Only")).toBeInTheDocument();

      // Tag input.
      expect(screen.getByTestId("tag-input")).toBeInTheDocument();

      // Generate AI button visible for Contributor.
      expect(screen.getByTestId("form-generate-ai-button")).toBeInTheDocument();

      // Submit and Cancel.
      const submitButton = screen.getByTestId("form-submit-button");
      expect(submitButton).toHaveTextContent(/add connection/i);
      expect(screen.getByTestId("form-cancel-button")).toBeInTheDocument();

      // Top-level heading.
      expect(
        screen.getByRole("heading", { level: 1, name: /add connection/i }),
      ).toBeInTheDocument();
    });

    it("starts with all fields empty and no involvement selected", async () => {
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      expect(screen.getByTestId("form-full-name")).toHaveValue("");
      expect(screen.getByTestId("form-linkedin-url")).toHaveValue("");
      expect(screen.getByTestId("form-company")).toHaveValue("");
      expect(screen.getByTestId("form-job-title")).toHaveValue("");
      expect(screen.getByTestId("form-relationship-context")).toHaveValue("");
      expect(screen.getByTestId("form-ai-notes")).toHaveValue("");

      // No involvement button is in the pressed state initially.
      expect(screen.getByTestId("form-involvement-Warm Intro")).toHaveAttribute(
        "aria-pressed",
        "false",
      );
      expect(screen.getByTestId("form-involvement-Soft Reference")).toHaveAttribute(
        "aria-pressed",
        "false",
      );
      expect(screen.getByTestId("form-involvement-Target Only")).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    });

    it("renders no field errors before the first submit attempt", async () => {
      // The submitAttempted flag gates inline error visibility; even
      // with an empty form, no errors should be visible until the user
      // clicks Submit.
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // No error text should be visible. We assert by sampling several
      // expected error message fragments.
      expect(screen.queryByText(/full name is required/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/linkedin url is required/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/company is required/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/job title is required/i)).not.toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2B - Validation (F-001)
  // ------------------------------------------------------------------------
  describe("Create mode - validation (F-001)", () => {
    it("shows multiple inline errors after submitting an empty form", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.click(screen.getByTestId("form-submit-button"));

      // Each required field renders its Zod error inline. We check
      // text fragments rather than exact strings so wording changes
      // upstream do not break the test surface.
      await waitFor(() => {
        expect(screen.getByText(/full name is required/i)).toBeInTheDocument();
      });
      expect(screen.getByText(/linkedin url is required/i)).toBeInTheDocument();
      expect(screen.getByText(/company is required/i)).toBeInTheDocument();
      expect(screen.getByText(/job title is required/i)).toBeInTheDocument();
      expect(screen.getByText(/relationship context is required/i)).toBeInTheDocument();
      // Involvement enum error text.
      expect(screen.getByText(/involvement must be one of/i)).toBeInTheDocument();

      // The submit did not propagate to a navigation.
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("rejects an invalid LinkedIn URL with an inline message", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // Fill all required fields except linkedin_url, which gets a
      // syntactically-invalid string.
      await user.type(screen.getByTestId("form-full-name"), "Jane Doe");
      await user.type(screen.getByTestId("form-linkedin-url"), "not-a-valid-url");
      await user.type(screen.getByTestId("form-company"), "Acme Corp");
      await user.type(screen.getByTestId("form-job-title"), "VP Operations");
      await user.type(
        screen.getByTestId("form-relationship-context"),
        "We worked together at FooCorp.",
      );
      await user.click(screen.getByTestId("form-involvement-Warm Intro"));

      await user.click(screen.getByTestId("form-submit-button"));

      // The LinkedIn URL refine error text mentions "linkedin profile URL".
      await waitFor(() => {
        expect(screen.getByText(/linkedin profile url/i)).toBeInTheDocument();
      });

      // Submit did not navigate.
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("clears a field's inline error as soon as the user types into it", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // Trigger validation.
      await user.click(screen.getByTestId("form-submit-button"));
      await waitFor(() => {
        expect(screen.getByText(/full name is required/i)).toBeInTheDocument();
      });

      // Type a single character into the full_name field.
      await user.type(screen.getByTestId("form-full-name"), "J");

      // The full_name error should disappear (errors clear optimistically).
      await waitFor(() => {
        expect(screen.queryByText(/full name is required/i)).not.toBeInTheDocument();
      });

      // Other required-field errors remain visible.
      expect(screen.getByText(/linkedin url is required/i)).toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2C - Create mode submit happy path
  // ------------------------------------------------------------------------
  describe("Create mode - submit happy path", () => {
    it("posts the full payload and navigates to /feed on success", async () => {
      const user = userEvent.setup();

      // Capture the request body so we can inspect for owner-attribution
      // permanence (AAP Sec 0.7.4: owner_* fields are server-derived
      // and must NEVER appear in the request body).
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.post("/api/connections", async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            makeConnectionRead({
              full_name:
                typeof capturedBody.full_name === "string" ? capturedBody.full_name : "Jane Doe",
            }),
            { status: 201 },
          );
        }),
      );

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await fillValidForm(user);
      await user.click(screen.getByTestId("form-submit-button"));

      // Navigation happened on success.
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });

      // The captured body matches the typed inputs. Owner attribution
      // is server-derived and absent from the payload.
      expect(capturedBody).not.toBeNull();
      const body = capturedBody as unknown as Record<string, unknown>;
      expect(body.full_name).toBe("Jane Doe");
      expect(body.linkedin_url).toBe("https://www.linkedin.com/in/jane-doe");
      expect(body.company).toBe("Acme Corp");
      expect(body.job_title).toBe("VP Operations");
      expect(body.involvement).toBe("Warm Intro");
      // Owner-attribution permanence: NEVER sent client-side.
      expect(body.owner_user_id).toBeUndefined();
      expect(body.owner_display_name).toBeUndefined();
    });

    it("submits successfully without ai_notes (the field is optional)", async () => {
      const user = userEvent.setup();

      // Default POST handler returns 201; no override needed.
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await fillValidForm(user);
      // Intentionally leave form-ai-notes empty.
      await user.click(screen.getByTestId("form-submit-button"));

      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2D - 422 server validation
  // ------------------------------------------------------------------------
  describe("Create mode - 422 server validation", () => {
    it("maps a 422 server response into a per-field inline error", async () => {
      const user = userEvent.setup();
      // Server returns a 422 with a duplicate-normalized error on
      // linkedin_url; the form should map this into an inline error.
      const serverMessage = "A connection with this LinkedIn already exists.";
      server.use(
        overrides.connections.create422([{ field: "linkedin_url", message: serverMessage }]),
      );

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await fillValidForm(user);
      await user.click(screen.getByTestId("form-submit-button"));

      // The inline error from the server propagates into the
      // linkedin_url field's helper text.
      await waitFor(() => {
        expect(screen.getByText(serverMessage)).toBeInTheDocument();
      });

      // Navigation never happened.
      expect(navigateMock).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2E - AI Note Generation (F-002)
  // ------------------------------------------------------------------------
  describe("AI note generation (F-002)", () => {
    it("disables the Generate AI button when relationship_context is empty", async () => {
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      expect(screen.getByTestId("form-generate-ai-button")).toBeDisabled();
    });

    it("enables the Generate AI button once relationship_context has text", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "Some context");

      expect(screen.getByTestId("form-generate-ai-button")).not.toBeDisabled();
    });

    it("populates the ai_notes textarea with the AI response on success", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // The default MSW handler echoes back the prefix
      // "Mock AI talking points based on: ..." so we assert on that.
      // toHaveValue accepts only strings/numbers, so we read the input
      // element's `.value` and assert via toContain.
      await user.type(
        screen.getByTestId("form-relationship-context"),
        "We met at a conference in NYC.",
      );
      await user.click(screen.getByTestId("form-generate-ai-button"));

      await waitFor(() => {
        const aiNotesField = screen.getByTestId("form-ai-notes") as HTMLInputElement;
        expect(aiNotesField.value).toContain("Mock AI talking points based on");
      });
    });

    it("changes the button label to 'Regenerate AI notes' after success", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "Some context");

      // Initial label.
      expect(screen.getByTestId("form-generate-ai-button")).toHaveTextContent(/generate ai notes/i);

      await user.click(screen.getByTestId("form-generate-ai-button"));

      // After success, the label changes.
      await waitFor(() => {
        expect(screen.getByTestId("form-generate-ai-button")).toHaveTextContent(
          /regenerate ai notes/i,
        );
      });
    });

    it("allows the user to edit the AI-generated text in place", async () => {
      const user = userEvent.setup();
      // Use a short, predictable success payload so the assertion
      // string is stable.
      server.use(overrides.notes.success("Initial AI text."));

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      await waitFor(() => {
        expect(screen.getByTestId("form-ai-notes")).toHaveValue("Initial AI text.");
      });

      // Append additional text directly to the textarea.
      await user.type(screen.getByTestId("form-ai-notes"), " Plus my own additions.");

      expect(screen.getByTestId("form-ai-notes")).toHaveValue(
        "Initial AI text. Plus my own additions.",
      );
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2F - AI SOFT failure (F-002)
  // ------------------------------------------------------------------------
  describe("AI soft failure (F-002)", () => {
    it("shows the inline soft-failure banner on 504 ai_timeout", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.timeout504());

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      // Banner appears.
      const banner = await screen.findByTestId("form-ai-soft-failure");

      // Banner is announced with role="status" (NOT role="alert" which
      // is reserved for hard failures).
      expect(banner).toHaveAttribute("role", "status");
      expect(banner).toHaveTextContent(/ai unavailable/i);

      // Retry button is present and accessible by aria-label.
      const retryButton = within(banner).getByRole("button", { name: /retry ai generation/i });
      expect(retryButton).toBeInTheDocument();

      // Submit button is still clickable (form is not blocked).
      expect(screen.getByTestId("form-submit-button")).not.toBeDisabled();
    });

    it("shows the inline soft-failure banner on 503 ai_unavailable", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.unavailable503());

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      const banner = await screen.findByTestId("form-ai-soft-failure");
      expect(banner).toHaveAttribute("role", "status");
      expect(banner).toHaveTextContent(/ai unavailable/i);
    });

    it("retries AI generation when the banner's retry button is clicked", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.timeout504());

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      // First attempt produces the soft-failure banner.
      const banner = await screen.findByTestId("form-ai-soft-failure");

      // Swap the handler to success for the retry.
      server.use(overrides.notes.success("Retry text generated"));

      const retryButton = within(banner).getByRole("button", {
        name: /retry ai generation/i,
      });
      await user.click(retryButton);

      // Banner disappears (mutation is no longer in error state).
      await waitFor(() => {
        expect(screen.queryByTestId("form-ai-soft-failure")).not.toBeInTheDocument();
      });

      // ai_notes is populated with the retry payload.
      await waitFor(() => {
        expect(screen.getByTestId("form-ai-notes")).toHaveValue("Retry text generated");
      });
    });

    it("permits successful submit even after a soft AI failure (non-blocking)", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.timeout504());
      // Default POST /api/connections returns 201.

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await fillValidForm(user);

      // Trigger AI failure.
      await user.click(screen.getByTestId("form-generate-ai-button"));
      await screen.findByTestId("form-ai-soft-failure");

      // Type custom AI notes manually.
      await user.type(screen.getByTestId("form-ai-notes"), "Manual notes here");

      await user.click(screen.getByTestId("form-submit-button"));

      // Submit succeeded; navigation occurred.
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2G - AI HARD failure (F-002)
  // ------------------------------------------------------------------------
  describe("AI hard failure (F-002)", () => {
    it("does NOT show the soft-failure banner on a 500 server error", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.error500());

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      // Wait long enough for the mutation to settle.
      await waitFor(() => {
        // The button label remains "Generate AI notes" because no data
        // arrived (data is undefined => button text stays at the
        // initial label).
        expect(screen.getByTestId("form-generate-ai-button")).toHaveTextContent(
          /generate ai notes/i,
        );
      });

      // The soft-failure banner is NEVER displayed for hard errors.
      expect(screen.queryByTestId("form-ai-soft-failure")).not.toBeInTheDocument();

      // Submit button is still clickable.
      expect(screen.getByTestId("form-submit-button")).not.toBeDisabled();
    });

    it("does NOT show the soft-failure banner on a 422 validation error", async () => {
      const user = userEvent.setup();
      server.use(overrides.notes.validation422());

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(screen.getByTestId("form-relationship-context"), "context");
      await user.click(screen.getByTestId("form-generate-ai-button"));

      // Mutation completes; no soft-failure banner.
      await waitFor(() => {
        expect(screen.getByTestId("form-generate-ai-button")).not.toBeDisabled();
      });

      expect(screen.queryByTestId("form-ai-soft-failure")).not.toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2H - Duplicate detection (F-010)
  // ------------------------------------------------------------------------
  describe("Duplicate detection (F-010)", () => {
    it("renders the DuplicateWarning banner when a duplicate is found", async () => {
      const user = userEvent.setup();
      server.use(overrides.duplicateCheck.duplicateFound("record-123", "Alice Admin"));

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // Type a LinkedIn URL into the form. The 400ms debounce + MSW
      // round-trip lands well under the 2-second waitFor timeout.
      await user.type(
        screen.getByTestId("form-linkedin-url"),
        "https://www.linkedin.com/in/jane-doe",
      );

      const banner = await screen.findByTestId("duplicate-warning", undefined, { timeout: 2000 });
      expect(banner).toHaveAttribute("role", "status");
      // Owner name from the override surfaces in the banner.
      expect(banner).toHaveTextContent(/alice admin/i);
      // Banner contains the canonical "team feed" copy.
      expect(banner).toHaveTextContent(/already in the team feed/i);

      // External link target.
      const link = within(banner).getByTestId("duplicate-warning-link");
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    });

    it("does NOT block submit when a duplicate is found (non-blocking warning)", async () => {
      const user = userEvent.setup();
      server.use(overrides.duplicateCheck.duplicateFound("record-123", "Alice Admin"));

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await fillValidForm(user);

      // Wait for the duplicate warning to surface.
      await screen.findByTestId("duplicate-warning", undefined, { timeout: 2000 });

      // Submit succeeds; the form does NOT prevent it.
      await user.click(screen.getByTestId("form-submit-button"));
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });
    });

    it("does NOT render the DuplicateWarning banner when not duplicate", async () => {
      const user = userEvent.setup();

      // Use a counter-based handler so we can deterministically wait
      // until the duplicate-check query has resolved at least once.
      // This avoids relying on a raw setTimeout (which leaks React
      // state updates outside the test's act boundary).
      let duplicateCheckCalls = 0;
      server.use(
        http.get("/api/connections/duplicate-check", ({ request }) => {
          duplicateCheckCalls += 1;
          const url = new URL(request.url);
          const linkedinUrl = url.searchParams.get("linkedin_url") ?? "";
          // Default response shape: duplicate_found=false.
          return HttpResponse.json({
            duplicate_found: false,
            existing_record_id: null,
            existing_owner_display_name: null,
            existing_submission_date: null,
            normalized_linkedin_url: linkedinUrl.toLowerCase(),
          });
        }),
      );

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.type(
        screen.getByTestId("form-linkedin-url"),
        "https://www.linkedin.com/in/jane-doe",
      );

      // Wait until the debounced query has fired and resolved at least
      // once. This is deterministic and stays inside React's act
      // boundary.
      await waitFor(
        () => {
          expect(duplicateCheckCalls).toBeGreaterThan(0);
        },
        { timeout: 2000 },
      );

      // After the query resolves with duplicate_found=false, the banner
      // is NEVER rendered.
      expect(screen.queryByTestId("duplicate-warning")).not.toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2I - Involvement single-select (F-003)
  // ------------------------------------------------------------------------
  describe("Involvement single-select (F-003)", () => {
    it("toggles aria-pressed correctly when switching between options", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      const warmIntro = screen.getByTestId("form-involvement-Warm Intro");
      const softReference = screen.getByTestId("form-involvement-Soft Reference");
      const targetOnly = screen.getByTestId("form-involvement-Target Only");

      // Initially nothing is selected.
      expect(warmIntro).toHaveAttribute("aria-pressed", "false");
      expect(softReference).toHaveAttribute("aria-pressed", "false");
      expect(targetOnly).toHaveAttribute("aria-pressed", "false");

      // Click Warm Intro.
      await user.click(warmIntro);
      expect(warmIntro).toHaveAttribute("aria-pressed", "true");
      expect(softReference).toHaveAttribute("aria-pressed", "false");
      expect(targetOnly).toHaveAttribute("aria-pressed", "false");

      // Click Soft Reference - exclusivity holds.
      await user.click(softReference);
      expect(warmIntro).toHaveAttribute("aria-pressed", "false");
      expect(softReference).toHaveAttribute("aria-pressed", "true");
      expect(targetOnly).toHaveAttribute("aria-pressed", "false");

      // Click Target Only - same.
      await user.click(targetOnly);
      expect(warmIntro).toHaveAttribute("aria-pressed", "false");
      expect(softReference).toHaveAttribute("aria-pressed", "false");
      expect(targetOnly).toHaveAttribute("aria-pressed", "true");
    });

    it("renders an InvolvementBadge inside each involvement button", async () => {
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // Each button contains the badge text. The InvolvementBadge
      // renders the value as visible text inside the button.
      const warmButton = screen.getByTestId("form-involvement-Warm Intro");
      expect(warmButton).toHaveTextContent(/warm intro/i);
      const softButton = screen.getByTestId("form-involvement-Soft Reference");
      expect(softButton).toHaveTextContent(/soft reference/i);
      const targetButton = screen.getByTestId("form-involvement-Target Only");
      expect(targetButton).toHaveTextContent(/target only/i);
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2J - Tag selection (F-008)
  // ------------------------------------------------------------------------
  describe("Tag selection (F-008)", () => {
    it("disables the tag input while the tags query is pending", async () => {
      // Use a never-resolving handler to keep the query in pending
      // state for the duration of the test.
      server.use(http.get("/api/tags", () => new Promise(() => undefined)));

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // The tag input field is disabled while tagsQuery.isPending.
      expect(screen.getByTestId("tag-input-field")).toBeDisabled();
    });

    it("includes the selected tag id in the POST body on submit", async () => {
      const user = userEvent.setup();

      // Seed a single, predictable tag so the suggestion id is known.
      const fixedTag = makeTagRead({ name: "industry:saas" });
      server.use(http.get("/api/tags", () => HttpResponse.json([fixedTag])));

      // Capture the request body so we can assert on tag_ids.
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.post("/api/connections", async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(makeConnectionRead({}), { status: 201 });
        }),
      );

      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      // Wait for the tag input to become enabled (tagsQuery resolved).
      await waitFor(() => {
        expect(screen.getByTestId("tag-input-field")).not.toBeDisabled();
      });

      // Open the autocomplete listbox by focusing the field and typing.
      await user.click(screen.getByTestId("tag-input-field"));
      await user.type(screen.getByTestId("tag-input-field"), "saas");

      // Click the suggestion. TagInput uses onMouseDown to add tags
      // (preventing input blur), so userEvent.click provides the
      // matching mousedown event.
      const suggestion = await screen.findByTestId(`tag-suggestion-${fixedTag.id}`);
      await user.click(suggestion);

      // The tag chip appears in the well.
      await waitFor(() => {
        expect(screen.getByTestId(`tag-chip-${fixedTag.id}`)).toBeInTheDocument();
      });

      // Fill the rest of the form and submit.
      await fillValidForm(user);
      await user.click(screen.getByTestId("form-submit-button"));

      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });

      // Inspect the captured POST body.
      expect(capturedBody).not.toBeNull();
      const body = capturedBody as unknown as { tag_ids?: ReadonlyArray<string> };
      expect(body.tag_ids).toContain(fixedTag.id);
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2K - RBAC: Generate AI button visibility
  // ------------------------------------------------------------------------
  describe("RBAC - Generate AI button visibility", () => {
    it("hides the Generate AI button when no session is present", async () => {
      renderWithMockedSession(<AddEditConnectionForm mode="create" />, null);
      await screen.findByTestId("connection-form");

      expect(screen.queryByTestId("form-generate-ai-button")).not.toBeInTheDocument();
    });

    it("shows the Generate AI button for the Admin role", async () => {
      renderWithMockedSession(<AddEditConnectionForm mode="create" />, sessionForRole("Admin"));
      await screen.findByTestId("connection-form");

      expect(screen.getByTestId("form-generate-ai-button")).toBeInTheDocument();
    });

    it("shows the Generate AI button for the Contributor role", async () => {
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      expect(screen.getByTestId("form-generate-ai-button")).toBeInTheDocument();
    });

    it("hides the Generate AI button for the Viewer role", async () => {
      renderWithMockedSession(<AddEditConnectionForm mode="create" />, sessionForRole("Viewer"));
      await screen.findByTestId("connection-form");

      expect(screen.queryByTestId("form-generate-ai-button")).not.toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2L - Edit mode hydration
  // ------------------------------------------------------------------------
  describe("Edit mode - hydration", () => {
    it("renders the loading state while the record is being fetched", async () => {
      // Keep the GET /api/connections/:id request pending forever so
      // the form is held in the isHydrating branch.
      server.use(http.get("/api/connections/:id", () => new Promise(() => undefined)));

      // Seed useParams() so editId resolves to a known value.
      mockParams = { id: "abc-123" };

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: ["/connections/abc-123/edit"] },
      );

      const loadingEl = await screen.findByTestId("connection-form-loading");
      expect(loadingEl).toHaveAttribute("role", "status");
      expect(loadingEl).toHaveAttribute("aria-busy", "true");
    });

    it("hydrates the form with fetched data on success", async () => {
      const id = "conn-1";
      mockParams = { id };

      // The default GET /api/connections/:id handler is used; we
      // override the body to control the visible field values.
      server.use(
        http.get(`/api/connections/${id}`, () =>
          HttpResponse.json(
            makeConnectionRead({
              id,
              full_name: "John Smith",
              linkedin_url: "https://www.linkedin.com/in/john-smith",
              company: "Globex",
              job_title: "Director of Engineering",
              relationship_context: "Met at the AI Summit 2025.",
              ai_notes: "Suggest a follow-up about scaling their ML platform.",
              involvement: "Soft Reference",
            }),
          ),
        ),
      );

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: [`/connections/${id}/edit`] },
      );

      await screen.findByTestId("connection-form");

      // The form is hydrated with the record's values.
      await waitFor(() => {
        expect(screen.getByTestId("form-full-name")).toHaveValue("John Smith");
      });
      expect(screen.getByTestId("form-linkedin-url")).toHaveValue(
        "https://www.linkedin.com/in/john-smith",
      );
      expect(screen.getByTestId("form-company")).toHaveValue("Globex");
      expect(screen.getByTestId("form-job-title")).toHaveValue("Director of Engineering");
      expect(screen.getByTestId("form-relationship-context")).toHaveValue(
        "Met at the AI Summit 2025.",
      );
      expect(screen.getByTestId("form-ai-notes")).toHaveValue(
        "Suggest a follow-up about scaling their ML platform.",
      );

      // The selected involvement reflects the hydrated value.
      expect(screen.getByTestId("form-involvement-Soft Reference")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByTestId("form-involvement-Warm Intro")).toHaveAttribute(
        "aria-pressed",
        "false",
      );

      // Submit button label switches to "Save changes".
      expect(screen.getByTestId("form-submit-button")).toHaveTextContent(/save changes/i);

      // Heading also reflects edit mode.
      expect(
        screen.getByRole("heading", { level: 1, name: /edit connection/i }),
      ).toBeInTheDocument();
    });

    it("renders the error fallback when the record fetch fails (404)", async () => {
      const id = "conn-bad";
      mockParams = { id };
      server.use(overrides.connections.detail404(id));

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: [`/connections/${id}/edit`] },
      );

      const errorEl = await screen.findByTestId("connection-form-load-error");
      expect(errorEl).toHaveAttribute("role", "alert");

      // The "Back to feed" button navigates to /feed when clicked.
      const user = userEvent.setup();
      const backButton = within(errorEl).getByRole("button", { name: /back to feed/i });
      await user.click(backButton);
      expect(navigateMock).toHaveBeenCalledWith("/feed");
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2M - Edit mode submit
  // ------------------------------------------------------------------------
  describe("Edit mode - submit", () => {
    it("PATCHes the record and navigates to /connections/:id on success", async () => {
      const user = userEvent.setup();
      const id = "conn-1";
      mockParams = { id };

      // Seed a hydrated record so the form starts populated.
      server.use(
        http.get(`/api/connections/${id}`, () =>
          HttpResponse.json(
            makeConnectionRead({
              id,
              full_name: "Original Name",
              linkedin_url: "https://www.linkedin.com/in/original",
              company: "OldCo",
              job_title: "Engineer",
              relationship_context: "Coworker at OldCo.",
              ai_notes: null,
              involvement: "Warm Intro",
            }),
          ),
        ),
      );

      // Capture the PATCH body to verify the new value is sent.
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.patch(`/api/connections/${id}`, async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            makeConnectionRead({
              id,
              ...(typeof capturedBody.job_title === "string"
                ? { job_title: capturedBody.job_title }
                : {}),
            }),
          );
        }),
      );

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: [`/connections/${id}/edit`] },
      );

      await screen.findByTestId("connection-form");

      // Wait for hydration to complete.
      await waitFor(() => {
        expect(screen.getByTestId("form-full-name")).toHaveValue("Original Name");
      });

      // Modify the job title.
      const jobTitle = screen.getByTestId("form-job-title");
      await user.clear(jobTitle);
      await user.type(jobTitle, "VP of Engineering");

      await user.click(screen.getByTestId("form-submit-button"));

      // Edit-mode submit navigates to detail, NOT feed.
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith(`/connections/${id}`);
      });

      // The captured PATCH body has the new job_title.
      expect(capturedBody).not.toBeNull();
      const body = capturedBody as unknown as Record<string, unknown>;
      expect(body.job_title).toBe("VP of Engineering");
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2M.2 - Edit mode 422 server validation
  // ------------------------------------------------------------------------
  describe("Edit mode - 422 server validation", () => {
    it("maps a PATCH 422 response into a per-field inline error", async () => {
      const user = userEvent.setup();
      const id = "conn-1";
      mockParams = { id };

      // Seed a hydrated record so the form starts populated.
      server.use(
        http.get(`/api/connections/${id}`, () =>
          HttpResponse.json(
            makeConnectionRead({
              id,
              full_name: "Original Name",
              linkedin_url: "https://www.linkedin.com/in/original",
              company: "OldCo",
              job_title: "Engineer",
              relationship_context: "Coworker at OldCo.",
              involvement: "Warm Intro",
            }),
          ),
        ),
      );

      // PATCH returns 422 with a per-field error.
      const serverMessage = "LinkedIn URL conflicts with an existing record.";
      server.use(
        http.patch(`/api/connections/${id}`, () =>
          HttpResponse.json(
            {
              error: {
                code: "validation_error",
                message: "Request validation failed",
                correlation_id: "test-correlation-id",
                fields: [{ field: "linkedin_url", message: serverMessage }],
              },
            },
            { status: 422 },
          ),
        ),
      );

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: [`/connections/${id}/edit`] },
      );

      await screen.findByTestId("connection-form");
      await waitFor(() => {
        expect(screen.getByTestId("form-full-name")).toHaveValue("Original Name");
      });

      // Submit the form (with the existing valid values).
      await user.click(screen.getByTestId("form-submit-button"));

      // The inline error from the 422 response surfaces.
      await waitFor(() => {
        expect(screen.getByText(serverMessage)).toBeInTheDocument();
      });

      // No navigation occurred.
      expect(navigateMock).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------------
  // Phase 2N - Cancel button
  // ------------------------------------------------------------------------
  describe("Cancel button", () => {
    it("navigates to /feed in create mode", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <AddEditConnectionForm mode="create" />,
        sessionForRole("Contributor"),
      );
      await screen.findByTestId("connection-form");

      await user.click(screen.getByTestId("form-cancel-button"));

      expect(navigateMock).toHaveBeenCalledWith("/feed");
    });

    it("navigates to /connections/:id in edit mode", async () => {
      const user = userEvent.setup();
      const id = "conn-1";
      mockParams = { id };

      // Seed the record so hydration completes.
      server.use(
        http.get(`/api/connections/${id}`, () => HttpResponse.json(makeConnectionRead({ id }))),
      );

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id/edit" element={<AddEditConnectionForm mode="edit" />} />
        </Routes>,
        sessionForRole("Admin"),
        { initialEntries: [`/connections/${id}/edit`] },
      );

      await screen.findByTestId("connection-form");

      // Wait for hydration so the cancel button is fully wired.
      await waitFor(() => {
        expect(screen.getByTestId("form-full-name")).not.toHaveValue("");
      });

      await user.click(screen.getByTestId("form-cancel-button"));

      expect(navigateMock).toHaveBeenCalledWith(`/connections/${id}`);
    });
  });
});
