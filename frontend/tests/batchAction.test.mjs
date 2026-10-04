import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import { QueryClient, QueryObserver, MutationObserver } from "@tanstack/react-query";
import { fileURLToPath } from "node:url";

const { outputFiles } = buildSync({
  stdin: {
    contents: 'export { api, batchActionOptions } from "./api"; export { batchOptions } from "./researchQueries";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)),
    loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "esm",
});
const application = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const value = (id, status, updated_at) => ({
  batch_id: id, reused: false,
  batch: { id, analysis_id: `analysis-${id}`, status, updated_at, items: [] },
});

function harness(t) {
  const priorFetch = globalThis.fetch, priorDocument = globalThis.document;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const server = new Map(), calls = [], commands = [], reads = [], observers = [];
  globalThis.document = { hidden: false };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (init.method === "POST") {
      const gate = deferred();
      commands.push(gate);
      return new Response(JSON.stringify(await gate.promise), { status: 200 });
    }
    const id = decodeURIComponent(url.split("/batches/")[1]);
    const snapshot = server.get(id);
    assert(snapshot, `Unexpected GET ${url}`);
    const gate = reads.shift();
    const response = gate ? await Promise.race([
      gate.promise,
      new Promise((_, reject) => init.signal?.addEventListener("abort", () =>
        reject(new DOMException("Aborted", "AbortError")), { once: true })),
    ]) : snapshot;
    return new Response(JSON.stringify(response), { status: 200 });
  };
  const observe = (id) => {
    const observer = new QueryObserver(client, application.batchOptions(id));
    const unsubscribe = observer.subscribe(() => {});
    observers.push(unsubscribe);
    return observer;
  };
  t.after(() => {
    observers.forEach(unsubscribe => unsubscribe());
    client.clear();
    globalThis.fetch = priorFetch;
    globalThis.document = priorDocument;
  });
  return { client, server, calls, commands, reads, observe,
    command: (id, action) => new MutationObserver(client, application.batchActionOptions(client)).mutate({ id, action }),
  };
}

for (const stamp of ["older", "equal", "missing", "invalid"]) {
  test(`a delayed pause receipt cannot replace a newer server state (${stamp} timestamp)`, async t => {
    const h = harness(t);
    const currentStamp = "2026-10-03T12:00:02.000000+00:00";
    h.server.set("target", value("target", "RUNNING", currentStamp));
    h.server.set("other", value("other", "PAUSED", currentStamp));
    const target = h.observe("target");
    h.observe("other");
    await tick();
    const frozenKey = ["analysis", "analysis-target", "frozen-result"];
    const frozen = { result_id: "frozen-result", data: { note: "Preserve complete evidence" } };
    h.client.setQueryData(frozenKey, frozen);
    const frozenBefore = h.client.getQueryData(frozenKey);
    const pending = h.command("target", "pause");
    await tick();
    assert.equal(h.commands.length, 1);

    // The pause committed first. A second client has resumed this batch and
    // this observer has already read the resumed state before the old POST
    // response reaches the first client. Server timestamps may be ambiguous.
    await target.refetch();
    const receiptStamp = stamp === "older" ? "2026-10-03T12:00:01.000000+00:00"
      : stamp === "equal" ? currentStamp : stamp === "invalid" ? "unknown" : undefined;
    h.commands[0].resolve(value("target", "PAUSED", receiptStamp));
    await pending;
    await tick();
    assert.equal(target.getCurrentResult().data.batch.status, "RUNNING");
    assert.equal(h.client.getQueryData(frozenKey), frozenBefore);
    assert.equal(h.client.getQueryState(frozenKey).isInvalidated, false);
    assert.equal(h.calls.filter(call => call.url.endsWith("/batches/other")).length, 1);
    assert.equal(h.calls.filter(call => call.init.method === "POST").length, 1);
  });
}

test("a command cancels a pre-command read and a new batch ID keeps its own identity", async t => {
  const h = harness(t);
  h.server.set("parent", value("parent", "CANCELLED", "2026-10-03T12:00:01Z"));
  h.server.set("child", value("child", "QUEUED", "2026-10-03T12:00:02Z"));
  const parent = h.observe("parent"), child = h.observe("child");
  await tick();
  const oldGate = deferred();
  h.reads.push(oldGate);
  const oldRead = child.refetch();
  const oldCall = h.calls.at(-1);
  const pending = h.command("parent", "continue_remaining");
  await tick();
  h.server.set("child", value("child", "RUNNING", "2026-10-03T12:00:03Z"));
  h.commands[0].resolve(value("child", "QUEUED", "2026-10-03T12:00:02Z"));
  await pending;
  await tick();
  assert(oldCall.init.signal.aborted);
  oldGate.resolve(value("child", "QUEUED", "2026-10-03T12:00:02Z"));
  await oldRead;
  assert.equal(child.getCurrentResult().data.batch.status, "RUNNING");
  assert.equal(parent.getCurrentResult().data.batch.id, "parent");
  assert.equal(parent.getCurrentResult().data.batch.status, "CANCELLED");
  assert.equal(h.client.getQueryData(["batch", "child"]).batch_id, "child");
  assert.equal(h.calls.filter(call => call.init.method === "POST").length, 1);
});

test("a failed authoritative read retains its error and can retry without repeating the command", async t => {
  const h = harness(t);
  h.server.set("target", value("target", "RUNNING", "2026-10-03T12:00:01Z"));
  const target = h.observe("target");
  await tick();
  const pending = h.command("target", "pause");
  await tick();
  h.server.set("target", value("target", "PAUSED", "2026-10-03T12:00:02Z"));
  const failedRead = deferred();
  h.reads.push(failedRead);
  h.commands[0].resolve(value("target", "PAUSED", "2026-10-03T12:00:02Z"));
  await pending;
  await tick();
  const currentGet = h.calls.at(-1);
  // An unreadable response is a visible GET failure, not cache cancellation.
  assert.equal(currentGet.init.method, "GET");
  failedRead.resolve(undefined);
  await tick();
  assert.equal(target.getCurrentResult().isError, true);
  assert.equal(target.getCurrentResult().data.batch.status, "RUNNING");
  await target.refetch();
  assert.equal(target.getCurrentResult().isError, false);
  assert.equal(target.getCurrentResult().data.batch.status, "PAUSED");
  assert.equal(h.calls.filter(call => call.init.method === "POST").length, 1);
});

test("an unopened continuation is read from the server when first observed", async t => {
  const h = harness(t);
  h.server.set("parent", value("parent", "CANCELLED", "2026-10-03T12:00:01Z"));
  h.observe("parent");
  await tick();
  const pending = h.command("parent", "continue_remaining");
  await tick();
  h.server.set("child", value("child", "SUCCEEDED", "2026-10-03T12:00:03Z"));
  h.commands[0].resolve(value("child", "QUEUED", "2026-10-03T12:00:02Z"));
  await pending;
  await tick();
  const child = h.observe("child");
  await tick();
  assert.equal(child.getCurrentResult().data.batch.status, "SUCCEEDED");
  assert.equal(h.calls.filter(call => call.url.endsWith("/batches/child")).length, 1);
  assert.equal(h.client.getQueryData(["batch", "parent"]).batch.status, "CANCELLED");
});
