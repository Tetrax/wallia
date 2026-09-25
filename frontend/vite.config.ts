import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:13745",
      "/healthz": "http://127.0.0.1:13745",
    },
  },
});
