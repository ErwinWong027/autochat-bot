import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/knowledge": "http://127.0.0.1:8000",
      "/contacts": "http://127.0.0.1:8000",
      "/draft": "http://127.0.0.1:8000",
      "/agent/bumble": "http://127.0.0.1:8000",
      "/agent/browser": "http://127.0.0.1:8000"
    }
  }
});
