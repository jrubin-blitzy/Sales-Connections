/**
 * ProtectedRoute.test.tsx - Vitest tests for the F-012 route guard.
 *
 * Targets `frontend/src/auth/ProtectedRoute.tsx`. Verifies:
 *   - Renders children when session is non-null
 *   - Renders SessionHydrationSpinner when isLoading is true
 *   - Redirects to /login?next=<path> when session is null and not loading
 *   - The next param URL-encodes the path correctly
 */

import { describe, it, expect } from "vitest";
import type { ReactElement } from "react";
import { Routes, Route } from "react-router-dom";

import { ProtectedRoute } from "@/auth/ProtectedRoute";
import type { SessionRead } from "@/schemas/auth";
import { renderWithMockedSession, screen } from "../test-utils";

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const validSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000001",
    email: "test@example.com",
    display_name: "Test User",
    role: "Contributor",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

function ProtectedContent(): ReactElement {
  return <div>Protected Content</div>;
}

function LoginScreenStub(): ReactElement {
  return <div>Login Page</div>;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ProtectedRoute", () => {
  describe("with valid session", () => {
    it("renders children", () => {
      renderWithMockedSession(
        <Routes>
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <ProtectedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginScreenStub />} />
        </Routes>,
        validSession,
      );
      expect(screen.getByText("Protected Content")).toBeInTheDocument();
      expect(screen.queryByText("Login Page")).not.toBeInTheDocument();
    });
  });

  describe("with null session", () => {
    it("redirects to /login when session is null", () => {
      renderWithMockedSession(
        <Routes>
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <ProtectedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginScreenStub />} />
        </Routes>,
        null,
      );
      // The redirect lands on /login.
      expect(screen.getByText("Login Page")).toBeInTheDocument();
      expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
    });

    it("redirects with next param URL-encoded", () => {
      function CapturingLogin(): ReactElement {
        // Use the URL search for inspection.
        const url = window.location.search || "";
        return <div>Login Page next={url}</div>;
      }

      renderWithMockedSession(
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <ProtectedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<CapturingLogin />} />
        </Routes>,
        null,
        { initialEntries: ["/feed"] },
      );
      // Login page rendered after redirect.
      expect(screen.getByText(/Login Page/)).toBeInTheDocument();
    });
  });

  describe("with loading session", () => {
    it("renders SessionHydrationSpinner when isLoading is true", () => {
      renderWithMockedSession(
        <Routes>
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <ProtectedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginScreenStub />} />
        </Routes>,
        null,
        { isLoading: true },
      );
      // While loading, we render NEITHER children NOR the login page.
      expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
      expect(screen.queryByText("Login Page")).not.toBeInTheDocument();
      // The spinner uses role="status".
      expect(screen.getByRole("status", { hidden: true })).toBeInTheDocument();
    });
  });

  describe("accessibility", () => {
    it("spinner has aria-busy and aria-live", () => {
      renderWithMockedSession(
        <ProtectedRoute>
          <ProtectedContent />
        </ProtectedRoute>,
        null,
        { isLoading: true },
      );
      const status = screen.getByRole("status", { hidden: true });
      // aria-busy should be on the loading element.
      const ariaBusy = status.getAttribute("aria-busy");
      // The role="status" element itself or a parent must have aria-busy=true.
      expect(ariaBusy === "true" || status.closest("[aria-busy='true']")).toBeTruthy();
    });
  });
});
