import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

// One bundle, so the formatters read the same i18n instance the test switches.
const { outputFiles } = buildSync({
  stdin: {
    contents: 'export { setLanguage, initialize } from "./i18n"; export { formatCompact, formatMoney, formatSignedRatio } from "./lib/format";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)),
    loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "esm",
  tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
  loader: { ".json": "json" },
});
const { setLanguage, initialize, formatCompact, formatMoney, formatSignedRatio } =
  await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);

await initialize();

test("Chinese amounts preserve small transaction differences; English and share counts retain their units", async () => {
  await setLanguage("zh");
  assert.equal(formatMoney(-29_412_345, { signed: true }), "−$2,941万");
  assert.equal(formatMoney(92_150_000), "$9,215万");
  assert.equal(formatMoney(123_456_789), "$1.23亿");
  assert.equal(formatMoney(9_850), "$9,850");
  assert.equal(formatMoney(9_999), "$9,999");
  assert.equal(formatMoney(10_000), "$1.00万");
  assert.equal(formatMoney(19_890), "$1.99万");
  assert.equal(formatMoney(19_000), "$1.90万");
  assert.equal(formatMoney(-19_890, { signed: true }), "−$1.99万");
  assert.equal(formatMoney(99_999), "$10.00万");
  assert.equal(formatMoney(100_000), "$10万");
  assert.equal(formatMoney(1_480_000), "$148万");
  assert.equal(formatMoney(99_996_000), "$10,000万");
  assert.equal(formatMoney(100_000_000), "$1.00亿");
  assert.equal(formatMoney(1_234.5), "$1,235");
  // Share counts keep two decimals of 万.
  assert.equal(formatCompact(20_678_630), "2067.86万");
  await setLanguage("en");
  assert.equal(formatMoney(19_890), "$19.89K");
  assert.equal(formatMoney(19_890, { language: "zh" }), "$1.99万");
  assert.equal(formatMoney(-29_412_345, { signed: true }), "−$29.41M");
  assert.equal(formatCompact(20_678_630), "20.68M");
});

test("ratios are signed percents; zero after rounding has no sign", async () => {
  await setLanguage("en");
  assert.equal(formatSignedRatio("0.021"), "+2.1%");
  assert.equal(formatSignedRatio("-0.0633"), "−6.3%");
  assert.equal(formatSignedRatio("0.0004"), "0.0%");
});
