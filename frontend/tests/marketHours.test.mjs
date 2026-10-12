import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { globexOpen, indexFuturesTrading } = await load("lib/marketHours.ts");
// US Eastern daylight time in October: UTC−4.
const at = (iso) => new Date(iso);

test("index futures trade from Sunday evening to Friday afternoon, with a daily pause", () => {
  assert.equal(globexOpen(at("2026-10-10T16:00:00Z")), false, "Saturday noon");
  assert.equal(globexOpen(at("2026-10-11T21:00:00Z")), false, "Sunday 17:00 ET");
  assert.equal(globexOpen(at("2026-10-11T22:05:00Z")), true, "Sunday 18:05 ET");
  assert.equal(globexOpen(at("2026-10-13T21:30:00Z")), false, "Tuesday 17:30 ET pause");
  assert.equal(globexOpen(at("2026-10-13T23:00:00Z")), true, "Tuesday 19:00 ET");
  assert.equal(globexOpen(at("2026-10-16T20:59:00Z")), true, "Friday 16:59 ET");
  assert.equal(globexOpen(at("2026-10-16T21:01:00Z")), false, "Friday 17:01 ET");
});

test("futures are hidden on weekends, in the daily pause and when the last trade is stale", () => {
  const sunday = at("2026-10-11T23:00:00Z");
  assert.equal(indexFuturesTrading(sunday, "2026-10-11T22:55:00Z"), true);
  // Saturday: the source still reports a calendar-day session and Friday's last trade.
  assert.equal(indexFuturesTrading(at("2026-10-10T15:00:00Z"), "2026-10-09T20:59:00Z"), false);
  // A holiday closure inside the weekly window: no trade for hours.
  assert.equal(indexFuturesTrading(at("2026-11-26T20:00:00Z"), "2026-11-26T18:00:00Z"), false);
  assert.equal(indexFuturesTrading(sunday, null), false);
  assert.equal(indexFuturesTrading(sunday, "not a time"), false);
});
