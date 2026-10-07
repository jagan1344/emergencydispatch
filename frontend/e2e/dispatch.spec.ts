import { expect, Page, test } from "@playwright/test";
import fs from "node:fs";

const SHOTS = process.env.SCREENSHOT_DIR || "../docs/screenshots";
const shot = async (page: Page, name: string) => {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
};

async function login(page: Page, user = "dispatcher", pw = "dispatch123") {
  await page.goto("/login");
  await page.fill("input[name=username]", user);
  await page.fill("input[name=password]", pw);
  await page.click("button:has-text('Sign in')");
  await expect(page.getByText("Priority queue")).toBeVisible();
}

test("rejects bad credentials", async ({ page }) => {
  await page.goto("/login");
  await page.fill("input[name=username]", "dispatcher");
  await page.fill("input[name=password]", "wrong");
  await page.click("button:has-text('Sign in')");
  await expect(page.getByRole("alert")).toContainText("Invalid username or password");
});

test("dispatch workflow: create emergency → dispatch → map → traffic event → re-route", async ({ page, request }) => {
  await login(page);
  await expect(page.getByText("Live feed")).toBeVisible();
  await shot(page, "01-dashboard");

  // --- create emergency through the form
  await page.click("text=New Emergency");
  const health = await (await request.get("/api/health")).json();
  await page.selectOption("select[name=emergency_type]", "accident");
  await page.selectOption("select[name=consciousness]", "UNRESPONSIVE");
  await page.selectOption("select[name=bleeding]", "SEVERE");
  await page.selectOption("select[name=injury_severity]", "SEVERE");
  await page.fill("input[name=heart_rate]", "142");
  await page.fill("input[name=respiratory_rate]", "31");
  await page.fill("input[name=oxygen_saturation]", "85");
  await page.check("input[name=breathing_difficulty]");
  // click on the map to place the incident (in picking mode markers/lines also set the location)
  const map = page.locator(".leaflet-container").first();
  const box = (await map.boundingBox())!;
  const centre = `${health.city.lat.toFixed(5)}, ${health.city.lon.toFixed(5)}`;
  await map.click({ position: { x: box.width * 0.4, y: box.height * 0.4 } });
  await expect(page.getByTestId("location")).not.toHaveValue(centre);
  await page.click("button:has-text('Preview severity')");
  await expect(page.locator(".preview")).toContainText("ML prediction");
  await shot(page, "02-create-emergency");
  await page.getByTestId("create-emergency").click();

  // --- details page: ML severity, dispatch explanation, assigned ambulance
  await expect(page).toHaveURL(/\/emergencies\/[0-9a-f-]{36}/);
  await expect(page.getByTestId("ml-severity")).toHaveText(/CRITICAL|HIGH/);
  await expect(page.getByTestId("dispatch-explanation")).toContainText("Final Dispatch Score");
  const amb = (await page.getByTestId("assigned-ambulance").innerText()).trim();
  expect(amb).toMatch(/^AMB-\d+/);
  await expect(page.getByTestId("candidates").locator("tbody tr")).not.toHaveCount(0);
  const incidentId = page.url().split("/").pop()!;
  await shot(page, "03-emergency-details");

  // --- ambulance visible on the live map
  await page.click("text=Live Map");
  await expect(page.locator(`[data-amb="${amb}"]`)).toBeVisible();
  await shot(page, "04-live-map");

  // --- traffic control page renders road network tools
  await page.click("text=Traffic Control");
  await expect(page.getByText("Congested / blocked roads")).toBeVisible();

  // --- block a road on the ambulance's remaining route -> automatic re-route
  const token = await page.evaluate(() => localStorage.getItem("ems_token"));
  const auth = { Authorization: `Bearer ${token}` };
  const detail = await (await request.get(`/api/emergencies/${incidentId}`, { headers: auth })).json();
  const active = detail.routes.find((r: any) => r.active);
  const full = await (await request.get(`/api/routes/${active.id}`, { headers: auth })).json();
  const progress = full.progress_m || 0;
  // Blocking a road ahead has two legitimate outcomes: a drivable detour exists -> ROUTE RECALCULATED, or the road
  // is a bottleneck with no drivable alternative -> ROUTE UNAVAILABLE (dispatcher review; never a walking-speed
  // "route"). Roads ahead are tried in turn until a detour exists; an unavailable outcome must show its banner.
  const ahead = [...new Set(full.segments.filter((s: any) => s.road_id && s.cum_distance_m > progress + 150)
    .map((s: any) => s.road_id as string))];
  expect(ahead.length).toBeGreaterThan(2);
  const order = [...ahead.slice(Math.floor(ahead.length / 2)), ...ahead.slice(0, Math.floor(ahead.length / 2))];
  let road = "";
  for (const candidate of order.slice(0, 6)) {
    const r = await request.post("/api/traffic/events", { headers: auth, data: { event_type: "BLOCK", road_id: candidate } });
    expect(r.status()).toBe(201);
    const outcome = page.getByTestId("reroute-banner").or(page.getByTestId("route-unavailable-banner"));
    await expect(outcome).toBeVisible({ timeout: 20_000 });
    if (await page.getByTestId("reroute-banner").isVisible()) { road = candidate; break; }
    await expect(page.getByTestId("route-unavailable-banner")).toContainText("Dispatcher review required");
    await request.post("/api/traffic/events", { headers: auth, data: { event_type: "CLEAR", road_id: candidate } });
    await page.getByTestId("route-unavailable-banner").getByText("dismiss").click();
  }
  expect(road, "no road ahead with a drivable detour").not.toBe("");
  await expect(page.getByTestId("reroute-banner")).toContainText("ROUTE RECALCULATED");
  await shot(page, "05-traffic-reroute");

  await page.goto(`/emergencies/${incidentId}`);
  await expect(page.getByTestId("routes-table")).toContainText("road blocked ahead");
  await shot(page, "06-route-recalculated");
  await request.post("/api/traffic/events", { headers: auth, data: { event_type: "CLEAR", road_id: road } });

  // --- analytics page renders data from the backend
  await page.click("text=Analytics");
  await expect(page.getByText("Re-routing log")).toBeVisible();
  await expect(page.getByText("road blocked ahead").first()).toBeVisible();
  await shot(page, "07-analytics");
});

test("viewer role is read-only", async ({ page }) => {
  await login(page, "viewer", "viewer123");
  await page.click("text=Traffic Control");
  await expect(page.getByText("Random traffic step")).toHaveCount(0);
});
