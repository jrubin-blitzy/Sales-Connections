import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_DEV_API_PROXY_TARGET ?? "http://localhost:5000";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    envPrefix: ["VITE_"],
    server: {
      port: 5173,
      strictPort: false,
      proxy: {
        // Forward backend traffic during dev so the SPA does not need CORS.
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
        "/auth": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
        "/healthz": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
        "/readyz": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
      },
    },
    build: {
      outDir: "dist",
      target: "es2022",
      sourcemap: mode !== "production",
      emptyOutDir: true,
      rollupOptions: {
        output: {
          // Predictable chunking simplifies CDN cache invalidation.
          manualChunks: {
            react: ["react", "react-dom", "react-router-dom"],
            query: ["@tanstack/react-query"],
            ui: ["lucide-react", "clsx"],
          },
        },
      },
    },
    preview: {
      port: 4173,
      strictPort: false,
    },
  };
});
