import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// V4 frontend mounts at /v4/ on FastAPI dashboard (port 8081).
// In dev (port 5173) we proxy /api/* to the running dashboard so SSE + REST
// work without CORS gymnastics.
export default defineConfig({
  base: "/v4/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: process.env.VITE_PROXY_TARGET || "http://127.0.0.1:8081",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
          charts: ["recharts"],
        },
      },
    },
  },
});
