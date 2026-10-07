import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { codeKey, primarySummary, readableCase, side } = await load("lib/insider.ts");
const { readFeedWithRetry } = await load("feedRecovery.ts");

test("only Table I purchases and sales have a side", () => {
  assert.equal(side({ table: "I", code: "P" }), "buy");
  assert.equal(side({ table: "I", code: "S" }), "sell");
  assert.equal(side({ table: "II", code: "S" }), null);
  assert.equal(side({ table: "I", code: "M" }), null);
});

test("a line leads with a purchase or sale before larger other actions", () => {
  const summaries = [{ table: "I", code: "M", rows: 9 }, { table: "I", code: "S", rows: 1 }];
  assert.equal(primarySummary(summaries).code, "S");
  assert.equal(primarySummary([{ table: "I", code: "A", rows: 1 }, { table: "I", code: "F", rows: 3 }]).code, "F");
  assert.equal(codeKey({ code: "g" }), "ui.code.G");
  assert.equal(codeKey({ code: "?" }), "ui.code.other");
});

test("all-capital SEC titles become readable; acronyms and mixed case stay", () => {
  assert.equal(readableCase("PRESIDENT AND CEO"), "President and CEO");
  assert.equal(readableCase("EVP & CHIEF FINANCIAL OFFICER"), "EVP & Chief Financial Officer");
  assert.equal(readableCase("GIC Private Ltd"), "GIC Private Ltd");
  assert.equal(readableCase("Chief Sci Ofcr, Ophthalmology"), "Chief Sci Ofcr, Ophthalmology");
});

test("a 503 feed read is retried twice, other errors are not", async () => {
  let calls = 0;
  const busy = Object.assign(new Error("busy"), { status: 503 });
  await assert.rejects(readFeedWithRetry(async () => { calls += 1; throw busy; }, () => true), /busy/);
  assert.equal(calls, 3);
  calls = 0;
  await assert.rejects(readFeedWithRetry(async () => { calls += 1; throw Object.assign(new Error("gone"), { status: 409 }); }, () => true));
  assert.equal(calls, 1);
  assert.equal(await readFeedWithRetry(async () => "page", () => false), undefined, "a read for a closed view is dropped");
});
