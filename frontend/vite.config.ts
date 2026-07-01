import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": backendTarget,
      "/knowledge": backendTarget,
      "/contacts": backendTarget,
      "/draft": backendTarget,
      "/agent/android": backendTarget,
      "/agent/bumble": backendTarget,
      "/agent/browser": backendTarget
    }
  }
});
