// PostCSS configuration for the Sales-Connections SPA.
//
// Plugins (in order):
//   1. tailwindcss - Compiles Tailwind directives in src/styles/index.css
//      using the content globs and theme defined in tailwind.config.ts.
//   2. autoprefixer - Adds vendor prefixes (e.g., -webkit-) to support the
//      browsers listed in package.json's "browserslist" field.
//
// Per AAP Sec 0.2.3 and 0.3.4: PostCSS 8.x + Autoprefixer 10.x.
// CommonJS module format (`.cjs`) ensures compatibility regardless of the
// package.json `"type": "module"` setting (Node 20 strict ESM/CJS interop).
//
// Plugin ordering rationale:
//   tailwindcss MUST run before autoprefixer because Tailwind compiles
//   @tailwind/@apply/@layer directives into raw CSS, which then becomes the
//   input that autoprefixer scans for declarations needing vendor prefixes.
//   PostCSS preserves declaration order from the `plugins` object, so the
//   order below is the executed order.
//
// What is intentionally NOT here:
//   - tailwindcss/nesting: Tailwind 3.x ships with built-in nesting support
//     via @apply and @layer; the project does not author nested CSS in raw
//     .css files, so the dedicated nesting plugin is unnecessary.
//   - cssnano: Vite already minifies CSS via esbuild's faster code path
//     (vite.config.ts -> build defaults). Adding cssnano would duplicate
//     work and slow production builds.

module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
