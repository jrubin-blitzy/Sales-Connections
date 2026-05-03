/**
 * AdminPanel.test.tsx - Vitest tests for the F-014 + F-009 admin panel layout shell.
 *
 * Targets `frontend/src/features/admin/AdminPanel.tsx`, which:
 *   - Renders a header with a brand-tinted ShieldCheck icon, the
 *     "Admin Panel" heading, and a brand-variant "Admin" Badge.
 *   - Renders a tab navigation with three <NavLink> tabs:
 *     Users, Records, Analytics. Each NavLink uses function-as-children
 *     to pair a Lucide icon with a label, and computes its className via
 *     a callback that branches on `isActive`.
 *   - Applies active styling (border-brand-600, text-brand-700) to the
 *     matching tab and inactive styling (border-transparent,
 *     text-slate-600) to the others.
 *   - Renders an <Outlet /> for child route content
 *     (UserManagement / RecordModeration / Analytics).
 *   - Has accessibility affordances: role="tablist" on the <ul>,
 *     role="presentation" on each <li>, aria-label="Admin sections" on
 *     the <nav>, aria-label="Admin tab content" on the outlet section,
 *     and aria-hidden="true" on every decorative icon.
 *   - Mobile-responsive: the tab list has overflow-x-auto so the three
 *     tabs scroll horizontally on narrow viewports.
 *
 * Test scope (per the assigned folder requirements in the AAP):
 *   1. Layout shell structure: root, header, nav, outlet rendered with
 *      the documented data-testid hooks.
 *   2. Three tabs render as <NavLink> elements with the correct testids
 *      and resolve to the documented hrefs (relative `to="users"` etc.
 *      resolves to /admin/users when AdminPanel is mounted at /admin).
 *   3. Active tab styling (text-brand-700, border-brand-600) on the
 *      matching tab, inactive styling (text-slate-600,
 *      border-transparent) on the others, for all three child routes.
 *   4. NavLink default aria-current="page" on the active anchor.
 *   5. Clicking a tab navigates the URL and swaps the Outlet content.
 *   6. <Outlet /> renders the matched child route component (verified
 *      via fake placeholder children mounted inline in <Routes>).
 *   7. Accessibility: role="tablist" on the <ul>, role="presentation"
 *      on each <li>, aria-label="Admin sections" on the <nav>, and the
 *      mobile-responsive overflow-x-auto class on the tab list.
 *   8. RoleGate defense-in-depth (per AAP Sec 0.7.1 invariant 7):
 *      AdminPanel renders fully under <RoleGate role="Admin"> for Admin
 *      sessions and is hidden (renders nothing) for Contributor,
 *      Viewer, and unauthenticated (null) sessions.
 *
 * Mocking strategy:
 *   - renderWithMockedSession with a synchronous SessionRead provided
 *     via the makeSessionRead/makeUserRead factories. The session role
 *     is varied per test ('Admin' / 'Contributor' / 'Viewer') and the
 *     unauthenticated case passes session=null directly.
 *   - <Routes>/<Route> are defined inline in each test so the AdminPanel
 *     <Outlet /> has matched children to render (fake-users / -records /
 *     -analytics placeholder elements).
 *   - No MSW handlers are needed: AdminPanel is a pure layout shell
 *     with no API calls, useQuery hooks, or mutations.
 */

import { describe, it, expect } from "vitest";
import { Routes, Route } from "react-router-dom";

import { AdminPanel } from "@/features/admin/AdminPanel";
import { RoleGate } from "@/auth/RoleGate";

import { makeSessionRead, makeUserRead } from "../../mocks/data";
import { renderWithMockedSession, screen, userEvent, within } from "../../test-utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * The three role names from the backend `app.models.enums.UserRole` and
 * the frontend `@/schemas/admin` USER_ROLE_VALUES tuple. Keeping this
 * narrow at the test boundary lets `buildSession` enforce role-name
 * discipline at compile time so a typo (e.g., "Sales Rep") becomes a
 * type error rather than a silent UX failure.
 */
type SessionRole = "Admin" | "Contributor" | "Viewer";

/**
 * Build a SessionRead with the given role. Wraps `makeSessionRead`
 * with a clearer call site for role-driven tests:
 *
 *   buildSession("Admin")        => Admin session with display_name "Admin User"
 *   buildSession("Contributor")  => Contributor session
 *   buildSession("Viewer")       => Viewer session
 *
 * The id, email, and display_name are derived from the role for
 * readability when assertions print failing test output - a session
 * for a Contributor is obviously a Contributor in the diff, rather
 * than a generic User-1 default.
 */
function buildSession(role: SessionRole = "Admin") {
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
 * Render AdminPanel mounted at /admin with three nested child routes.
 *
 * The child routes mount fake placeholder elements (fake-users,
 * fake-records, fake-analytics) so AdminPanel's <Outlet /> has
 * something concrete to render, letting tests assert which child is
 * currently visible without depending on the production
 * UserManagement / RecordModeration / Analytics components.
 *
 * The MemoryRouter inside `renderWithMockedSession` is initialized
 * to `initialEntry` so each test starts on a specific route. Clicking
 * a NavLink updates that in-memory location, the <Routes> tree
 * re-matches, and the <Outlet /> swaps to the new child.
 *
 * @param initialEntry The initial route to navigate to (e.g.,
 *                     "/admin/users"). Defaults to "/admin/users".
 * @param role         The session role; defaults to "Admin".
 */
function renderAdminPanel(initialEntry: string = "/admin/users", role: SessionRole = "Admin") {
  return renderWithMockedSession(
    <Routes>
      <Route path="/admin" element={<AdminPanel />}>
        <Route path="users" element={<div data-testid="fake-users">USERS PAGE</div>} />
        <Route path="records" element={<div data-testid="fake-records">RECORDS PAGE</div>} />
        <Route path="analytics" element={<div data-testid="fake-analytics">ANALYTICS PAGE</div>} />
      </Route>
    </Routes>,
    buildSession(role),
    { initialEntries: [initialEntry] },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<AdminPanel />", () => {
  // -------------------------------------------------------------------------
  // Layout shell structure
  // -------------------------------------------------------------------------

  describe("layout shell structure", () => {
    it("renders the root container with admin-panel testid", () => {
      renderAdminPanel("/admin/users");

      const root = screen.getByTestId("admin-panel");
      expect(root).toBeInTheDocument();
      // The root is the layout container that wraps header + nav + outlet.
      // Per AdminPanel.tsx the root element is a flex column <div>.
      expect(root.tagName).toBe("DIV");
    });

    it("renders the header element with admin-panel-header testid", () => {
      renderAdminPanel("/admin/users");

      const header = screen.getByTestId("admin-panel-header");
      expect(header).toBeInTheDocument();
      expect(header.tagName).toBe("HEADER");
    });

    it('renders the "Admin Panel" h1 heading inside the header', () => {
      renderAdminPanel("/admin/users");

      const heading = screen.getByRole("heading", { level: 1, name: /admin panel/i });
      expect(heading).toBeInTheDocument();
      expect(heading).toHaveTextContent(/^Admin Panel$/);

      // Heading lives inside the header region.
      const header = screen.getByTestId("admin-panel-header");
      expect(header).toContainElement(heading);
    });

    it("renders the descriptive subtitle in the header", () => {
      renderAdminPanel("/admin/users");

      const subtitle = screen.getByText(
        /manage users and roles, moderate records, and view organization analytics\./i,
      );
      expect(subtitle).toBeInTheDocument();

      const header = screen.getByTestId("admin-panel-header");
      expect(header).toContainElement(subtitle);
    });

    it('renders an "Admin" brand-variant badge in the header', () => {
      renderAdminPanel("/admin/users");

      const header = screen.getByTestId("admin-panel-header");
      // The Badge component renders data-testid={`badge-${variant}`}, so
      // the brand-variant badge in this header is `badge-brand`.
      const badge = within(header).getByTestId("badge-brand");
      expect(badge).toBeInTheDocument();
      expect(badge).toHaveTextContent(/admin/i);
    });

    it("renders the tab nav with admin-panel-nav testid and aria-label", () => {
      renderAdminPanel("/admin/users");

      const nav = screen.getByTestId("admin-panel-nav");
      expect(nav).toBeInTheDocument();
      expect(nav.tagName).toBe("NAV");
      expect(nav).toHaveAttribute("aria-label", "Admin sections");
    });

    it("renders the outlet section with admin-panel-outlet testid", () => {
      renderAdminPanel("/admin/users");

      const outlet = screen.getByTestId("admin-panel-outlet");
      expect(outlet).toBeInTheDocument();
      expect(outlet.tagName).toBe("SECTION");
    });
  });

  // -------------------------------------------------------------------------
  // Tab nav links
  // -------------------------------------------------------------------------

  describe("tab nav links", () => {
    it("renders all three tabs as <a> (NavLink) elements with correct testids", () => {
      renderAdminPanel("/admin/users");

      const usersTab = screen.getByTestId("admin-tab-users");
      const recordsTab = screen.getByTestId("admin-tab-records");
      const analyticsTab = screen.getByTestId("admin-tab-analytics");

      // NavLink renders as <a> at the DOM layer.
      expect(usersTab.tagName).toBe("A");
      expect(recordsTab.tagName).toBe("A");
      expect(analyticsTab.tagName).toBe("A");

      // Each tab carries its visible label (the Lucide icon plus the
      // text label rendered by the function-as-children).
      expect(usersTab).toHaveTextContent(/users/i);
      expect(recordsTab).toHaveTextContent(/records/i);
      expect(analyticsTab).toHaveTextContent(/analytics/i);
    });

    it("renders each tab with the correct href resolved relative to /admin", () => {
      renderAdminPanel("/admin/users");

      // NavLink `to` is relative ("users") so the resolved href becomes
      // /admin/users when AdminPanel is mounted at /admin per the
      // <Routes>/<Route> tree above.
      expect(screen.getByTestId("admin-tab-users")).toHaveAttribute("href", "/admin/users");
      expect(screen.getByTestId("admin-tab-records")).toHaveAttribute("href", "/admin/records");
      expect(screen.getByTestId("admin-tab-analytics")).toHaveAttribute("href", "/admin/analytics");
    });

    it("renders exactly three tab links inside the nav", () => {
      renderAdminPanel("/admin/users");

      const nav = screen.getByTestId("admin-panel-nav");
      const tabLinks = within(nav).getAllByTestId(/^admin-tab-/);
      expect(tabLinks).toHaveLength(3);
    });

    it("renders an aria-label on each tab matching its visible label", () => {
      renderAdminPanel("/admin/users");

      // Per AdminPanel.tsx each NavLink has aria-label={tab.label} so
      // the accessible name matches the visible text exactly.
      expect(screen.getByTestId("admin-tab-users")).toHaveAttribute("aria-label", "Users");
      expect(screen.getByTestId("admin-tab-records")).toHaveAttribute("aria-label", "Records");
      expect(screen.getByTestId("admin-tab-analytics")).toHaveAttribute("aria-label", "Analytics");
    });
  });

  // -------------------------------------------------------------------------
  // Active tab styling
  // -------------------------------------------------------------------------

  describe("active tab styling", () => {
    it("highlights the Users tab when route is /admin/users", () => {
      renderAdminPanel("/admin/users");

      const usersTab = screen.getByTestId("admin-tab-users");
      const recordsTab = screen.getByTestId("admin-tab-records");
      const analyticsTab = screen.getByTestId("admin-tab-analytics");

      // Active tab gets the brand-tinted text + bottom-border classes.
      expect(usersTab.className).toMatch(/text-brand-700/);
      expect(usersTab.className).toMatch(/border-brand-600/);

      // Inactive tabs get the slate text + transparent bottom border.
      expect(recordsTab.className).toMatch(/text-slate-600/);
      expect(recordsTab.className).toMatch(/border-transparent/);
      expect(analyticsTab.className).toMatch(/text-slate-600/);
      expect(analyticsTab.className).toMatch(/border-transparent/);
    });

    it("highlights the Records tab when route is /admin/records", () => {
      renderAdminPanel("/admin/records");

      const usersTab = screen.getByTestId("admin-tab-users");
      const recordsTab = screen.getByTestId("admin-tab-records");
      const analyticsTab = screen.getByTestId("admin-tab-analytics");

      expect(recordsTab.className).toMatch(/text-brand-700/);
      expect(recordsTab.className).toMatch(/border-brand-600/);

      expect(usersTab.className).toMatch(/text-slate-600/);
      expect(usersTab.className).toMatch(/border-transparent/);
      expect(analyticsTab.className).toMatch(/text-slate-600/);
      expect(analyticsTab.className).toMatch(/border-transparent/);
    });

    it("highlights the Analytics tab when route is /admin/analytics", () => {
      renderAdminPanel("/admin/analytics");

      const usersTab = screen.getByTestId("admin-tab-users");
      const recordsTab = screen.getByTestId("admin-tab-records");
      const analyticsTab = screen.getByTestId("admin-tab-analytics");

      expect(analyticsTab.className).toMatch(/text-brand-700/);
      expect(analyticsTab.className).toMatch(/border-brand-600/);

      expect(usersTab.className).toMatch(/text-slate-600/);
      expect(usersTab.className).toMatch(/border-transparent/);
      expect(recordsTab.className).toMatch(/text-slate-600/);
      expect(recordsTab.className).toMatch(/border-transparent/);
    });

    it("never assigns active styling to more than one tab at a time", () => {
      // The NavLink uses end={true}, so paths must match exactly. With
      // exactly one route loaded, only one tab carries the active class.
      renderAdminPanel("/admin/records");

      const tabs = [
        screen.getByTestId("admin-tab-users"),
        screen.getByTestId("admin-tab-records"),
        screen.getByTestId("admin-tab-analytics"),
      ];
      const activeTabs = tabs.filter((tab) => /text-brand-700/.test(tab.className));
      expect(activeTabs).toHaveLength(1);
      expect(activeTabs[0]).toBe(screen.getByTestId("admin-tab-records"));
    });

    it('reflects active state via aria-current="page" (NavLink default)', () => {
      // react-router-dom NavLink emits aria-current="page" on the
      // matched anchor automatically when isActive is true. The tabs
      // that do NOT match the URL omit the attribute (or set it to a
      // non-"page" value, which is also acceptable - we assert the
      // negative form that "the attribute is not equal to 'page'").
      renderAdminPanel("/admin/records");

      const recordsTab = screen.getByTestId("admin-tab-records");
      expect(recordsTab).toHaveAttribute("aria-current", "page");

      const usersTab = screen.getByTestId("admin-tab-users");
      expect(usersTab).not.toHaveAttribute("aria-current", "page");

      const analyticsTab = screen.getByTestId("admin-tab-analytics");
      expect(analyticsTab).not.toHaveAttribute("aria-current", "page");
    });
  });

  // -------------------------------------------------------------------------
  // NavLink navigation
  // -------------------------------------------------------------------------

  describe("NavLink navigation", () => {
    it("clicking the Records tab navigates to /admin/records and swaps the outlet content", async () => {
      const user = userEvent.setup();
      renderAdminPanel("/admin/users");

      // Sanity: Users tab starts active and the Users page is shown.
      expect(screen.getByTestId("admin-tab-users").className).toMatch(/text-brand-700/);
      expect(screen.getByTestId("fake-users")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-records")).not.toBeInTheDocument();

      // Click the Records tab.
      await user.click(screen.getByTestId("admin-tab-records"));

      // The Records tab is now active and the Records page is rendered.
      expect(screen.getByTestId("admin-tab-records").className).toMatch(/text-brand-700/);
      expect(screen.getByTestId("admin-tab-users").className).toMatch(/text-slate-600/);
      expect(screen.getByTestId("fake-records")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-users")).not.toBeInTheDocument();
    });

    it("clicking the Analytics tab navigates to /admin/analytics", async () => {
      const user = userEvent.setup();
      renderAdminPanel("/admin/users");

      await user.click(screen.getByTestId("admin-tab-analytics"));

      expect(screen.getByTestId("admin-tab-analytics").className).toMatch(/text-brand-700/);
      expect(screen.getByTestId("fake-analytics")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-users")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fake-records")).not.toBeInTheDocument();
    });

    it("clicking back to Users navigates to /admin/users", async () => {
      const user = userEvent.setup();
      renderAdminPanel("/admin/records");

      // Sanity: Records is currently active.
      expect(screen.getByTestId("fake-records")).toBeInTheDocument();

      await user.click(screen.getByTestId("admin-tab-users"));

      expect(screen.getByTestId("admin-tab-users").className).toMatch(/text-brand-700/);
      expect(screen.getByTestId("fake-users")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-records")).not.toBeInTheDocument();
    });

    it("supports navigating across all three tabs in sequence", async () => {
      // Smoke test that NavLink + Outlet handle multiple consecutive
      // clicks correctly (no stale state, no double-render of inactive
      // child components, no leakage between tab visits).
      const user = userEvent.setup();
      renderAdminPanel("/admin/users");

      await user.click(screen.getByTestId("admin-tab-records"));
      expect(screen.getByTestId("fake-records")).toBeInTheDocument();

      await user.click(screen.getByTestId("admin-tab-analytics"));
      expect(screen.getByTestId("fake-analytics")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-records")).not.toBeInTheDocument();

      await user.click(screen.getByTestId("admin-tab-users"));
      expect(screen.getByTestId("fake-users")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-analytics")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Outlet renders child route
  // -------------------------------------------------------------------------

  describe("Outlet renders child route", () => {
    it("renders the Users child component inside the outlet at /admin/users", () => {
      renderAdminPanel("/admin/users");

      const outlet = screen.getByTestId("admin-panel-outlet");
      const fakeUsers = screen.getByTestId("fake-users");

      expect(outlet).toContainElement(fakeUsers);
      expect(fakeUsers).toHaveTextContent("USERS PAGE");
    });

    it("renders the Records child component inside the outlet at /admin/records", () => {
      renderAdminPanel("/admin/records");

      const outlet = screen.getByTestId("admin-panel-outlet");
      const fakeRecords = screen.getByTestId("fake-records");

      expect(outlet).toContainElement(fakeRecords);
      expect(fakeRecords).toHaveTextContent("RECORDS PAGE");
    });

    it("renders the Analytics child component inside the outlet at /admin/analytics", () => {
      renderAdminPanel("/admin/analytics");

      const outlet = screen.getByTestId("admin-panel-outlet");
      const fakeAnalytics = screen.getByTestId("fake-analytics");

      expect(outlet).toContainElement(fakeAnalytics);
      expect(fakeAnalytics).toHaveTextContent("ANALYTICS PAGE");
    });

    it("renders only the matching child route (no leakage between routes)", () => {
      renderAdminPanel("/admin/records");

      // Only the Records page is mounted; the other two routes are not
      // rendered at all (each lives in its own <Route>, and react-router
      // matches exactly one).
      expect(screen.getByTestId("fake-records")).toBeInTheDocument();
      expect(screen.queryByTestId("fake-users")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fake-analytics")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Accessibility and semantics
  // -------------------------------------------------------------------------

  describe("accessibility and semantics", () => {
    it('places role="tablist" on the <ul> inside the nav', () => {
      renderAdminPanel("/admin/users");

      const nav = screen.getByTestId("admin-panel-nav");
      const tablist = within(nav).getByRole("tablist");
      expect(tablist).toBeInTheDocument();
      expect(tablist.tagName).toBe("UL");
    });

    it('places role="presentation" on each tab <li>', () => {
      renderAdminPanel("/admin/users");

      const nav = screen.getByTestId("admin-panel-nav");
      // role="presentation" hides the <li> from the accessibility tree
      // so screen readers announce the tab navigation as a tablist
      // rather than a list. RTL exposes the elements via the role
      // selector, but only when they are actively returned by the a11y
      // tree query - getAllByRole works for "presentation" because RTL
      // explicitly exposes presentation elements.
      const presentationItems = within(nav).getAllByRole("presentation", {
        hidden: true,
      });
      expect(presentationItems.length).toBeGreaterThanOrEqual(3);

      // Filter to <li> elements (the role may also appear on other
      // structural elements; we want the tab list items specifically).
      const liItems = presentationItems.filter((el) => el.tagName === "LI");
      expect(liItems).toHaveLength(3);
    });

    it('places aria-label="Admin sections" on the <nav>', () => {
      renderAdminPanel("/admin/users");

      const nav = screen.getByRole("navigation", { name: /admin sections/i });
      expect(nav).toBe(screen.getByTestId("admin-panel-nav"));
    });

    it("has overflow-x-auto on the tab list for mobile responsiveness", () => {
      renderAdminPanel("/admin/users");

      // overflow-x-auto is applied to the <ul role="tablist">, which
      // sits inside the <nav>. On narrow viewports the three tabs
      // remain readable by scrolling horizontally instead of
      // collapsing to icons or wrapping to a second line.
      const tablist = screen.getByRole("tablist");
      expect(tablist.className).toMatch(/overflow-x-auto/);
    });

    it("renders the nav with all three tabs in a single tablist", () => {
      renderAdminPanel("/admin/users");

      const tablist = screen.getByRole("tablist");
      const tabs = within(tablist).getAllByTestId(/^admin-tab-/);
      expect(tabs).toHaveLength(3);
    });

    it("places aria-hidden on each decorative icon", () => {
      renderAdminPanel("/admin/users");

      // Each tab's Lucide icon is marked aria-hidden="true" so the
      // surrounding <span>{label}</span> is the accessible name. The
      // icon is the first SVG inside the tab anchor.
      const usersTab = screen.getByTestId("admin-tab-users");
      const icon = usersTab.querySelector("svg");
      expect(icon).not.toBeNull();
      expect(icon).toHaveAttribute("aria-hidden", "true");
    });
  });

  // -------------------------------------------------------------------------
  // RoleGate defense-in-depth (UI hiding for non-Admin sessions)
  // -------------------------------------------------------------------------

  describe("RoleGate defense-in-depth (UI hiding for non-Admin sessions)", () => {
    /**
     * Mount AdminPanel inside <RoleGate role="Admin"> exactly the way
     * the production router (router.tsx) wraps the /admin subtree.
     * Verifies that AdminPanel renders or hides based on the session
     * role, exercising the secondary defense layer per AAP Sec 0.7.1
     * invariant 7 (the backend's @requires_role decorator is the
     * authoritative gate; this is UX courtesy only).
     */
    function renderGated(role: SessionRole) {
      return renderWithMockedSession(
        <Routes>
          <Route
            path="/admin"
            element={
              <RoleGate role="Admin">
                <AdminPanel />
              </RoleGate>
            }
          >
            <Route path="users" element={<div data-testid="fake-users">USERS PAGE</div>} />
          </Route>
        </Routes>,
        buildSession(role),
        { initialEntries: ["/admin/users"] },
      );
    }

    /**
     * Mount AdminPanel inside <RoleGate role="Admin"> with NO session
     * (unauthenticated) to verify the gate hides the panel even before
     * the session has been hydrated. Production routes the user to
     * /login via <ProtectedRoute>, but here we exercise the gate's
     * isolated behavior: no session means no role match, so children
     * are not rendered.
     */
    function renderGatedUnauthenticated() {
      return renderWithMockedSession(
        <Routes>
          <Route
            path="/admin"
            element={
              <RoleGate role="Admin">
                <AdminPanel />
              </RoleGate>
            }
          >
            <Route path="users" element={<div data-testid="fake-users">USERS PAGE</div>} />
          </Route>
        </Routes>,
        null,
        { initialEntries: ["/admin/users"] },
      );
    }

    it("renders AdminPanel for Admin sessions", () => {
      renderGated("Admin");

      // The full layout shell is visible: root, header, nav, and tabs.
      expect(screen.getByTestId("admin-panel")).toBeInTheDocument();
      expect(screen.getByTestId("admin-panel-header")).toBeInTheDocument();
      expect(screen.getByTestId("admin-panel-nav")).toBeInTheDocument();
      expect(screen.getByTestId("admin-tab-users")).toBeInTheDocument();
      expect(screen.getByTestId("admin-tab-records")).toBeInTheDocument();
      expect(screen.getByTestId("admin-tab-analytics")).toBeInTheDocument();
    });

    it("does NOT render AdminPanel for Contributor sessions", () => {
      renderGated("Contributor");

      // The RoleGate fallback is undefined (defaults to null) so the
      // entire AdminPanel subtree is omitted from the DOM.
      expect(screen.queryByTestId("admin-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-panel-header")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-panel-nav")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-tab-users")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-tab-records")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-tab-analytics")).not.toBeInTheDocument();
    });

    it("does NOT render AdminPanel for Viewer (Sales Rep) sessions", () => {
      renderGated("Viewer");

      // Same expectation as Contributor: the gate denies non-Admin
      // roles regardless of which non-Admin role is in play.
      expect(screen.queryByTestId("admin-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-panel-nav")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-tab-users")).not.toBeInTheDocument();
    });

    it("does NOT render AdminPanel when there is no session (unauthenticated)", () => {
      renderGatedUnauthenticated();

      // No session means no role to compare against; the gate falls
      // through to the default null fallback and the panel is hidden.
      expect(screen.queryByTestId("admin-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-panel-nav")).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-tab-users")).not.toBeInTheDocument();
    });
  });
});
