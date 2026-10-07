import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { applyDelta, appendPage, canApplyInPlace, freshMark, sortKey } = await load("lib/feedStream.ts");

const group = (id, accepted, days = ["2026-10-05"]) => ({
  id, issuer_id: id, accepted_at: accepted, transaction_dates: days, revision_id: `${id}-r`,
  revision_created_at: accepted, summary: [], transactions: [], trader_groups: [],
});
const stream = (groups, order = "accepted") => ({
  session: "s1", latest: "s1", order, groups, nextCursor: "c1", asOf: "2026-10-07T00:00:00Z", fresh: {}, dropped: [],
});

test("a new filing drops in at the top and is marked new; the watermark advances", () => {
  const base = stream([group("B", "2026-10-06T20:00:00-04:00"), group("A", "2026-10-06T23:00:00+00:00")]);
  const { stream: next, added } = applyDelta(base, [group("C", "2026-10-07T01:00:00+00:00")], [], "s2", 1000);
  assert.deepEqual(next.groups.map((g) => g.id), ["C", "B", "A"]);
  assert.deepEqual(added, ["C"]);
  assert.equal(next.latest, "s2");
  assert.equal(freshMark(next, "C", 2000), "new");
  assert.equal(freshMark(next, "C", 1000 + 12_000), null, "the mark fades");
  assert.equal(freshMark(next, "B", 2000), null);
});

test("mixed UTC offsets sort as instants", () => {
  // 20:00 -04:00 is 00:00Z, later than 23:00Z the day before.
  assert.ok(sortKey(group("B", "2026-10-06T20:00:00-04:00"), "accepted") > sortKey(group("A", "2026-10-06T23:00:00+00:00"), "accepted"));
});

test("a revised group moves to its new place and is marked updated; removed groups leave", () => {
  const base = stream([group("B", "2026-10-07T00:30:00Z"), group("A", "2026-10-07T00:10:00Z"), group("X", "2026-10-06T22:00:00Z")]);
  const { stream: next, added } = applyDelta(base, [group("A", "2026-10-07T01:00:00Z")], ["X"], "s2", 0);
  assert.deepEqual(next.groups.map((g) => g.id), ["A", "B"]);
  assert.deepEqual(added, []);
  assert.equal(freshMark(next, "A", 1), "updated");
  assert.deepEqual(next.dropped, ["X"]);
});

test("older pages append without duplicates and never bring back a removed group", () => {
  const base = { ...stream([group("A", "2026-10-07T01:00:00Z")]), dropped: ["X"] };
  const next = appendPage(base, [group("A", "2026-10-06T00:00:00Z"), group("X", "2026-10-05T00:00:00Z"), group("Y", "2026-10-04T00:00:00Z")], null);
  assert.deepEqual(next.groups.map((g) => g.id), ["A", "Y"]);
  assert.equal(next.groups[0].accepted_at, "2026-10-07T01:00:00Z", "the newer revision stays");
  assert.equal(next.nextCursor, null);
});

test("transaction order places a group by its latest trade date", () => {
  const base = stream([group("A", "2026-10-07T01:00:00Z", ["2026-10-06"]), group("B", "2026-10-07T00:00:00Z", ["2026-10-02"])], "transaction");
  const { stream: next } = applyDelta(base, [group("C", "2026-10-07T02:00:00Z", ["2026-10-01", "2026-10-03"])], [], "s2");
  assert.deepEqual(next.groups.map((g) => g.id), ["A", "C", "B"]);
});

test("updates merge in place only at the top with nothing selected; otherwise the pill waits", () => {
  assert.equal(canApplyInPlace(0, false), true);
  assert.equal(canApplyInPlace(80, false), true);
  assert.equal(canApplyInPlace(400, false), false);
  assert.equal(canApplyInPlace(0, true), false);
});
