import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const watchlist = await load("lib/watchlist.ts");

const list = (...tickers) => ({
  items: tickers.map((ticker) => ({ ticker, name: null, issuer_id: null, local: true, added_at: "2026-10-01T00:00:00Z" })),
  limit: 500, acknowledged_at: null,
});

test("an added code shows at once, in the server's order, and only once", () => {
  const added = watchlist.withCode(list("AAPL", "TSM"), "NVDA", true);
  assert.deepEqual(added.items.map((item) => item.ticker), ["AAPL", "NVDA", "TSM"]);
  assert.deepEqual(watchlist.withCode(added, "NVDA", true).items.map((item) => item.ticker), ["AAPL", "NVDA", "TSM"]);
  // An entry already there keeps its own fields (the day it was added).
  assert.equal(watchlist.withCode(list("AAPL"), "AAPL", true).items[0].added_at, "2026-10-01T00:00:00Z");
});

test("a removed code leaves at once", () => {
  assert.deepEqual(watchlist.withCode(list("AAPL", "NVDA"), "AAPL", false).items.map((item) => item.ticker), ["NVDA"]);
});

test("the day a code was added is the US Eastern day", () => {
  assert.equal(watchlist.etDay("2026-10-12T03:00:00Z"), "2026-10-11");
  assert.equal(watchlist.etDay("2026-10-12T15:00:00Z"), "2026-10-12");
});

const news = await load("lib/watchlistNews.ts");
const entry = (key, ticker, at) => ({ key, ticker, at, summary: key, detail: key });

test("news groups by stock, newest stock first, every source's entries together", () => {
  const stocks = news.newsByStock([
    entry("insider:NVDA", "NVDA", "2026-10-10T14:00:00Z"),
    entry("insider:AAPL", "AAPL", "2026-10-09T14:00:00Z"),
    entry("pattern:1", "AAPL", "2026-10-11T22:00:00Z"),
    entry("insider:TSM", "TSM", "2026-10-08T14:00:00Z"),
  ], null);
  assert.deepEqual(stocks.map((stock) => stock.ticker), ["AAPL", "NVDA", "TSM"]);
  assert.deepEqual(stocks[0].summaries.map((part) => part.key), ["insider:AAPL", "pattern:1"]);
  assert.equal(stocks[0].at, "2026-10-11T22:00:00Z");
});

test("with Insider entries only (no optional feature) the news is the Insider entries", () => {
  const stocks = news.newsByStock([entry("insider:NVDA", "NVDA", "2026-10-10T14:00:00Z")], null);
  assert.deepEqual(stocks.map((stock) => [stock.ticker, stock.summaries.length]), [["NVDA", 1]]);
});

test("entries up to a just-acknowledged time are hidden at once", () => {
  const items = [entry("insider:NVDA", "NVDA", "2026-10-10T14:00:00Z"), entry("pattern:2", "MSFT", "2026-10-12T14:00:00Z")];
  assert.deepEqual(news.newsByStock(items, "2026-10-11T00:00:00Z").map((stock) => stock.ticker), ["MSFT"]);
  assert.deepEqual(news.newsByStock(items, "2026-10-12T14:00:00Z"), []);
});
