/**
 * RoleGate.test.tsx - Vitest tests for the F-009 role-gating UI component.
 *
 * Targets `frontend/src/auth/RoleGate.tsx` - a UX-only courtesy component
 * that hides controls from users whose role does not match. Per AAP Sec
 * 0.7.1 invariant 7, this is NOT a security boundary. The tests verify:
 *
 *   - Renders children when session.user.role matches the role prop
 *   - Renders fallback when role does not match
 *   - Renders null when role does not match and no fallback
 *   - Single-role string and array-of-roles both work (OR semantics)
 *   - Null session (unauthenticated) hides children
 *   - Multi-role array admits when ANY role matches
 *   - Hides Admin-only UI from Contributor/Viewer
 *   - Hides Viewer-only UI from Contributor (sales rep status mutation)
 */

import { describe, it, expect } from "vitest";

import { RoleGate } from "@/auth/RoleGate";
import type { SessionRead } from "@/schemas/auth";
import { renderWithMockedSession, screen } from "../test-utils";

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const adminSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000001",
    email: "admin@example.com",
    display_name: "Admin User",
    role: "Admin",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const contributorSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000002",
    email: "contributor@example.com",
    display_name: "Contributor User",
    role: "Contributor",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const viewerSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000003",
    email: "viewer@example.com",
    display_name: "Viewer User",
    role: "Viewer",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("RoleGate", () => {
  describe("single-role string", () => {
    it("renders children when role matches", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <button>Admin Action</button>
        </RoleGate>,
        adminSession,
      );
      expect(
        screen.getByRole("button", { name: "Admin Action" }),
      ).toBeInTheDocument();
    });

    it("hides children when role does not match", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <button>Admin Action</button>
        </RoleGate>,
        contributorSession,
      );
      expect(
        screen.queryByRole("button", { name: "Admin Action" }),
      ).not.toBeInTheDocument();
    });

    it("renders fallback when role does not match", () => {
      renderWithMockedSession(
        <RoleGate role="Admin" fallback={<span>Admin only</span>}>
          <button>Admin Action</button>
        </RoleGate>,
        contributorSession,
      );
      expect(screen.getByText("Admin only")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Admin Action" }),
      ).not.toBeInTheDocument();
    });
  });

  describe("array-of-roles (OR semantics)", () => {
    it("renders children when ANY role matches", () => {
      renderWithMockedSession(
        <RoleGate role={["Admin", "Viewer"]}>
          <button>Status Mutation</button>
        </RoleGate>,
        viewerSession,
      );
      expect(
        screen.getByRole("button", { name: "Status Mutation" }),
      ).toBeInTheDocument();
    });

    it("renders children for Admin in array", () => {
      renderWithMockedSession(
        <RoleGate role={["Admin", "Viewer"]}>
          <button>Status Mutation</button>
        </RoleGate>,
        adminSession,
      );
      expect(
        screen.getByRole("button", { name: "Status Mutation" }),
      ).toBeInTheDocument();
    });

    it("hides children when NO role matches", () => {
      renderWithMockedSession(
        <RoleGate role={["Admin", "Viewer"]}>
          <button>Status Mutation</button>
        </RoleGate>,
        contributorSession,
      );
      expect(
        screen.queryByRole("button", { name: "Status Mutation" }),
      ).not.toBeInTheDocument();
    });
  });

  describe("null session (unauthenticated)", () => {
    it("hides children when session is null", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <button>Admin Action</button>
        </RoleGate>,
        null,
      );
      expect(
        screen.queryByRole("button", { name: "Admin Action" }),
      ).not.toBeInTheDocument();
    });

    it("renders fallback when session is null", () => {
      renderWithMockedSession(
        <RoleGate role="Admin" fallback={<span>Login required</span>}>
          <button>Admin Action</button>
        </RoleGate>,
        null,
      );
      expect(screen.getByText("Login required")).toBeInTheDocument();
    });
  });

  describe("documented use cases (AAP Sec 0.5.2 Layer 6)", () => {
    it("hides Admin-only UI from Contributor", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <button>Hard Delete</button>
        </RoleGate>,
        contributorSession,
      );
      expect(
        screen.queryByRole("button", { name: "Hard Delete" }),
      ).not.toBeInTheDocument();
    });

    it("hides Admin-only UI from Viewer", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <button>Hard Delete</button>
        </RoleGate>,
        viewerSession,
      );
      expect(
        screen.queryByRole("button", { name: "Hard Delete" }),
      ).not.toBeInTheDocument();
    });

    it("shows status mutation to Admin and Viewer (sales rep), hides from Contributor", () => {
      // Admin can see it.
      const { rerender } = renderWithMockedSession(
        <RoleGate role={["Admin", "Viewer"]}>
          <button>Update Status</button>
        </RoleGate>,
        adminSession,
      );
      expect(
        screen.getByRole("button", { name: "Update Status" }),
      ).toBeInTheDocument();

      // Viewer (sales rep) can see it.
      rerender(
        <RoleGate role={["Admin", "Viewer"]}>
          <button>Update Status</button>
        </RoleGate>,
      );
      expect(
        screen.getByRole("button", { name: "Update Status" }),
      ).toBeInTheDocument();
    });
  });

  describe("complex children", () => {
    it("renders complex children when role matches", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <div>
            <h2>Admin Panel</h2>
            <button>Manage Users</button>
            <button>View Analytics</button>
          </div>
        </RoleGate>,
        adminSession,
      );
      expect(screen.getByText("Admin Panel")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Manage Users" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "View Analytics" }),
      ).toBeInTheDocument();
    });

    it("hides complex children when role does not match", () => {
      renderWithMockedSession(
        <RoleGate role="Admin">
          <div>
            <h2>Admin Panel</h2>
            <button>Manage Users</button>
          </div>
        </RoleGate>,
        contributorSession,
      );
      expect(screen.queryByText("Admin Panel")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Manage Users" }),
      ).not.toBeInTheDocument();
    });
  });
});
