import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { sectorChoice, SECTOR_FORM } = await load("lib/insiderOverview.ts");
const { choiceFrom, withChoice } = await load("lib/urlChoice.ts");
const { parseSectorState, sectorSearch, DEFAULT_STATE } = await load("lib/sectorState.ts");
const SECTORS = ["energy", "financials", "information_technology"];

test("the sector filter takes all, a known sector or unclassified; anything else falls back to all", () => {
  assert.equal(sectorChoice("all", SECTORS), "all");
  assert.equal(sectorChoice("unclassified", SECTORS), "unclassified");
  assert.equal(sectorChoice("financials", SECTORS), "financials");
  assert.equal(sectorChoice("nope", SECTORS), "all");
  assert.equal(sectorChoice("Financials", null), "all");
  // Before the server has named its sectors, a well-formed id is kept.
  assert.equal(sectorChoice("financials", null), "financials");
  assert.ok(SECTOR_FORM.test("real_estate") && !SECTOR_FORM.test("real-estate"));
});

test("sector, index and tab live together in the overview link", () => {
  let params = new URLSearchParams("index=sp500&days=30");
  params.set("sector", "information_technology");
  params = withChoice(params, "tab", "large_buys", "cluster_buys");
  const link = params.toString();
  assert.equal(link, "index=sp500&days=30&sector=information_technology&tab=large_buys");
  const back = new URLSearchParams(link);
  assert.equal(sectorChoice(back.get("sector"), SECTORS), "information_technology");
  assert.equal(back.get("index"), "sp500");
  assert.equal(choiceFrom(back, "tab", ["cluster_buys", "large_buys"], "cluster_buys"), "large_buys");
  // Switching the tab keeps the filters.
  const next = withChoice(back, "tab", "cluster_buys", "cluster_buys");
  assert.equal(next.toString(), "index=sp500&days=30&sector=information_technology");
});

test("the sector tables sort by the Insider column through the link", () => {
  const state = { ...DEFAULT_STATE, sort: "insider", desc: true };
  assert.equal(sectorSearch(state), "sort=insider.desc");
  assert.deepEqual(parseSectorState(new URLSearchParams("sort=insider.asc")), { ...DEFAULT_STATE, sort: "insider", desc: false });
});
