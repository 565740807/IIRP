import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const notable = await load("lib/notable.ts");

const trade = (id, fields = {}) => ({
  id, source: "house", price_ticker: "NVDA", transaction_date: "2026-09-20", published: "2026-10-08",
  published_basis: "2026-10-07", issuer_id: null, ...fields,
});

test("window prices are asked for the rows on screen only", () => {
  const rows = [trade("a"), trade("b", { price_ticker: "AAPL" }), trade("c", { price_ticker: "MSFT" })];
  const keys = notable.windowKeys(rows, ["b"]);
  assert.deepEqual(keys.map((key) => key.ticker), ["AAPL", "AAPL"]);
  assert.deepEqual(notable.windowKeys(rows, []), []);
});

test("each coded trade asks from its trade date and from the close before publication", () => {
  const keys = notable.windowKeys([trade("a", { issuer_id: "0001045810" })], ["a"]);
  assert.deepEqual(keys, [
    { ticker: "NVDA", date: "2026-09-20", issuer_id: "0001045810" },
    { ticker: "NVDA", date: "2026-10-07", issuer_id: "0001045810" },
  ]);
});

test("bonds, funds and rows without a code ask for nothing", () => {
  const rows = [trade("bond", { price_ticker: null }), trade("fund", { price_ticker: null }), trade("nodate", { transaction_date: null })];
  assert.deepEqual(notable.windowKeys(rows, ["bond", "fund", "nodate"]), [{ ticker: "NVDA", date: "2026-10-07", issuer_id: null }]);
});

test("a row opens the SEC trade page or the government trade page", () => {
  assert.equal(notable.tradeHref({ id: "e1", source: "sec" }), "/transactions/e1");
  assert.equal(notable.tradeHref({ id: "t1", source: "oge" }), "/notable/trades/t1");
});
