import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, proxy API calls to a local `uvicorn deal_desk.api:app` so the UI stays same-origin.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/reviews": api, "/health": api } },
});
