import { expect, Page, test } from "@playwright/test";
import fs from "node:fs";

const SHOTS = process.env.SCREENSHOT_DIR || "../docs/screenshots";

async function login(page: Page) {
  await page.goto("/login");
  await page.fill("input[name=username]", "dispatcher");
  await page.fill("input[name=password]", "dispatch123");
  await page.click("button:has-text('Sign in')");
  await expect(page.getByText("Priority queue")).toBeVisible();
}

test("decision panels: confidence, explanation, counterfactuals, traffic, trace", async ({ page, request }) => {
  const tok = (await (await request.post("/api/auth/login", { data: { username: "dispatcher", password: "dispatch123" } })).json()).access_token;
  const auth = { Authorization: `Bearer ${tok}` };
  const h = await (await request.get("/api/health")).json();
  // make sure units are free for this test
  await request.post("/api/simulation/reset", { headers: auth, data: {} });
  const inc = await (await request.post("/api/emergencies", { headers: auth, data: {
    emergency_type: "accident", patient_age: 40, heart_rate: 145, respiratory_rate: 32, oxygen_saturation: 84,
    consciousness: "UNRESPONSIVE", bleeding: "SEVERE", injury_severity: "SEVERE", accident_type: "ROAD",
    breathing_difficulty: true, latitude: h.city.lat + 0.003, longitude: h.city.lon - 0.004 } })).json();
  expect(inc.status).toBe("DISPATCHED");
  await login(page);
  await page.goto(`/emergencies/${inc.id}`);
  await expect(page.getByTestId("ai-decision")).toContainText("AUTO DISPATCH");
  await expect(page.getByTestId("ai-decision")).toContainText("%");
  await expect(page.getByTestId("why-selected")).toContainText(`${inc.assigned_ambulance} was selected because`);
  await expect(page.getByTestId("decision-factors")).toContainText("Capability");
  const firstWhyNot = page.locator("[data-testid=alternatives] button").first();
  await firstWhyNot.click();
  await expect(page.locator(".why-not").first()).toContainText(/scores|unsuitable|better individual score/);
  await expect(page.getByTestId("traffic-panel")).toContainText("ETA (predicted");
  await expect(page.getByTestId("decision-trace")).toContainText("Automatic dispatch authorized");
  await expect(page.getByTestId("decision-trace")).toContainText("route candidates evaluated");
  await expect(page.getByTestId("decision-summary")).toContainText("reallocation considered");
  await expect(page.getByTestId("decision-summary")).toContainText("rerouting considered");
  await expect(page.getByTestId("route-status")).toContainText("Route status");
  await expect(page.getByTestId("route-status")).toContainText("Remaining distance");
  await expect(page.getByTestId("route-checkpoint")).toBeVisible();
  await expect(page.locator("[data-testid=alternatives] [data-testid=rejection-codes]").first()).toContainText(/[A-Z_]{5,}/);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/09-decision-panels.png`, fullPage: true });
  await request.post(`/api/emergencies/${inc.id}/cancel`, { headers: auth });
});

test("predictions are visible on traffic and hospital pages", async ({ page }) => {
  await login(page);
  await page.click("text=Traffic Control");
  await expect(page.getByTestId("traffic-model")).toContainText(/traffic-(rf|fallback)/);
  await page.click("text=Hospitals");
  await expect(page.getByText("Predicted (est.)")).toBeVisible();
  await expect(page.getByTestId("hospital-table")).toContainText("min");
});
