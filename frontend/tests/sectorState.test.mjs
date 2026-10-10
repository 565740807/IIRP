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
  const state = { level: "sector", sector: null, view: "kline", period: "3m", sort: "ytd", desc: false, selected: ["XLK", "XLC"],
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

test("the industry group level and its sector round-trip, and a selection spans both levels", () => {
  const groups = ["SOXX", "SMH", "KBE"];
  const state = { ...DEFAULT_STATE, level: "group", sector: "information_technology", view: "heatmap",
    selected: ["XLK", "SOXX", "SMH"] };
  const search = sectorSearch(state);
  assert.equal(search, "level=group&sector=information_technology&view=heatmap&selected=XLK%2CSOXX%2CSMH");
  assert.deepEqual(parseSectorState(new URLSearchParams(search), [...ETFS, ...groups], ["information_technology"]), state);
  // Back at the sector level the group ETFs stay selected and the sector is dropped.
  const back = parseSectorState(new URLSearchParams(sectorSearch({ ...state, level: "sector" })), [...ETFS, ...groups]);
  assert.deepEqual(back, { ...state, level: "sector", sector: null });
  // All groups, and an unknown sector, have no sector.
  assert.equal(parseSectorState(new URLSearchParams("level=group"), ETFS).sector, null);
  assert.equal(parseSectorState(new URLSearchParams("level=group&sector=nope"), ETFS, ["energy"]).sector, null);
  assert.equal(parseSectorState(new URLSearchParams("sector=energy"), ETFS, ["energy"]).sector, null);
});
