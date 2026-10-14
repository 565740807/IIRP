import { test, expect, homeRows, translated, sample, output } from "./fixtures";
import fs from "node:fs";
import path from "node:path";

// Bundle boundaries do not vary with wall time; the full smoke suite covers every clock.
test.skip(!sample.clock.includes("T15:"), "Bundle boundaries are checked once per language");

const manifest = JSON.parse(fs.readFileSync(new URL("../dist/.vite/manifest.json", import.meta.url), "utf8")) as Record<string, { file: string }>;

test("home loads its language and defers charts, tables and dialogs", async ({ page }, info) => {
  const scripts: string[] = [];
  page.on("response", response => {
    if (response.request().resourceType() === "script") scripts.push(new URL(response.url()).pathname);
  });
  await page.goto("/");
  await homeRows(page);
  const current = info.project.name;
  const other = current === "en" ? "zh" : "en";
  await page.waitForLoadState("networkidle");
  fs.writeFileSync(path.join(output, `home-scripts-${current}.json`), JSON.stringify([...new Set(scripts)], null, 2));
  await expect.poll(() => scripts).toContain("/" + manifest[`src/generated/locales/${current}/translation.json`].file);
  expect(scripts).not.toContain("/" + manifest[`src/generated/locales/${other}/translation.json`].file);
  for (const [key, value] of Object.entries(manifest)) {
    if (/features\/[^/]+\/(?:locales|generated)\//.test(key)) {
      if (key.endsWith(`/generated/entry-${current}.json`)) expect(scripts).toContain("/" + value.file);
      else expect(scripts).not.toContain("/" + value.file);
    }
  }
  expect(scripts.filter(url => /(?:vendor-echarts|vendor-zrender|vendor-table|SettingsDialog|SystemStatus|Tasks-|column-menu|WatchlistNewsDetails|InsiderNewsDetail)/.test(url))).toEqual([]);
  await page.getByRole("radio", { name: other === "zh" ? "中文" : "English", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", other === "zh" ? "zh-CN" : "en");
  expect(scripts).toContain("/" + manifest[`src/generated/locales/${other}/translation.json`].file);
  for (const [key, value] of Object.entries(manifest)) {
    if (key.includes("features/") && key.endsWith(`/generated/entry-${other}.json`)) expect(scripts).toContain("/" + value.file);
  }
  await translated(page);
  await page.goto("/companies/0000000123");
  await expect(page.locator("main canvas:visible").first()).toBeVisible();
  expect(scripts.some(url => url.includes("vendor-echarts"))).toBe(true);
});

test("lazy route shows skeletons while its JavaScript loads", async ({ page }) => {
  await page.goto("/");
  await homeRows(page);
  await page.route("**/assets/Entity-*.js", async route => {
    await new Promise(resolve => setTimeout(resolve, 800));
    await route.continue();
  });
  await page.locator('main a[href="/companies/0000000123"]').first().click();
  await expect(page.locator('main [role="status"]').first()).toBeVisible();
  await expect(page.locator("main canvas:visible").first()).toBeVisible();
  await translated(page);
});

// Routing requests through Playwright disables the HTTP cache. Each full
// navigation also creates a fresh document and i18next resource store.
test("sidebar routes render translated text on cold direct navigation", async ({ page }) => {
  await page.goto("/");
  await homeRows(page);
  const links = await page.locator("aside nav a").evaluateAll(nodes => nodes.map(node => node.getAttribute("href")!));
  for (const href of links) {
    await page.goto(href);
    await expect(page.locator("body > #root header h1").first()).toBeVisible();
    if (href === "/") await homeRows(page);
    else if (href === "/data") await expect(page.locator("main button[role=switch]").first()).toBeVisible();
    else await expect(page.locator("main table:visible, main input:visible, main textarea:visible").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    await translated(page);
  }
});

test("SEC summary owns its translations without health-message preloading", async ({ page, text }) => {
  // A healthy response carries no message code that could accidentally preload
  // settings. Translation readiness must come from the rendering component.
  await page.route("**/api/v1/system/health", route => route.fulfill({ json: {
    status: "ok", checked_at: sample.clock, groups: [],
  } }));
  await page.route("**/assets/settings-*.js", async route => {
    await new Promise(resolve => setTimeout(resolve, 600));
    await route.continue();
  });
  await page.goto("/data");
  const contact = page.getByRole("button", { name: `${text("ui.sec_contact.title")}: ${text("ui.sec_contact.status.none")}`, exact: true });
  await expect(contact).toBeVisible();
  await translated(page);
  await contact.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog").getByText(text("ui.sec_contact.email"), { exact: true })).toBeVisible();
  await translated(page);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: text("ui.nav.system"), exact: true }).click();
  await expect(page.getByRole("dialog").getByText(text("ui.sec_contact.title"), { exact: true })).toBeVisible();
  await translated(page);
});
