import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// owner: builder-C
// v5 frontend mounts at `/` on FastAPI dashboard (port 8081) per spec §4.1.
// In dev (port 5174) we proxy /api/* to the running dashboard so REST + WS
// work without CORS gymnastics. Code-split heavy panels per spec §4.1.
export default defineConfig({
  base: "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5174,
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
          react: ["react", "react-dom"],
          query: ["@tanstack/react-query"],
          charts: ["recharts"],
        },
      },
    },
  },
});
