import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate } from "node:timers/promises";
import ts from "typescript";
import { QueryClient } from "@tanstack/react-query";

const compiled = ts.transpileModule(
  readFileSync(new URL("../src/freshnessRefresh.ts", import.meta.url), "utf8"),
  {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
    },
  },
).outputText;
const helpers = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

assert.equal(helpers.marketPageVisible("/"), true);
assert.equal(helpers.marketPageVisible("/market/%5EGSPC"), true);
assert.equal(helpers.marketPageVisible("/analysis/monthly"), false);
assert.deepEqual(helpers.freshnessRequest("open", false), {
  reason: "open",
  sources: ["sec", "market"],
  market_visible: false,
  force: false,
});
assert.deepEqual(helpers.freshnessRequest("visible", false).sources, ["sec"]);
assert.deepEqual(helpers.freshnessRequest("visible", true).sources, [
  "sec",
  "market",
]);
assert.equal(helpers.freshnessRequest("manual", true).force, true);

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
function harness() {
  const state = {
    visible: true,
    marketVisible: false,
    calls: [],
    values: [],
    errors: [],
    checking: false,
  };
  const controller = helpers.createFreshnessRefresher({
    visible: () => state.visible,
    marketVisible: () => state.marketVisible,
    request: (payload) => {
      const response = deferred();
      state.calls.push({ payload, response });
      return response.promise;
    },
    started: (reason) => {
      state.checking = reason;
    },
    received: (value) => state.values.push(value),
    failed: (error) => state.errors.push(error),
    settled: () => {
      state.checking = false;
    },
  });
  return { state, controller };
}

// Simultaneous mount, focus, online and visibility notifications do not fan out.
{
  const { state, controller } = harness();
  const first = controller.ensure("open");
  assert.equal(
    state.checking,
    "open",
    "feedback begins before the server responds",
  );
  await controller.ensure("resume");
  await controller.ensure("resume");
  await controller.ensure("visible");
  assert.equal(state.calls.length, 1);
  state.calls[0].response.resolve({ batch_ids: ["shared-round"] });
  await first;
  assert.equal(state.checking, false);
  assert.deepEqual(state.values, [{ batch_ids: ["shared-round"] }]);
}

// Returning to the quote page while an unrelated-page check is pending renews demand.
{
  const { state, controller } = harness();
  const first = controller.ensure("open");
  state.marketVisible = true;
  await controller.ensure("open");
  assert.equal(state.calls.length, 1);
  state.calls[0].response.resolve({ round: 1 });
  await setImmediate();
  assert.equal(state.calls.length, 2);
  assert.equal(state.calls[1].payload.market_visible, true);
  state.calls[1].response.resolve({ round: 2 });
  await first;
  assert.deepEqual(state.values, [{ round: 1 }, { round: 2 }]);
}

// A manual check is retained even when the visible heartbeat is still pending.
{
  const { state, controller } = harness();
  const first = controller.ensure("visible");
  await controller.ensure("manual");
  state.marketVisible = true;
  await controller.ensure("open");
  state.calls[0].response.resolve({ round: 1 });
  await setImmediate();
  assert.equal(state.calls[1].payload.force, true);
  assert.equal(state.calls[1].payload.market_visible, true);
  state.calls[1].response.reject(new Error("Source unavailable"));
  await first;
  assert.deepEqual(
    state.values,
    [{ round: 1 }],
    "failure keeps previously readable status",
  );
  assert.equal(state.errors[0].message, "Source unavailable");
  assert.equal(state.checking, false);
}

// Hidden pages issue neither periodic work nor a queued check after hiding.
{
  const { state, controller } = harness();
  state.visible = false;
  await controller.ensure("visible");
  await controller.ensure("resume");
  assert.equal(state.calls.length, 0);
  state.visible = true;
  const first = controller.ensure("resume");
  state.marketVisible = true;
  await controller.ensure("open");
  state.visible = false;
  state.calls[0].response.resolve({ round: 1 });
  await first;
  assert.equal(state.calls.length, 1);
}

// An explicit manual action retains its priority if the user switches tabs while it waits.
{
  const { state, controller } = harness();
  const first = controller.ensure("visible");
  await controller.ensure("manual");
  state.visible = false;
  state.calls[0].response.resolve({ round: 1 });
  await setImmediate();
  assert.equal(state.calls.length, 2);
  assert.equal(state.calls[1].payload.force, true);
  state.calls[1].response.resolve({ round: 2 });
  await first;
}

// A late response from the previous component lifetime cannot change the new view.
{
  const { state, controller } = harness();
  const first = controller.ensure("open");
  controller.dispose();
  state.calls[0].response.resolve({ stale: true });
  await first;
  assert.deepEqual(state.values, []);
  await controller.ensure("resume");
  assert.equal(state.calls.length, 1);
}

// A slower GET cannot replace a more recent ensure response in the actual query cache.
{
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const response = deferred();
  const old = { observed_at: "2026-09-17T14:00:00+00:00", stage: "waiting" };
  const newest = {
    observed_at: "2026-09-17T14:00:05+00:00",
    stage: "checking",
  };
  const get = client.fetchQuery({
    queryKey: ["freshness"],
    queryFn: async () => {
      const value = await response.promise;
      return helpers.newestFreshness(client.getQueryData(["freshness"]), value);
    },
  });
  client.setQueryData(["freshness"], newest);
  response.resolve(old);
  await get;
  assert.deepEqual(client.getQueryData(["freshness"]), newest);
  client.setQueryData(["freshness"], (previous) =>
    helpers.newestFreshness(previous, old),
  );
  assert.deepEqual(client.getQueryData(["freshness"]), newest);
  client.clear();
}

// Partial source checks update that source without hiding the last observed market state.
{
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const market = {
    stage: "休市，已有报价可读",
    source_time: "2026-09-16T20:00:00Z",
  };
  const initial = {
    observed_at: "2026-09-17T14:00:00+00:00",
    sources: { market, sec: { stage: "等待 SEC 通道" } },
    batch_ids: ["initial"],
  };
  client.setQueryData(["freshness"], initial);
  const secOnly = {
    observed_at: "2026-09-17T14:00:05+00:00",
    sources: { sec: { stage: "下载并解析申报" } },
    batch_ids: ["sec-current"],
  };
  client.setQueryData(["freshness"], (previous) =>
    helpers.newestFreshness(previous, secOnly),
  );
  const merged = client.getQueryData(["freshness"]);
  assert.deepEqual(merged.sources.market, market);
  assert.deepEqual(merged.sources.sec, secOnly.sources.sec);
  assert.deepEqual(
    merged.batch_ids,
    ["sec-current"],
    "requested round comes from the current response",
  );
  assert.equal(merged.observed_at, secOnly.observed_at);
  client.setQueryData(["freshness"], (previous) =>
    helpers.newestFreshness(previous, initial),
  );
  assert.deepEqual(
    client.getQueryData(["freshness"]),
    merged,
    "late full-source read cannot regress a newer partial check",
  );
  const newest = {
    observed_at: "2026-09-17T14:00:10+00:00",
    sources: {
      market: { stage: "获取最新报价" },
      sec: { stage: "本轮已检查" },
    },
    batch_ids: [],
  };
  client.setQueryData(["freshness"], (previous) =>
    helpers.newestFreshness(previous, newest),
  );
  assert.deepEqual(client.getQueryData(["freshness"]), newest);
  client.clear();
}

assert.equal(
  helpers.quoteTime({
    status: "DAILY",
    source_time: "2026-09-17T14:00:00Z",
    as_of: "2026-09-16",
  }),
  "2026-09-16",
);
assert.equal(
  helpers.quoteTime({
    status: "DELAYED",
    source_time: "2026-09-17T14:00:00Z",
    as_of: "2026-09-17",
  }),
  "2026-09-17T14:00:00Z",
);
assert.equal(helpers.quoteStatusLabel("STALE"), "较早报价");
assert.equal(helpers.quoteStatusLabel("DELAYED"), "延迟报价");
console.log(
  "freshness presence, immediate feedback, coalescing, retained intent, hidden/unmount, late cache responses and quote timestamp checks passed",
);
