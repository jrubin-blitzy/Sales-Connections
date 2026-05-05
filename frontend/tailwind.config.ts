/**
 * Tailwind CSS configuration for the Sales-Connections SPA.
 *
 * Consumed by:
 *   - frontend/postcss.config.cjs (registers tailwindcss as a PostCSS plugin)
 *   - frontend/src/styles/index.css (uses the @tailwind base/components/utilities directives)
 *   - frontend/src/features/connections/InvolvementBadge.tsx (consumes theme.colors.involvement.*)
 *   - frontend/src/features/connections/StatusChip.tsx (consumes theme.colors.outreach.*)
 *
 * The configuration declares:
 *   - content globs for the JIT engine to scan at build time
 *   - the class-based dark-mode strategy (forward-looking; MVP is light-only)
 *   - extended theme tokens for the brand palette, involvement indicator (F-003),
 *     outreach status chip (F-005), font stack, responsive breakpoints, transition
 *     timing, and elevation shadows
 *
 * Type-safety: the literal config object is paired with `satisfies Config` so that
 * downstream tooling (IntelliJ Tailwind plugin, IDE IntelliSense) can autocomplete
 * the exact tokens defined here rather than the generic `Config` shape.
 */

import type { Config } from "tailwindcss";

// ---------------------------------------------------------------------------
// Default export: Tailwind 3.x configuration object
// ---------------------------------------------------------------------------

export default {
  // -------------------------------------------------------------------------
  // Content globs
  //
  // The JIT engine scans these files at build time to extract used class names
  // and emit only those utilities into the production bundle. Test files are
  // intentionally excluded so that test-only utility usages do not bloat the
  // shipped CSS.
  // -------------------------------------------------------------------------
  content: ["./index.html", "./src/**/*.{ts,tsx,html}"],

  // -------------------------------------------------------------------------
  // Dark-mode strategy
  //
  // `class` enables dark mode by toggling a `dark` class on <html> or <body>
  // rather than reacting to the user's `prefers-color-scheme` media query.
  // MVP is light-mode only; this strategy is forward-looking so a future
  // toggle component can flip the entire UI without re-architecting tokens.
  // -------------------------------------------------------------------------
  darkMode: "class",

  // -------------------------------------------------------------------------
  // Theme extensions
  //
  // `extend` merges with Tailwind's defaults rather than replacing them; the
  // out-of-the-box spacing, typography, and color scales remain available.
  // -------------------------------------------------------------------------
  theme: {
    extend: {
      colors: {
        // ---------------------------------------------------------------
        // Brand palette
        //
        // Aligned with the Blitzy reveal.js executive deck for visual
        // consistency between leadership materials and the SPA. The full
        // 50..950 ramp matches Tailwind's standard color-token shape so
        // the palette can be substituted into any color-aware utility
        // (bg-brand-500, text-brand-700, ring-brand-300, etc.).
        // The canonical palette decision is recorded in
        // docs/decision-log.md per the Explainability rule.
        // ---------------------------------------------------------------
        brand: {
          50: "#eff6ff",
          100: "#dbeafe",
          200: "#bfdbfe",
          300: "#93c5fd",
          400: "#60a5fa",
          500: "#3b82f6", // primary action
          600: "#2563eb",
          700: "#1d4ed8",
          800: "#1e40af",
          900: "#1e3a8a",
          950: "#172554",
        },

        // ---------------------------------------------------------------
        // Involvement indicator colors (F-003) - three semantic states
        //
        // Each state exposes coordinated bg / fg / border tokens so a
        // single token namespace governs the entire badge appearance.
        // Consumed by frontend/src/features/connections/InvolvementBadge.tsx.
        //
        // Class examples:
        //   bg-involvement-warm-intro-bg
        //   text-involvement-warm-intro-fg
        //   border-involvement-warm-intro-border
        // ---------------------------------------------------------------
        involvement: {
          "warm-intro": {
            bg: "#dcfce7", // green-100
            fg: "#166534", // green-800
            border: "#86efac", // green-300
          },
          "soft-reference": {
            bg: "#fef3c7", // amber-100
            fg: "#92400e", // amber-800
            border: "#fcd34d", // amber-300
          },
          "target-only": {
            bg: "#e0e7ff", // indigo-100
            fg: "#3730a3", // indigo-800
            border: "#a5b4fc", // indigo-300
          },
        },

        // ---------------------------------------------------------------
        // Outreach status colors (F-005) - four semantic states
        //
        // Each state exposes coordinated bg / fg / border tokens for the
        // status chip rendered in the connection feed and detail view.
        // Consumed by frontend/src/features/connections/StatusChip.tsx.
        //
        // Class examples:
        //   bg-outreach-not-started-bg
        //   text-outreach-in-progress-fg
        //   border-outreach-contacted-border
        // ---------------------------------------------------------------
        outreach: {
          "not-started": {
            bg: "#f1f5f9", // slate-100
            fg: "#334155", // slate-700
            border: "#cbd5e1", // slate-300
          },
          "in-progress": {
            bg: "#dbeafe", // blue-100
            fg: "#1e40af", // blue-800
            border: "#93c5fd", // blue-300
          },
          contacted: {
            bg: "#f3e8ff", // purple-100
            fg: "#6b21a8", // purple-800
            border: "#d8b4fe", // purple-300
          },
          closed: {
            bg: "#dcfce7", // green-100
            fg: "#14532d", // green-900
            border: "#86efac", // green-300
          },
        },
      },

      // -----------------------------------------------------------------
      // Font stack
      //
      // Inter is the preferred SaaS-app sans-serif; the system-ui fallback
      // chain provides a polished default on every OS if Inter is not
      // bundled or loaded. MVP does not bundle Inter; a future iteration
      // may add @fontsource/inter via npm with a decision-log entry.
      // -----------------------------------------------------------------
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          "Roboto",
          '"Helvetica Neue"',
          "Arial",
          '"Noto Sans"',
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          '"Liberation Mono"',
          '"Courier New"',
          "monospace",
        ],
      },

      // -----------------------------------------------------------------
      // Responsive breakpoints
      //
      // Adds `xs` (360px) for small phones in addition to the standard
      // Tailwind set, addressing the AAP mobile-responsive constraint
      // (the connection feed re-flows columns on narrow viewports).
      // -----------------------------------------------------------------
      screens: {
        xs: "360px",
        sm: "640px",
        md: "768px",
        lg: "1024px",
        xl: "1280px",
        "2xl": "1536px",
      },

      // -----------------------------------------------------------------
      // Transition timing
      //
      // Default duration tuned for the Toast slide-in and Modal fade.
      // -----------------------------------------------------------------
      transitionDuration: {
        DEFAULT: "150ms",
      },

      // -----------------------------------------------------------------
      // Elevation shadows
      //
      // A small, opinionated shadow scale used by feed rows, detail
      // cards, and modals. Consistent elevation prevents visual drift
      // across the SPA's surfaces.
      // -----------------------------------------------------------------
      boxShadow: {
        card: "0 1px 2px 0 rgb(0 0 0 / 0.05), 0 1px 3px 0 rgb(0 0 0 / 0.1)",
        "card-hover": "0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)",
        modal: "0 25px 50px -12px rgb(0 0 0 / 0.25)",
      },
    },
  },

  // -------------------------------------------------------------------------
  // Plugins
  //
  // No third-party Tailwind plugins are required for MVP. Form styling is
  // handled with explicit utilities in the AddEditConnectionForm component
  // (F-001) for tighter visual control. If form styling proves repetitive,
  // @tailwindcss/forms can be added with a corresponding decision-log entry
  // per the Explainability rule.
  // -------------------------------------------------------------------------
  plugins: [],
} satisfies Config;
