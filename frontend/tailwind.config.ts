import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}", "./tests/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Brand palette aligned with the Blitzy reveal.js deck for visual
        // consistency between the executive presentation and the SPA.
        brand: {
          DEFAULT: "#0F62FE",
          50: "#F0F4FF",
          100: "#D6E4FF",
          500: "#0F62FE",
          600: "#0043CE",
          700: "#002D9C",
          900: "#001141",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};

export default config;
