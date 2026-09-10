import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 8002,
    host: true,
    // Local dev only: forwards /api calls to the FastAPI backend running on
    // its default port so `npm run dev` works without a built image.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
