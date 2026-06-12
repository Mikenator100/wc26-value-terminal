import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // dev mode talks to a locally running datalayer.service on the same
    // /api paths the deployed (same-origin) build uses
    proxy: { "/api": "http://localhost:8000" },
  },
});
