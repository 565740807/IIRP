import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { DEFAULT_STATE, analysisLinks, parseSectorState, sectorSearch } = await load("lib/sectorState.ts");
const ETFS = ["XLK", "XLV", "XLF", "XLY", "XLP", "XLC", "XLI", "XLE", "XLB", "XLU", "XLRE"];

test("defaults leave the link empty", () => {
  assert.deepEqual(parseSectorState(new URLSearchParams(""), ETFS), DEFAULT_STATE);
  assert.equal(sectorSearch(DEFAULT_STATE), "");
});

test("view, period, sort, selection and chart choices round-trip through the link", () => {
  const state = { view: "kline", period: "3m", sort: "ytd", desc: false, selected: ["XLK", "XLC"],
    onlySelected: true, range: "1y", interval: "week" };
  const search = sectorSearch(state);
  assert.equal(search, "view=kline&period=3m&sort=ytd.asc&selected=XLK%2CXLC&only=1&range=1y&interval=week");
  assert.deepEqual(parseSectorState(new URLSearchParams(search), ETFS), state);
});

test("unknown values fall back and only-selected needs a selection", () => {
  const state = parseSectorState(new URLSearchParams("view=pie&period=2y&sort=volume.asc&selected=XLK,ZZZ,xlk&only=1&range=5y&interval=hour"), ETFS);
  assert.deepEqual(state, { ...DEFAULT_STATE, selected: ["XLK"], onlySelected: true });
  assert.equal(parseSectorState(new URLSearchParams("only=1"), ETFS).onlySelected, false);
  assert.equal(parseSectorState(new URLSearchParams("sort=1m"), ETFS).desc, true);
});

test("analysis links name the ETFs for the monthly and interval pages", () => {
  assert.deepEqual(analysisLinks(["XLK", "XLV"]), {
    monthly: "/analysis/monthly?tickers=XLK%2CXLV", interval: "/analysis/interval?tickers=XLK%2CXLV" });
});
