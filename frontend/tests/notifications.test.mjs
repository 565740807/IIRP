import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const notifications = await load("lib/notifications.ts");

const item = (id, created_at, fields = {}) => ({ id, created_at, kind: "trade", person_key: "sec:0001767470", issuer_id: "0001326380", read: false, data: { source: "sec", trade_id: "e1" }, ...fields });

test("turning desktop notifications on never replays older ones", () => {
  const items = [item("b", "2026-10-09T15:01:00Z"), item("a", "2026-10-09T15:00:00Z")];
  const first = notifications.unseen(items, null);
  assert.deepEqual(first.fresh, []);
  assert.equal(first.newest, "2026-10-09T15:01:00Z");
});

test("only notifications newer than the last one shown appear, oldest first", () => {
  const items = [item("c", "2026-10-09T15:03:00Z"), item("b", "2026-10-09T15:02:00Z"), item("a", "2026-10-09T15:00:00Z")];
  const next = notifications.unseen(items, "2026-10-09T15:01:00Z");
  assert.deepEqual(next.fresh.map((entry) => entry.id), ["b", "c"]);
  assert.equal(next.newest, "2026-10-09T15:03:00Z");
});

test("a notification opens the trade, the person or the company", () => {
  assert.equal(notifications.notificationHref(item("a", "x")), "/transactions/e1");
  assert.equal(notifications.notificationHref(item("b", "x", { data: { source: "house", trade_id: "t1" } })), "/notable/trades/t1");
  assert.equal(notifications.notificationHref(item("c", "x", { person_key: "house:Nancy Pelosi|CA11", data: { source: "house", trade_id: null } })),
    "/notable/house/Nancy%20Pelosi%7CCA11");
  assert.equal(notifications.notificationHref(item("d", "x", { kind: "rule_cluster" })), "/companies/0001326380");
});
