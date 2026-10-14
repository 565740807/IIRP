import { test as base, expect, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

export const output = process.env.IIRP_E2E_OUTPUT_DIR!;
export const sample = JSON.parse(fs.readFileSync(path.join(output, "fixtures.json"), "utf8"));
const translations = Object.fromEntries(["en", "zh"].map(language => [language,
  JSON.parse(fs.readFileSync(new URL(`../src/locales/${language}/translation.json`, import.meta.url), "utf8"))]));

export const test = base.extend<{ text: (key: string) => string }>({
  text: async ({}, use, info) => { await use(key => translations[info.project.name][key]); },
  page: async ({ page, context, baseURL }, use, info) => {
    const errors: string[] = [];
    const untranslated = new Set<string>();
    await page.exposeFunction("reportUntranslated", (keys: string[]) => keys.forEach(key => untranslated.add(key)));
    await context.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        const check = () => {
          const keys = document.body.innerText.match(/\b(?:ui|patterns\.ui)\.[a-z_]+(?:\.[a-z_\d]+)+/g);
          if (keys) void (window as any).reportUntranslated(keys);
        };
        new MutationObserver(check).observe(document.body, { subtree: true, childList: true, characterData: true });
        check();
      });
    });
    await context.addInitScript(language => localStorage.setItem("iirp.language", language), info.project.name);
    await page.clock.setFixedTime(new Date(sample.clock));
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(baseURL!).origin) {
        errors.push(`External request: ${url.href}`);
        await route.abort();
      } else await route.continue();
    });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    page.on("response", response => {
      // No API 404 is expected with these complete local fixtures. Future
      // exceptions must name the exact endpoint and why its absence is normal.
      if (new URL(response.url()).pathname.startsWith("/api/") && response.status() >= 400)
        errors.push(`${response.status()} ${response.url()}`);
    });
    page.on("requestfailed", request => {
      // Route navigation may cancel an obsolete query; other failures are errors.
      if (request.url().includes("/api/") && request.failure()?.errorText !== "net::ERR_ABORTED")
        errors.push(`${request.failure()?.errorText} ${request.url()}`);
    });
    await expect.poll(async () => {
      try {
        const response = await page.request.get(`${baseURL}/api/v1/home`);
        return new Date((await response.json()).observed_at).toISOString();
      } catch { return "starting"; }
    }, { message: "The web worker thread must use the same fixed clock as Chromium" })
      .toBe(new Date(sample.clock).toISOString());
    await use(page);
    await info.attach("browser-errors", { body: JSON.stringify(errors), contentType: "application/json" });
    await info.attach("untranslated-keys", { body: JSON.stringify([...untranslated]), contentType: "application/json" });
    expect([...untranslated], "No message keys may flash during rendering or language changes").toEqual([]);
    expect(errors, "Console, page errors, API failures and external requests").toEqual([]);
  },
});

export async function translated(page: Page) {
  await expect(page.locator("body")).not.toContainText(/\b(?:ui|patterns\.ui)\.[a-z_]+(?:\.[a-z_]+)+/);
}

export async function homeRows(page: Page) {
  await expect(page.locator("main [data-group-id]").filter({ hasText: /synthetic/i }).first()).toBeVisible();
}
export { expect };
