import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { parseRange, rangeParam, rangeQuery, monthsBefore, parseN, todayEt } = await load("lib/insiderPrefs.ts");

test("range choices round-trip through the link", () => {
  for (const value of ["6m", "12m", "10t", "50t", "2025-01-01..2025-06-30"]) assert.equal(rangeParam(parseRange(value)), value);
  for (const value of ["0m", "999m", "0t", "2025-06-30..2025-01-01", "x", ""]) assert.equal(parseRange(value), null);
});

test("months count back by calendar month and clamp to the month's last day", () => {
  assert.equal(monthsBefore("2026-10-07", 6), "2026-04-07");
  assert.equal(monthsBefore("2026-08-31", 6), "2026-02-28");
  assert.equal(monthsBefore("2026-03-31", 13), "2025-02-28");
});

test("a range becomes the history query of the company/person page", () => {
  assert.deepEqual(rangeQuery({ mode: "months", months: 6 }, "2026-10-07"), { start_date: "2026-04-07", end_date: "2026-10-07" });
  assert.deepEqual(rangeQuery({ mode: "count", count: 10 }), { recent_count: 10 });
  assert.deepEqual(rangeQuery({ mode: "dates", start: "2026-01-02", end: "2026-02-03" }), { start_date: "2026-01-02", end_date: "2026-02-03" });
});

test("n is a whole number within the configured limit; today is the US Eastern date", () => {
  assert.equal(parseN("5"), 5);
  assert.equal(parseN("61"), null);
  assert.equal(parseN("0"), null);
  assert.equal(todayEt(new Date("2026-10-08T03:00:00Z")), "2026-10-07");
});
