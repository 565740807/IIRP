import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

// One bundle, so the formatters read the same i18n instance the test switches.
const { outputFiles } = buildSync({
  stdin: {
    contents: 'export { setLanguage } from "./i18n"; export { formatCompact, formatMoney, formatSignedRatio } from "./lib/format";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)),
    loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "esm",
  tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
  loader: { ".json": "json" },
});
const { setLanguage, formatCompact, formatMoney, formatSignedRatio } =
  await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);

test("Chinese amounts use whole 万 below 1 亿, English keeps K/M/B; share counts keep decimals", () => {
  setLanguage("zh");
  assert.equal(formatMoney(-29_412_345, { signed: true }), "−$2,941万");
  assert.equal(formatMoney(92_150_000), "$9,215万");
  assert.equal(formatMoney(123_456_789), "$1.23亿");
  assert.equal(formatMoney(99_996_000), "$1亿");
  assert.equal(formatMoney(1_234.5), "$1,235");
  // Share counts keep two decimals of 万.
  assert.equal(formatCompact(20_678_630), "2067.86万");
  setLanguage("en");
  assert.equal(formatMoney(-29_412_345, { signed: true }), "−$29.41M");
  assert.equal(formatCompact(20_678_630), "20.68M");
});

test("ratios are signed percents; zero after rounding has no sign", () => {
  setLanguage("en");
  assert.equal(formatSignedRatio("0.021"), "+2.1%");
  assert.equal(formatSignedRatio("-0.0633"), "−6.3%");
  assert.equal(formatSignedRatio("0.0004"), "0.0%");
});
