// Start every E2E run from a known operational state: all units back at base and on duty, no open incidents.
import { request } from "@playwright/test";

export default async function globalSetup() {
  const base = process.env.E2E_BASE_URL || "http://127.0.0.1:5173";
  const ctx = await request.newContext({ baseURL: base });
  const login = await ctx.post("/api/auth/login", { data: { username: "dispatcher", password: "dispatch123" } });
  const { access_token } = await login.json();
  await ctx.post("/api/simulation/reset", { headers: { Authorization: `Bearer ${access_token}` }, data: {} });
  await ctx.dispose();
}
