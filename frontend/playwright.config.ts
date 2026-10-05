import { defineConfig } from "@playwright/test";

// Requires the backend (http://localhost:8000) and the Vite dev server (npm run dev) to be running,
// and ideally the IoT simulator (python simulator/run_simulator.py --no-traffic).
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5173",
    viewport: { width: 1500, height: 950 },
    screenshot: "only-on-failure",
  },
});
