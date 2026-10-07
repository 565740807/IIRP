import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

const { outputFiles } = buildSync({
  stdin: {
    contents: 'export { localize, tm } from "./i18n";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)),
    loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "esm",
});
const { localize, tm } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
const msg = (code, params = {}) => JSON.stringify({ code, params });

test("messages render the current Chinese text, nested and with list formatters", () => {
  assert.equal(tm(msg("stage.waiting_channel", { source: "Yahoo", stage: msg("stage.market_history") })),
    "等待 Yahoo 通道 · 补齐历史日线");
  assert.equal(tm(msg("events.set_title.earnings_more", { tickers: ["AAPL", "MSFT", "NVDA"], first: "2018", last: "2026" })),
    "AAPL、MSFT、NVDA 等 · 财报 2018—2026");
  assert.equal(tm(msg("common.items", { items: [msg("market.bar.dated", { day: "2026-01-02", reason: msg("market.bar.jump") }), "旧文字"] })),
    "2026-01-02：相邻交易日收盘变化达到 50%，保留原值待核对；旧文字");
  assert.ok(tm(msg("http.request_too_large", { size: 1234567, limit: 524288 }))
    .startsWith("本次请求至少 1,234,567 字节，当前接口最多 524,288 字节"));
  assert.equal(tm({ code: "job.not_found", params: {} }), "任务不存在。");
});

test("responses are localized in place; data with a code field and old text stay", () => {
  const response = {
    title: msg("batch.title.sec_latest"),
    rows: [{ code: "P", kind: msg("insider.kind.derivative", { kind: msg("insider.action.grant_or_award") }) }],
    error: "升级前写入的中文原文",
    value: 3,
  };
  assert.deepEqual(localize(response), {
    title: "更新最新 Insider",
    rows: [{ code: "P", kind: "衍生品 · 授予或奖励" }],
    error: "升级前写入的中文原文",
    value: 3,
  });
});
