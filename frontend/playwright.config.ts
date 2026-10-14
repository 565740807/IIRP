import { defineConfig } from "@playwright/test";
import path from "node:path";

const output = process.env.IIRP_E2E_OUTPUT_DIR;
if (!output) throw new Error("IIRP_E2E_OUTPUT_DIR is required (outside the repository)");

export default defineConfig({
  testDir: ".",
  testMatch: ["e2e/**/*.spec.ts", "src/features/*/e2e/**/*.spec.mjs"],
  outputDir: path.join(output, "test-results"),
  reporter: [["list"], ["json", { outputFile: path.join(output, "results.json") }]],
  timeout: 30_000,
  globalTimeout: 180_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 2,
  retries: 0,
  use: {
    baseURL: process.env.IIRP_E2E_BASE_URL ?? "http://127.0.0.1:18081",
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "en", use: { locale: "en-US" } },
    { name: "zh", use: { locale: "zh-CN" } },
  ],
});
