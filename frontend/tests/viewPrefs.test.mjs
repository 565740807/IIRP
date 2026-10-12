import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const prefs = await load("lib/viewPrefs.ts");
const { choiceFrom, withChoice } = await load("lib/urlChoice.ts");
const { activeSection } = await load("lib/sectionNav.ts");

function memory() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    values,
  };
}

test("column choices are remembered per table and only where they differ from the defaults", () => {
  const store = memory();
  const hidden = ["ownership", "plan"];
  assert.deepEqual(prefs.readColumnChoices("trades", store), {});
  prefs.writeColumnChoices("trades", { plan: true, amount: false, ownership: false }, hidden, store);
  assert.deepEqual(prefs.readColumnChoices("trades", store), { plan: true, amount: false });
  assert.deepEqual(prefs.readColumnChoices("other", store), {});
  const visibility = prefs.columnVisibility(["amount", "plan", "ownership", "price"], hidden, prefs.readColumnChoices("trades", store));
  assert.deepEqual(visibility, { amount: false, plan: true, ownership: false, price: true });
  // Back to the defaults: nothing is stored any more.
  prefs.writeColumnChoices("trades", { plan: false, amount: true }, hidden, store);
  assert.equal(store.values.has(prefs.columnsKey("trades")), false);
});

test("unreadable or missing storage falls back to the defaults", () => {
  const store = memory();
  store.setItem(prefs.columnsKey("trades"), "{not json");
  assert.deepEqual(prefs.readColumnChoices("trades", store), {});
  store.setItem(prefs.columnsKey("trades"), JSON.stringify({ plan: "yes", amount: false }));
  assert.deepEqual(prefs.readColumnChoices("trades", store), { amount: false });
  assert.deepEqual(prefs.readColumnChoices("trades", null), {});
  assert.equal(prefs.readFold("details", false, null), false);
});

test("folded sections are remembered and the default is not stored", () => {
  const store = memory();
  assert.equal(prefs.readFold("analysis.details", false, store), false);
  prefs.writeFold("analysis.details", true, false, store);
  assert.equal(prefs.readFold("analysis.details", false, store), true);
  prefs.writeFold("analysis.details", false, false, store);
  assert.equal(store.values.size, 0);
  prefs.writeFold("analysis.box", false, true, store);
  assert.equal(prefs.readFold("analysis.box", true, store), false);
});

test("a tab or focus/all choice round-trips through the link and the default keeps it short", () => {
  const tabs = ["cluster_buys", "cluster_sales", "large_buys"];
  let params = new URLSearchParams("days=30");
  assert.equal(choiceFrom(params, "tab", tabs, "cluster_buys"), "cluster_buys");
  params = withChoice(params, "tab", "large_buys", "cluster_buys");
  assert.equal(params.toString(), "days=30&tab=large_buys");
  assert.equal(choiceFrom(params, "tab", tabs, "cluster_buys"), "large_buys");
  params = withChoice(params, "tab", "cluster_buys", "cluster_buys");
  assert.equal(params.toString(), "days=30");
  assert.equal(choiceFrom(new URLSearchParams("tab=pie"), "tab", tabs, "cluster_buys"), "cluster_buys");
  const rows = withChoice(new URLSearchParams("range=10t"), "rows", "all", "focus");
  assert.equal(rows.toString(), "range=10t&rows=all");
  assert.equal(choiceFrom(rows, "rows", ["focus", "all"], "focus"), "all");
});

test("the contents bar highlights the section being read", () => {
  const sections = [{ id: "summary", top: 120 }, { id: "heat", top: 600 }, { id: "ranking", top: 1400 }];
  assert.equal(activeSection(sections, 130), "summary");
  assert.equal(activeSection(sections, 50), "summary");
  assert.equal(activeSection(sections, 600), "heat");
  assert.equal(activeSection(sections, 1399), "heat");
  assert.equal(activeSection(sections, 2000), "ranking");
  assert.equal(activeSection(sections, 130, true), "ranking");
  assert.equal(activeSection([], 0), null);
});
