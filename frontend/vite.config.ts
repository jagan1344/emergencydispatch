import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// The dev server proxies /api and /ws to the FastAPI backend, so the browser talks to one origin.
// `npm run dev:https` (mode "https") serves over https with a self-signed certificate: phones only allow GPS (geolocation) on
// secure origins, so the Crew GPS page needs this when opened from a phone on the same Wi-Fi.
const backend = process.env.VITE_BACKEND_URL || "http://localhost:8000";
const proxy = {
  "/api": backend,
  "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
};

export default defineConfig(({ mode }) => {
  const https = mode === "https";
  return {
    plugins: [react(), ...(https ? [basicSsl()] : [])],
    server: { port: 5173, host: https ? "0.0.0.0" : undefined, proxy },
    preview: { port: 4173, proxy },
  };
});
