import { test, expect, sample, output } from "./fixtures";
import fs from "node:fs";
import path from "node:path";

test("home cold navigation measurements", async ({ browser, baseURL }, info) => {
  test.skip(process.env.IIRP_E2E_SKIP_PERFORMANCE === "1" || info.project.name !== "en" || !sample.clock.includes("T15:"));
  const networkProfile = { latency: 20, downloadThroughput: 1_250_000, uploadThroughput: 1_250_000 };
  const samples = [];
  for (let iteration = 0; iteration < 3; iteration++) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
    try {
      const page = await context.newPage();
      const session = await context.newCDPSession(page);
      await session.send("Network.enable");
      await session.send("Network.emulateNetworkConditions", { offline: false, ...networkProfile });
      await page.clock.setFixedTime(new Date(sample.clock));
      await page.route("**/*", route => new URL(route.request().url()).origin === new URL(baseURL!).origin ? route.continue() : route.abort());
      const scripts: string[] = [];
      page.on("response", response => {
        if (response.request().resourceType() === "script") scripts.push(response.url());
      });
      const started = performance.now();
      await page.goto(baseURL!);
      await expect(page.locator("main [data-group-id]").filter({ hasText: /synthetic/i }).first()).toBeVisible();
      const milliseconds = performance.now() - started;
      // Include every initial script response without adding the settling wait to the first-table time.
      await page.waitForLoadState("networkidle");
      samples.push({ milliseconds, scripts });
    } finally { await context.close(); }
  }
  fs.writeFileSync(path.join(output, "home-performance.json"), JSON.stringify({ networkProfile, samples,
    median: samples.map(item => item.milliseconds).sort((a, b) => a - b)[1] }, null, 2));
});
