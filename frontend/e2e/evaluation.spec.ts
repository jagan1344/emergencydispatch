import { expect, Page, test } from "@playwright/test";
import fs from "node:fs";

const SHOTS = process.env.SCREENSHOT_DIR || "../docs/screenshots";

async function login(page: Page, user: string, pw: string) {
  await page.goto("/login");
  await page.fill("input[name=username]", user);
  await page.fill("input[name=password]", pw);
  await page.click("button:has-text('Sign in')");
  await expect(page.getByText("Priority queue")).toBeVisible();
}

test("evaluation page: admin runs a small experiment and sees the comparison", async ({ page }) => {
  test.setTimeout(240_000);
  await login(page, "admin", "admin123");
  await page.click("text=Evaluation");
  await expect(page.getByTestId("evaluation-page")).toBeVisible();
  await page.getByLabel("Scenarios").fill("2");
  await page.getByTestId("run-experiment").click();
  await expect(page.getByTestId("experiment-job")).toContainText("RUNNING");
  await expect(page.getByTestId("experiment-job")).toContainText(/COMPLETED|PARTIAL/, { timeout: 200_000 });
  const table = page.getByTestId("comparison-table");
  await expect(table).toContainText("Response time");
  for (const s of ["BASELINE", "SEVERITY", "TRAFFIC", "HOSPITAL", "FULL"]) await expect(table).toContainText(s);
  await expect(page.getByTestId("experiment-meta")).toContainText(/traffic prediction (LEARNED|FALLBACK)/);
  if (fs.existsSync(SHOTS)) await page.screenshot({ path: `${SHOTS}/12-evaluation.png`, fullPage: true });
});

test("evaluation page is read-only for viewers", async ({ page }) => {
  await login(page, "viewer", "viewer123");
  await page.goto("/evaluation");
  await expect(page.getByTestId("evaluation-page")).toBeVisible();
  await expect(page.getByTestId("run-experiment")).toHaveCount(0);
  await expect(page.getByTestId("experiment-runs")).toBeVisible();
  await expect(page.getByTestId("calibration-report")).toContainText("Brier score");
  await expect(page.getByTestId("calibration-report")).toContainText("ECE");
  await expect(page.getByTestId("traffic-comparison")).toContainText("Persistence");
  await expect(page.getByTestId("traffic-comparison")).toContainText("Rule fallback");
});
