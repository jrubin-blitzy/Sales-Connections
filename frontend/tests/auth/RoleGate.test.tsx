/**
 * RoleGate.test.tsx - Vitest tests for the F-009 role-gating UI component.
 *
 * Targets `frontend/src/auth/RoleGate.tsx`. Per AAP Sec 0.7.1 invariant 7,
 * RoleGate is the UX-only secondary defense layer; the backend's
 * @requires_role decorator is the authoritative authorization gate. These
 * tests verify the SECONDARY defense semantics:
 *
 *   - Single allowed role renders children.
 *   - Single denied role hides children (or renders fallback when supplied).
 *   - Array of allowed roles uses OR semantics: child renders if user
 *     matches ANY entry.
 *   - Array of denied roles hides children (or renders fallback).
 *   - No-session state (role === null) hides children regardless of role
 *     prop, including when the role array is empty.
 *   - fallback prop renders when present and the gate is closed; renders
 *     nothing when absent.
 *   - Backend role names ('Admin' | 'Contributor' | 'Viewer') are honored;
 *     'Sales Rep' is NOT a valid role string.
 *   - children may be ANY ReactNode (string, single element, array, JSX
 *     tree).
 *
 * Strategy: Mock `useRole` from '@/auth/AuthProvider' via vi.mock at module
 * scope. Tests synchronously control what useRole() returns by calling
 * vi.mocked(useRole).mockReturnValue({ role, has }) per case. This keeps
 * the suite isolated from the production AuthProvider's TanStack Query
 * round-trip; we only need synchronous control over useRole() to exercise
 * RoleGate's rendering branches.
 */

import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { RoleGate, type UserRole } from "@/auth/RoleGate";
import { useRole, type UseRoleReturn } from "@/auth/AuthProvider";

// ---------------------------------------------------------------------------
// Module-scope vi.mock for @/auth/AuthProvider
// ---------------------------------------------------------------------------
//
// CRITICAL: vi.mock calls at module scope are HOISTED by Vitest above any
// imports, so the mock is in place when RoleGate.tsx resolves its
// `import { useRole } from "@/auth/AuthProvider"` at module load time.
//
// We provide stubs for ALL exports of @/auth/AuthProvider (not just useRole)
// so that any transitive import of the module from RoleGate.tsx or its
// imports resolves cleanly without TypeScript errors. RoleGate.tsx only
// directly uses `useRole`, but we mock the full export surface for safety.
vi.mock("@/auth/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useRole: vi.fn<() => UseRoleReturn>(),
  useSession: vi.fn(),
  useSessionLoading: vi.fn(),
  useLogout: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a UseRoleReturn matching the production useRole shape.
 *
 * The `has` predicate is constructed from the role: exact-match if role
 * is non-null; always returns false if role is null. This mirrors the
 * production hook in @/auth/AuthProvider exactly so tests exercise the
 * same predicate semantics RoleGate sees in production.
 *
 * Use `setRole(null)` for the no-session case.
 */
function setRole(role: UserRole | null): UseRoleReturn {
  return {
    role,
    has: (target: UserRole): boolean => role !== null && role === target,
  };
}

/**
 * Configure useRole() to return the supplied role for the next render.
 * Call from inside a test before render(...).
 */
function mockUseRole(role: UserRole | null): void {
  vi.mocked(useRole).mockReturnValue(setRole(role));
}

// ---------------------------------------------------------------------------
// Setup and teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset call history and any prior mockReturnValue so each test starts
  // with a fresh mock and must explicitly call mockUseRole(...) before
  // rendering. Tests that forget to set the role will see useRole()
  // return undefined, which throws inside RoleGate - a loud failure
  // mode is preferable to silent default behavior.
  vi.mocked(useRole).mockReset();
});

afterEach(() => {
  // Unmount any rendered trees and restore mocks for full isolation
  // between tests. Without cleanup(), DOM nodes from test A leak into
  // test B's screen queries (jsdom is shared across tests in a worker).
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Test suite: Single role prop
// ---------------------------------------------------------------------------

describe("<RoleGate /> with a single role prop", () => {
  it("renders children when the user role matches the allowed role", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role="Admin">
        <span data-testid="gated-content">Admin-only content</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("gated-content")).toBeInTheDocument();
    expect(screen.getByTestId("gated-content")).toHaveTextContent("Admin-only content");
  });

  it("hides children when the user role does not match the allowed role", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate role="Admin">
        <span data-testid="gated-content">Admin-only content</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("hides children when there is no session (role === null)", () => {
    mockUseRole(null);
    render(
      <RoleGate role="Admin">
        <span data-testid="gated-content">Admin-only content</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("renders fallback when the role does not match and fallback is provided", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate role="Admin" fallback={<span data-testid="fallback-content">Locked</span>}>
        <span data-testid="gated-content">Admin-only content</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("fallback-content")).toBeInTheDocument();
    expect(screen.getByTestId("fallback-content")).toHaveTextContent("Locked");
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("renders nothing (no fallback) when role does not match and fallback is omitted", () => {
    mockUseRole("Contributor");
    const { container } = render(
      <RoleGate role="Admin">
        <span data-testid="gated-content">Admin-only content</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
    // The fragment renders nothing when fallback is undefined; the container
    // should be empty (or contain only whitespace from React's fragment).
    expect(container.textContent).toBe("");
  });
});

// ---------------------------------------------------------------------------
// Test suite: Array role prop (OR semantics)
// ---------------------------------------------------------------------------

describe("<RoleGate /> with a role array prop", () => {
  it("renders children when role matches any entry in the array (Admin in [Admin, Viewer])", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role={["Admin", "Viewer"]}>
        <span data-testid="gated-content">Status mutator</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("gated-content")).toBeInTheDocument();
  });

  it("renders children when role matches second entry (Viewer in [Admin, Viewer])", () => {
    mockUseRole("Viewer");
    render(
      <RoleGate role={["Admin", "Viewer"]}>
        <span data-testid="gated-content">Status mutator</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("gated-content")).toBeInTheDocument();
  });

  it("hides children when role matches no entry in the array", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate role={["Admin", "Viewer"]}>
        <span data-testid="gated-content">Status mutator</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("hides children when there is no session and role array is provided", () => {
    mockUseRole(null);
    render(
      <RoleGate role={["Admin", "Viewer", "Contributor"]}>
        <span data-testid="gated-content">Anyone authenticated</span>
      </RoleGate>,
    );
    // null session never matches any role, even when every role is allowed.
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("renders fallback when role array has no match and fallback is provided", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate
        role={["Admin", "Viewer"]}
        fallback={<span data-testid="fallback-content">Sales team only</span>}
      >
        <span data-testid="gated-content">Status mutator</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("fallback-content")).toBeInTheDocument();
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("renders children when role array has only one entry that matches", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role={["Admin"]}>
        <span data-testid="gated-content">Admin only</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("gated-content")).toBeInTheDocument();
  });

  it("hides children when role array is empty (no roles allowed)", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role={[] as ReadonlyArray<UserRole>}>
        <span data-testid="gated-content">Should never render</span>
      </RoleGate>,
    );
    // Empty allowedRoles => some(...) returns false => not allowed.
    // Catches a regression where an accidentally-empty array might
    // fall through to "all allowed".
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Test suite: Children variants (string, array, nested JSX tree, JSX fallback)
// ---------------------------------------------------------------------------

describe("<RoleGate /> children variants", () => {
  it("renders string children when allowed", () => {
    mockUseRole("Admin");
    render(<RoleGate role="Admin">Plain text content</RoleGate>);
    expect(screen.getByText("Plain text content")).toBeInTheDocument();
  });

  it("renders array of children when allowed", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role="Admin">
        <span data-testid="first">First</span>
        <span data-testid="second">Second</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("first")).toBeInTheDocument();
    expect(screen.getByTestId("second")).toBeInTheDocument();
  });

  it("renders nested JSX tree children when allowed", () => {
    mockUseRole("Admin");
    render(
      <RoleGate role="Admin">
        <div data-testid="outer">
          <button type="button" data-testid="inner-button">
            Click me
          </button>
        </div>
      </RoleGate>,
    );
    expect(screen.getByTestId("outer")).toBeInTheDocument();
    expect(screen.getByTestId("inner-button")).toBeInTheDocument();
  });

  it("renders fallback that is a JSX tree, not a plain string", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate
        role="Admin"
        fallback={
          <div data-testid="fallback-tree">
            <p>You need Admin role.</p>
            <a href="/contact" data-testid="fallback-link">
              Contact admin
            </a>
          </div>
        }
      >
        <span data-testid="gated-content">Admin tools</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("fallback-tree")).toBeInTheDocument();
    expect(screen.getByTestId("fallback-link")).toBeInTheDocument();
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Test suite: Three-role permission matrix
// ---------------------------------------------------------------------------

describe("<RoleGate /> three-role permission matrix", () => {
  // Verify backend role names exactly. AAP Sec 0.1.2: 'Admin', 'Contributor',
  // 'Viewer'. 'Viewer' IS the Sales Rep; 'Sales Rep' is NOT a role string.
  const roles: ReadonlyArray<UserRole> = ["Admin", "Contributor", "Viewer"];

  it.each(roles)("renders children when role matches %s", (role) => {
    mockUseRole(role);
    render(
      <RoleGate role={role}>
        <span data-testid="gated-content">{role} content</span>
      </RoleGate>,
    );
    expect(screen.getByTestId("gated-content")).toBeInTheDocument();
  });

  it.each(roles)("hides children when role is %s but allowed roles exclude it", (role) => {
    mockUseRole(role);
    const otherRoles = roles.filter((r) => r !== role);
    render(
      <RoleGate role={otherRoles}>
        <span data-testid="gated-content">Not for {role}</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Test suite: Architectural invariants (no role hierarchy)
// ---------------------------------------------------------------------------

describe("<RoleGate /> architectural invariants", () => {
  it("exact-match semantics: Admin role does NOT imply Contributor (no role hierarchy)", () => {
    mockUseRole("Admin");
    // RoleGate must reject Admin when only Contributor is allowed.
    // Backend RBAC also uses exact match; client behavior must mirror.
    // If hierarchical roles were ever introduced, this test must update
    // and the change must be coordinated with backend @requires_role.
    render(
      <RoleGate role="Contributor">
        <span data-testid="gated-content">Contributor only</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });

  it("exact-match semantics: Contributor does NOT imply Viewer", () => {
    mockUseRole("Contributor");
    render(
      <RoleGate role="Viewer">
        <span data-testid="gated-content">Viewer only</span>
      </RoleGate>,
    );
    expect(screen.queryByTestId("gated-content")).toBeNull();
  });
});
