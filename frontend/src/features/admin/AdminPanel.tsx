/**
 * AdminPanel.tsx - F-014 Admin Panel layout shell.
 *
 * Mounted at /admin per the route configuration in @/router.tsx. The
 * route is wrapped in <ProtectedRoute> and <RoleGate role="Admin">
 * upstream so by the time this component renders, the session is
 * authenticated and confirmed to have the Admin role. The backend's
 * @requires_role(Admin) decorator on every /api/admin/* endpoint is
 * the authoritative gate per AAP Sec 0.7.1 invariant 7; this layout's
 * role gating is a UI courtesy.
 *
 * Responsibilities:
 *   - Render a header banner that anchors the admin surface visually,
 *     including a brand-tinted ShieldCheck icon, the page title, an
 *     informational "Admin" Badge, and a one-line subtitle.
 *   - Render a horizontal tab nav (Users / Records / Analytics) with
 *     active-state styling driven by NavLink's isActive callback. Each
 *     tab anchor includes a Lucide icon plus a short label.
 *   - Render <Outlet /> for the selected child route content
 *     (UserManagement / RecordModeration / Analytics).
 *
 * What this file does NOT do:
 *   - Fetch any data; child routes own their own queries.
 *   - Apply any role gate; that is configured at the route level.
 *   - Render the children directly; they are mounted as nested routes
 *     and rendered via <Outlet />. The index redirect from /admin to
 *     /admin/users is configured in @/router.tsx via
 *     `{ index: true, element: <Navigate to="/admin/users" replace /> }`.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only; no inline styles.
 *   - Strict TypeScript; no `any`; explicit JSX.Element return type.
 *   - Lucide-React icons throughout (header + tabs).
 *   - clsx for conditional className composition.
 *   - Path imports use the @/ alias for src.
 *   - Semantic HTML: <header>, <nav>, <section>, <h1>, <ul>, <li>,
 *     and <a> (rendered by NavLink under the hood).
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false).
 *
 * Accessibility:
 *   - aria-label="Admin sections" on <nav>.
 *   - role="tablist" on the <ul> to clarify grouping for assistive
 *     tech (this is a navigation pattern, not a true ARIA tab pattern,
 *     but the role provides useful semantic grouping without invoking
 *     the full ARIA tab keyboard model).
 *   - role="presentation" on each <li> so screen readers do not
 *     announce list semantics on top of the tab semantics.
 *   - aria-label on the <section> hosting the outlet so the content
 *     region is identifiable in landmark navigation.
 *   - aria-hidden="true" on every decorative icon; the surrounding
 *     text is the accessible name in each case.
 *   - Visible :focus-visible ring on each NavLink so keyboard users
 *     have an obvious focus indicator.
 *
 * Mobile-responsive:
 *   - Padding scales with breakpoints (px-4 -> sm:px-6 -> lg:px-8).
 *   - Tab list is `overflow-x-auto` so the three tabs scroll
 *     horizontally on viewports too narrow to fit them all - labels
 *     stay readable rather than collapsing to icons.
 *
 * Coordinates with:
 *   - react-router-dom 6.x - NavLink (active-state aware) and Outlet
 *     (nested route renderer); no internal imports needed.
 *   - @/components/ui/Badge - the canonical pill-shaped label
 *     primitive used for the "Admin" header badge (variant='brand',
 *     size='sm', withDot=true).
 *   - @/router.tsx - mounts <AdminPanel /> at /admin with three nested
 *     children (`users`, `records`, `analytics`) and an index redirect
 *     to /admin/users.
 *   - @/features/admin/UserManagement.tsx,
 *     @/features/admin/RecordModeration.tsx,
 *     @/features/admin/Analytics.tsx - mounted as nested route children
 *     in @/router.tsx and rendered via <Outlet /> here. NOT imported
 *     from this file.
 *   - frontend/tests/features/admin/AdminPanel.test.tsx - verifies
 *     header rendering, tab rendering, active-state styling, NavLink
 *     navigation, and Outlet rendering of a child route.
 */

import { NavLink, Outlet } from "react-router-dom";
import { BarChart3, FileWarning, ShieldCheck, Users } from "lucide-react";
import clsx from "clsx";
import type { JSX } from "react";

import { Badge } from "@/components/ui/Badge";

// ---------------------------------------------------------------------------
// Tab definition
//
// The three admin tabs are declared as a module-level constant so the
// nav rendering loop has a single source of truth and the icon-and-label
// pairing for each tab is colocated in one place. Adding or removing a
// tab is a one-line change here plus a matching nested route in
// @/router.tsx.
//
// `to` is a relative path (matches the nested route children of /admin
// in @/router.tsx). `end` is applied on the NavLink (not here) to
// enforce strict path matching so /admin/users does not match a future
// /admin/users/:id sub-route.
// ---------------------------------------------------------------------------

/**
 * Shape of a single Admin Panel tab descriptor.
 *
 * - `to`     Relative URL path appended to /admin. Restricted to the
 *            three values configured as nested route children in
 *            @/router.tsx so a typo here is a TypeScript error rather
 *            than a runtime 404.
 * - `label`  Short visible text shown next to the icon in the tab.
 * - `icon`   Lucide-React icon component rendered alongside the label.
 *            Typed as `typeof Users` (any Lucide component shares the
 *            same forward-ref type signature, so this is the
 *            simplest, accurate type).
 * - `testId` Stable selector for component tests in
 *            frontend/tests/features/admin/AdminPanel.test.tsx.
 */
interface AdminTab {
  readonly to: "users" | "records" | "analytics";
  readonly label: string;
  readonly icon: typeof Users;
  readonly testId: string;
}

const ADMIN_TABS: ReadonlyArray<AdminTab> = [
  { to: "users", label: "Users", icon: Users, testId: "admin-tab-users" },
  { to: "records", label: "Records", icon: FileWarning, testId: "admin-tab-records" },
  { to: "analytics", label: "Analytics", icon: BarChart3, testId: "admin-tab-analytics" },
];

// ---------------------------------------------------------------------------
// AdminPanel component
// ---------------------------------------------------------------------------

/**
 * Admin Panel layout shell. Top-level component mounted at /admin.
 *
 * Layout: vertical stack of (header, tab nav, outlet). The header
 * contains the page title, a brand-tinted ShieldCheck icon, an
 * informational "Admin" badge, and a one-line subtitle. The tab nav
 * is a horizontal row of three NavLink anchors; the outlet renders
 * whichever nested child route is currently matched.
 *
 * Behavior at /admin (no child segment): @/router.tsx redirects to
 * /admin/users via an index route, so this component never renders an
 * empty outlet in the happy path.
 *
 * @returns The Admin Panel layout shell as a JSX.Element.
 */
export function AdminPanel(): JSX.Element {
  return (
    <div
      className="flex min-h-full flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8"
      data-testid="admin-panel"
    >
      <header className="flex flex-col gap-1" data-testid="admin-panel-header">
        <div className="flex items-center gap-2">
          <ShieldCheck aria-hidden="true" className="h-6 w-6 text-brand-600" />
          <h1 className="text-2xl font-semibold text-slate-900">Admin Panel</h1>
          <Badge variant="brand" size="sm" withDot>
            Admin
          </Badge>
        </div>
        <p className="text-sm text-slate-600">
          Manage users and roles, moderate records, and view organization analytics.
        </p>
      </header>

      <nav
        aria-label="Admin sections"
        className="border-b border-slate-200"
        data-testid="admin-panel-nav"
      >
        <ul className="flex gap-1 overflow-x-auto" role="tablist">
          {ADMIN_TABS.map((tab) => {
            const Icon = tab.icon;
            return (
              <li key={tab.to} role="presentation">
                <NavLink
                  to={tab.to}
                  end
                  className={({ isActive }) =>
                    clsx(
                      "inline-flex items-center gap-2 whitespace-nowrap",
                      "border-b-2 px-3 py-2.5 text-sm font-medium",
                      "transition-colors duration-150",
                      "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
                      isActive
                        ? "border-brand-600 text-brand-700"
                        : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-900",
                    )
                  }
                  data-testid={tab.testId}
                  aria-label={tab.label}
                >
                  {({ isActive }) => (
                    <>
                      <Icon
                        aria-hidden="true"
                        className={clsx("h-4 w-4", isActive ? "text-brand-600" : "text-slate-500")}
                      />
                      <span>{tab.label}</span>
                    </>
                  )}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>

      <section aria-label="Admin tab content" className="flex-1" data-testid="admin-panel-outlet">
        <Outlet />
      </section>
    </div>
  );
}
