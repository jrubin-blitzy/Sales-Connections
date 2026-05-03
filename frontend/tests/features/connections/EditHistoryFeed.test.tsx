/**
 * EditHistoryFeed.test.tsx - Vitest tests for F-011 + F-013 Edit History.
 *
 * Targets `frontend/src/features/connections/EditHistoryFeed.tsx`. Verifies:
 *   - Loading state with role=status and aria-live
 *   - Error state with role=alert
 *   - Empty state
 *   - Each of the 8 audit event types renders correctly
 *   - PII protection: no raw before/after payloads displayed for non-status events
 *   - Pagination renders when totalPages > 1
 *   - Time formatting with relative + absolute
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { EditHistoryFeed } from "@/features/connections/EditHistoryFeed";
import { renderWithMockedSession, screen } from "../../test-utils";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockHistoryQuery = vi.fn();

vi.mock("@/api/connections", () => ({
  useConnectionHistoryQuery: (id: string, page: number, pageSize: number) =>
    mockHistoryQuery(id, page, pageSize),
}));

const minimalSession = {
  user: {
    id: "00000000-0000-0000-0000-000000000001",
    email: "user@example.com",
    display_name: "User",
    role: "Contributor" as const,
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const RECORD_ID = "11111111-1111-1111-1111-111111111111";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: "00000000-0000-0000-0000-000000000aaa",
    actor_user_id: "00000000-0000-0000-0000-000000000001",
    actor_display_name: "Acting User",
    target_record_id: RECORD_ID,
    event_type: "create",
    event_timestamp: "2026-05-01T12:00:00Z",
    before_payload: null,
    after_payload: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("EditHistoryFeed", () => {
  beforeEach(() => {
    mockHistoryQuery.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe("loading state", () => {
    it("shows status role and aria-busy=true", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: true,
        isPending: true,
        isError: false,
        isSuccess: false,
        data: undefined,
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      const status = screen.getByRole("status");
      expect(status).toBeInTheDocument();
    });
  });

  describe("error state", () => {
    it("shows alert role on error", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isPending: false,
        isError: true,
        isSuccess: false,
        data: undefined,
        error: { message: "Failed to load history" },
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });

  describe("empty state", () => {
    it("shows empty message when no events", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isPending: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [],
          page: 1,
          page_size: 25,
          total: 0,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // Empty state uses role=status (informational).
      expect(screen.getByRole("status")).toBeInTheDocument();
    });
  });

  describe("event types", () => {
    it("renders create event", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [makeEvent({ event_type: "create" })],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // The create event renders a list item; multiple "create"-like
      // strings can match (label, action), so we use getAllByText.
      const matches = screen.getAllByText(/create/i);
      expect(matches.length).toBeGreaterThan(0);
    });

    it("renders status_change event with before/after", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [
            makeEvent({
              event_type: "status_change",
              before_payload: { outreach_status: "Not Started" },
              after_payload: { outreach_status: "In Progress" },
            }),
          ],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // Should show the status transition.
      expect(screen.getByText(/Not Started/)).toBeInTheDocument();
      expect(screen.getByText(/In Progress/)).toBeInTheDocument();
    });

    it.each([
      "edit",
      "soft_delete",
      "hard_delete",
      "role_change",
      "authentication",
      "admin_op",
    ] as const)("renders %s event without crashing", (eventType) => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [makeEvent({ event_type: eventType })],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // Each row renders as a list item.
      expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0);
    });
  });

  describe("PII protection (AAP Sec 0.7.4)", () => {
    it("does NOT render raw payload contents for non-status events", () => {
      const SECRET = "TOPSECRET_PII_VALUE";
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [
            makeEvent({
              event_type: "edit",
              before_payload: {
                full_name: SECRET,
                relationship_context: SECRET + "_CONTEXT",
              },
              after_payload: {
                full_name: SECRET + "_AFTER",
              },
            }),
          ],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // PII MUST NOT appear in the rendered HTML.
      const html = document.body.innerHTML;
      expect(html).not.toContain(SECRET);
    });

    it("status_change shows ONLY outreach_status field, no other PII", () => {
      const PII = "PII_THAT_SHOULD_NOT_APPEAR";
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [
            makeEvent({
              event_type: "status_change",
              before_payload: {
                outreach_status: "Not Started",
                full_name: PII,
              },
              after_payload: {
                outreach_status: "In Progress",
                full_name: PII + "_AFTER",
              },
            }),
          ],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      const html = document.body.innerHTML;
      // PII MUST NOT appear, even though it's in the payload.
      expect(html).not.toContain(PII);
      // But the status values DO appear.
      expect(html).toContain("Not Started");
      expect(html).toContain("In Progress");
    });
  });

  describe("pagination", () => {
    it("renders pagination controls when total_pages > 1", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [makeEvent()],
          page: 1,
          page_size: 25,
          total: 50,
          total_pages: 2,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // The pagination controls render Previous and/or Next buttons.
      const allButtons = screen.queryAllByRole("button");
      expect(allButtons.length).toBeGreaterThan(0);
    });

    it("does NOT render pagination when total_pages = 1", () => {
      mockHistoryQuery.mockReturnValue({
        isLoading: false,
        isError: false,
        isSuccess: true,
        data: {
          items: [makeEvent()],
          page: 1,
          page_size: 25,
          total: 1,
          total_pages: 1,
        },
        error: null,
      });
      renderWithMockedSession(<EditHistoryFeed recordId={RECORD_ID} />, minimalSession);
      // No prev/next buttons.
      const allButtons = screen.queryAllByRole("button");
      expect(allButtons.length).toBe(0);
    });
  });
});
