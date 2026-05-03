/**
 * RoleGate.tsx - Role-based UI gating component for the Sales-Connections SPA.
 *
 * !!! CRITICAL SECURITY NOTICE !!!
 *
 * This component is a UI courtesy ONLY. It hides UI controls from users
 * whose role does not authorize the action, improving UX by avoiding
 * 403 errors when the user clicks a forbidden button.
 *
 * IT IS NOT A SECURITY BOUNDARY.
 *
 * Per AAP Sec 0.7.1 invariant 7:
 *   "API-layer authorization is authoritative. The frontend's <RoleGate>
 *   is a UX courtesy; the backend RBAC decorator is the only authoritative
 *   gate. Never rely on the absence of a UI control to prevent an action."
 *
 * The actual security boundary is enforced by the backend's
 * `@requires_role(...)` decorator on every protected handler in
 * backend/app/api/*.py (per backend/app/middleware/rbac.py). A
 * sufficiently determined attacker can bypass this <RoleGate> by:
 *   - Editing the React DevTools state to forge their role
 *   - Calling the API directly with curl/Postman
 *   - Patching the JS bundle in their browser
 *
 * In every case, the backend rejects unauthorized requests with HTTP 403
 * BEFORE any state change occurs. <RoleGate> simply ensures normal users
 * don't see buttons that would 403 if clicked.
 *
 * DEFENSE IN DEPTH:
 *   - Layer 1 (UI):  <RoleGate> hides forbidden controls (this file).
 *   - Layer 2 (API): @requires_role decorator rejects forbidden requests.
 *   - Layer 3 (DB):  Database-level grants on audit_events.
 *
 * Skipping Layer 1 leads to a confusing UX (403 toasts on click).
 * Skipping Layer 2 is a security incident. The two layers are NOT
 * substitutes; both must be present. This component implements Layer 1
 * as a secondary defense ONLY; Layer 2 is the authoritative gate.
 *
 * Role-name discipline:
 *   The role names ("Admin", "Contributor", "Viewer") MUST match the
 *   backend's UserRole enum (app.models.enums.UserRole) exactly. The
 *   "Viewer" role is the Sales Rep per AAP Sec 0.1.2; do NOT use
 *   "Sales Rep" as a role-name string anywhere - the predicate would
 *   never match because session.user.role is always one of the three
 *   backend enum values.
 *
 * Usage examples:
 *
 *   // Hide an admin-only button from non-admins:
 *   <RoleGate role="Admin">
 *     <Button onClick={onHardDelete}>Permanently delete</Button>
 *   </RoleGate>
 *
 *   // Show the status-mutation control to admins and viewers (sales reps),
 *   // but not contributors:
 *   <RoleGate role={["Admin", "Viewer"]}>
 *     <StatusEditDropdown />
 *   </RoleGate>
 *
 *   // Render a fallback message when the role doesn't match:
 *   <RoleGate role="Admin" fallback={<span>Admin only</span>}>
 *     <AdminPanel />
 *   </RoleGate>
 *
 *   // Inverted role check (uncommon; usually wrap the un-allowed UI):
 *   <RoleGate role={["Contributor", "Viewer"]}>
 *     <p>You can submit connection ideas via the form below.</p>
 *   </RoleGate>
 */

import type { JSX, ReactNode } from "react";

import { useRole } from "@/auth/AuthProvider";
import { USER_ROLE_VALUES } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * The three valid role names from the backend's UserRole enum
 * (app.models.enums.UserRole). Derived from the USER_ROLE_VALUES tuple
 * in @/schemas/admin so this file stays in lockstep with the schema; if
 * the backend ever adds a fourth role, only the schema changes and this
 * type follows automatically.
 *
 * Sourcing the type from the runtime tuple makes drift between frontend
 * role strings and the backend enum a compile-time error, which is
 * critical because a typo here would cause the predicate to silently
 * fail (the gate would never open for the affected role).
 */
export type UserRole = (typeof USER_ROLE_VALUES)[number];

/**
 * Props for the <RoleGate> component.
 *
 * Per the AAP Sec 0.2.3 frontend-tier table, this component is a
 * "wrapper hiding admin-only or status-mutation actions in the UI as
 * secondary defense (F-009)." It accepts a role or array of roles and
 * an optional fallback, and renders its children only when the current
 * user's role matches one of the allowed roles.
 */
export interface RoleGateProps {
  /**
   * The role(s) that may see the gated content. Accepts a single
   * UserRole or a (readonly) array of UserRole. Array semantics is OR -
   * if the user's role matches ANY value in the array, the children are
   * rendered.
   *
   * Examples:
   *   role="Admin"
   *   role={["Admin", "Viewer"]}
   *
   * The `ReadonlyArray<UserRole>` accepts both `UserRole[]` and
   * `readonly UserRole[]` callers; the inner logic never mutates the
   * array, so read-only is the correct contract.
   */
  role: UserRole | ReadonlyArray<UserRole>;

  /**
   * Optional content to render when the user's role does NOT match.
   * Defaults to `null` (the gated UI is silently omitted from the DOM).
   *
   * Common patterns:
   *   - Omit (default null): hide the control entirely (most common).
   *   - Provide a <Navigate>: route-level role guard (admin sub-routes).
   *   - Provide a placeholder message: indicate why the control is
   *     missing (e.g., "<Admin only>") for clarity in admin panels.
   */
  fallback?: ReactNode;

  /**
   * The content to render when the user's role matches the allowed
   * list. Required. Accepts any ReactNode (string, number, element,
   * fragment, array, null, undefined, boolean) so consumers can pass
   * arbitrary JSX trees through the gate.
   */
  children: ReactNode;
}

// ---------------------------------------------------------------------------
// RoleGate component
// ---------------------------------------------------------------------------

/**
 * Conditionally renders children based on the current user's role.
 *
 * Per AAP Sec 0.7.1 invariant 7, this component is UX-only defense.
 * The backend's @requires_role decorator is the actual security gate.
 *
 * Decision logic:
 *
 *   role prop      | session role     | Outcome
 *   -------------- | ---------------- | -------------------------------
 *   "Admin"        | "Admin"          | Render children
 *   "Admin"        | "Contributor"    | Render fallback (or null)
 *   ["A", "V"]     | "Viewer"         | Render children (matches "V")
 *   ["A", "V"]     | "Contributor"    | Render fallback (matches neither)
 *   "Admin"        | null (no session)| Render fallback (no role to match)
 *
 * Implementation notes:
 *   - `Array.isArray(role)` runtime-narrows the union prop to a uniform
 *     array shape. This is the standard idiom and works correctly for
 *     both `UserRole[]` and `ReadonlyArray<UserRole>` inputs.
 *   - `.some((t) => has(t))` implements OR semantics: children render if
 *     ANY allowed role matches the user's role.
 *   - `has(target)` from `useRole()` already handles the no-session case
 *     (returns `false` when there is no session), so an explicit null
 *     check is unnecessary here. Centralizing the comparison logic in
 *     `useRole` also lets a future role hierarchy (e.g., Admin implies
 *     Contributor) be absorbed in one place without modifying RoleGate.
 *   - Both branches return a fragment (<>...</>) to satisfy the
 *     JSX.Element return type uniformly. When `fallback` is undefined,
 *     `fallback ?? null` short-circuits to `null` inside the fragment.
 *   - No memoization is added: `useRole` already returns a memoized
 *     object, and the only computation here is a `.some()` over a
 *     1-3 element array. React.memo / useMemo would be premature.
 *   - No DOM wrapper is rendered: the gate is structural, not visual.
 *     Adding `data-role="Admin"` would expose the role check in the
 *     DOM, a minor information leak (an unauthenticated user could
 *     enumerate role-gated UI by inspecting the DOM).
 *
 * @example
 *   <RoleGate role="Admin">
 *     <DeleteButton />
 *   </RoleGate>
 *
 * @example
 *   <RoleGate role={["Admin", "Viewer"]} fallback={<NotAuthorizedMessage />}>
 *     <StatusEditDropdown />
 *   </RoleGate>
 */
export function RoleGate({ role, fallback, children }: RoleGateProps): JSX.Element {
  // Read the role predicate from the AuthProvider context. If the
  // provider is missing, useRole throws a clear error (we let it
  // propagate rather than swallowing - a missing provider is a
  // configuration bug, not a runtime state we should mask).
  const { has } = useRole();

  // Normalize the role prop to an array for uniform handling.
  // Single role "Admin" becomes ["Admin"]; readonly array passes through.
  // Array.isArray correctly narrows both UserRole[] and ReadonlyArray<UserRole>.
  const allowedRoles: ReadonlyArray<UserRole> = Array.isArray(role) ? role : [role];

  // The user's role must match AT LEAST ONE allowed role (OR semantics).
  // `has(target)` returns true only when the current session.user.role
  // exactly equals `target`; returns false for the no-session case.
  const isAllowed = allowedRoles.some((target) => has(target));

  if (!isAllowed) {
    // Render fallback if provided, otherwise render nothing.
    // Wrapping in a fragment satisfies the JSX.Element return type
    // even when fallback is undefined (becomes <></>).
    return <>{fallback ?? null}</>;
  }

  // The user's role is in the allowed list; render the gated content.
  // Fragment wrapper keeps the return type uniformly JSX.Element and
  // avoids introducing an extra DOM node around the children.
  return <>{children}</>;
}
