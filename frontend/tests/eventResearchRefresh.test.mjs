import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const compiled = ts.transpileModule(
  readFileSync(
    new URL("../src/eventResearchRefresh.ts", import.meta.url),
    "utf8",
  ),
  {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
    },
  },
).outputText;
const { createEventResearchRefresher, eventFollowParams } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);
const pending = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
function harness() {
  const state = {
    context: {
      analysisId: "old",
      originId: "origin",
      navigation: "set:1:old:",
      epoch: 0,
      snapshotId: "",
      dirty: false,
      ready: true,
    },
    time: 100000,
    visible: true,
    calls: [],
    views: ["old-readable"],
    errors: [],
    busy: false,
  };
  const controller = createEventResearchRefresher({
    current: () => ({ ...state.context }),
    visible: () => state.visible,
    clock: () => state.time,
    request: (id, manual) => {
      const value = pending();
      state.calls.push({ id, manual, ...value });
      return value.promise;
    },
    received: (value) => state.views.push(value),
    failed: (error) => state.errors.push(error),
    started: () => {
      state.busy = true;
    },
    settled: () => {
      state.busy = false;
    },
  });
  return { state, controller };
}
{
  const { state, controller } = harness();
  const task = controller.ensure();
  assert.equal(state.busy, true);
  assert.deepEqual(state.views, ["old-readable"]);
  await controller.ensure();
  assert.equal(state.calls.length, 1);
  state.calls[0].resolve("new-readable");
  await task;
  assert.deepEqual(state.views, ["old-readable", "new-readable"]);
  state.context.analysisId = "new";
  await controller.ensure();
  assert.equal(state.calls.length, 1, "new child keeps origin cooldown");
  state.time += 60000;
  const next = controller.ensure();
  state.calls[1].resolve("newest");
  await next;
}
for (const change of [
  (s) => {
    s.context.dirty = true;
  },
  (s) => {
    s.context.ready = false;
  },
  (s) => {
    s.context.snapshotId = "fixed";
    s.context.navigation = "set:1:old:fixed";
  },
  (s) => {
    s.context.epoch += 2;
    s.context.dirty = false;
  }, // edit then revert is still a new intent
  (s) => {
    s.context.analysisId = "other";
  },
  (s) => {
    s.context.navigation = "set:2:old:";
  },
]) {
  const { state, controller } = harness();
  const task = controller.ensure();
  change(state);
  state.calls[0].resolve("late");
  await task;
  assert.deepEqual(
    state.views,
    ["old-readable"],
    "late response cannot replace changed intent",
  );
}
{
  const { state, controller } = harness();
  state.context.snapshotId = "fixed";
  await controller.ensure();
  assert.equal(state.calls.length, 0);
  const task = controller.ensure(true);
  assert.equal(state.calls[0].manual, true);
  state.calls[0].resolve("new-version");
  await task;
  assert.equal(state.views[1], "new-version");
}
{
  const { state, controller } = harness();
  state.context.dirty = true;
  await controller.ensure(true);
  assert.equal(state.calls.length, 0);
  state.context.dirty = false;
  state.visible = false;
  await controller.ensure();
  assert.equal(state.calls.length, 0);
  state.visible = true;
  const task = controller.ensure();
  state.calls[0].reject(new Error("source unavailable"));
  await task;
  assert.deepEqual(state.views, ["old-readable"]);
  assert.equal(state.errors[0].message, "source unavailable");
  const retry = controller.ensure(true);
  assert.equal(state.calls[1].manual, true);
  state.calls[1].resolve("retried");
  await retry;
}
{
  const { state, controller } = harness();
  const task = controller.ensure();
  controller.dispose();
  state.calls[0].resolve("late");
  await task;
  assert.deepEqual(state.views, ["old-readable"]);
}
{
  const original = new URLSearchParams(
    "kind=custom&set=set&version=1&a=old&result=frozen&result_ids=frozen&years=2018%2C2024&cutoff_date=2026-09-08",
  );
  const next = eventFollowParams(original, "new");
  assert.equal(next.get("a"), "new");
  assert.equal(next.has("result"), false);
  assert.equal(next.has("result_ids"), false);
  assert.equal(next.get("years"), "2018,2024");
  assert.equal(next.get("version"), "1");
  assert.equal(
    original.get("result"),
    "frozen",
    "immutable original link is unchanged",
  );
}
console.log(
  "Event follow-latest and fixed snapshots: retained result, coalescing, force retry, origin throttle, late draft/snapshot/version response rejection passed",
);

{
  const scopeCode = ts.transpileModule(readFileSync(new URL("../src/eventScopeCompatibility.ts", import.meta.url), "utf8"), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
  const { eventScopeConflict, eventAnalysisForImportedScope } = await import(`data:text/javascript;base64,${Buffer.from(scopeCode).toString("base64")}`);
  const old = {event_version:1,years:[2018,2019,2020,2021,2022,2023,2024,2025],date_window:"after5",benchmark:"^GSPC",excluded_years:[2020]};
  const scope = {year_start:2024,year_end:2024};
  const {state,controller}=harness();
  state.context.ready = !eventScopeConflict(old,scope);
  await controller.ensure();
  await controller.ensure(true);
  assert.equal(state.calls.length,0,"an incompatible saved scope does not automatically retry or force-post invalid years");
  assert.deepEqual(state.views,["old-readable"],"legacy and fixed content stay readable while a scope choice is needed");
  state.context.snapshotId="old-frozen";
  await controller.ensure();
  assert.equal(state.calls.length,0,"fixed legacy links remain read-only without an explicit new-study choice");
  const selected = eventAnalysisForImportedScope(old,scope);
  assert.equal(eventScopeConflict(selected,scope),null,"only the explicit new-study action constructs compatible input");
  assert.deepEqual(old.years,[2018,2019,2020,2021,2022,2023,2024,2025]);
  assert.equal(selected.date_window,"after5");
  assert.equal(selected.benchmark,"^GSPC");
}
console.log("legacy scope gates automatic/forced refresh and preserves old frozen reads until an explicit new-study choice");
