import { expect, test } from "@playwright/test";

// Emulates a crew phone: the browser Geolocation API reports positions along the planned route.
test("crew GPS page drives the ambulance with real (emulated) device positions", async ({ browser, request }) => {
  const login = await (await request.post("/api/auth/login", { data: { username: "dispatcher", password: "dispatch123" } })).json();
  const auth = { Authorization: `Bearer ${login.access_token}` };
  const health = await (await request.get("/api/health")).json();
  // create + dispatch an emergency via the API
  const inc = await (await request.post("/api/emergencies", { headers: auth, data: {
    emergency_type: "cardiac", patient_age: 70, heart_rate: 130, respiratory_rate: 26, oxygen_saturation: 89,
    systolic_bp: 92, consciousness: "VERBAL", bleeding: "NONE", injury_severity: "NONE", accident_type: "NONE",
    breathing_difficulty: true, chest_pain: true, latitude: health.city.lat + 0.004, longitude: health.city.lon - 0.004,
    auto_dispatch: true } })).json();
  const amb = inc.assigned_ambulance;
  expect(amb).toMatch(/^AMB-/);
  const route = (await (await request.get(`/api/emergencies/${inc.id}`, { headers: auth })).json()).routes.find((r: any) => r.active);
  const pts: [number, number][] = route.geometry;

  const ctx = await browser.newContext({ geolocation: { latitude: pts[0][0], longitude: pts[0][1] }, permissions: ["geolocation"] });
  const page = await ctx.newPage();
  await page.goto("/login");
  await page.fill("input[name=username]", "dispatcher");
  await page.fill("input[name=password]", "dispatch123");
  await page.click("button:has-text('Sign in')");
  await page.click("text=Crew GPS");
  await page.getByTestId("crew-unit").selectOption(amb);
  await page.getByTestId("crew-start").click();
  await expect(page.getByTestId("crew-source")).toHaveText("DEVICE", { timeout: 30_000 });

  // drive along the route: 1/3, 2/3, then the patient location
  for (const frac of [0.33, 0.66]) {
    const p = pts[Math.floor((pts.length - 1) * frac)];
    await ctx.setGeolocation({ latitude: p[0], longitude: p[1] });
    await page.waitForTimeout(1500);
  }
  const progress = (await (await request.get(`/api/routes/${route.id}`, { headers: auth })).json()).progress_m;
  expect(progress).toBeGreaterThan(route.distance_m * 0.3);
  await ctx.setGeolocation({ latitude: inc.latitude, longitude: inc.longitude });
  await expect.poll(async () => (await (await request.get(`/api/emergencies/${inc.id}`, { headers: auth })).json()).status,
    { timeout: 15_000 }).toBe("ARRIVED");
  await page.screenshot({ path: "../docs/screenshots/08-crew-gps.png" });
  await page.click("text=Stop & hand back to simulator");
  await request.post(`/api/emergencies/${inc.id}/cancel`, { headers: auth });
  await ctx.close();
});
