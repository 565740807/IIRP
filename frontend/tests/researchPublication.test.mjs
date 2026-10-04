import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const compiled = ts.transpileModule(
  readFileSync(new URL("../src/researchPublication.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } },
).outputText;
const { researchViewIdentity, captureResearchPublication, canPublishResearch, canStartResearchRefresh } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const state = {
    epoch: 0, view: researchViewIdentity("/analysis/monthly", "a=old"),
    navigation: "explicit-navigation-1", mounted: true, edited: false, analysisPending: false,
  };
  const availability = { analysisId: "old", frozen: false, visible: true, busy: false };
  const calls = [], visible = ["old-readable"], errors = [], cached = [];
  function refresh() {
    if (!canStartResearchRefresh(state, availability)) return Promise.resolve(false);
    availability.busy = true; // Same synchronous reservation as Analysis.launchRefresh.
    const ticket = captureResearchPublication(state);
    const request = deferred();
    calls.push(request);
    return request.promise.then(value => {
      cached.push(value); // Late immutable results may enter cache without navigation.
      if (canPublishResearch("refresh", ticket, state)) visible.push(value);
    }, error => {
      if (canPublishResearch("refresh", ticket, state)) errors.push(error.message);
    }).finally(() => { availability.busy = false; });
  }
  function submit() {
    state.epoch += 1;
    state.edited = false;
    state.analysisPending = true;
    const ticket = captureResearchPublication(state);
    const request = deferred();
    const settled = request.promise.then(value => {
      cached.push(value);
      if (canPublishResearch("submit", ticket, state)) visible.push(value);
    }, error => {
      if (canPublishResearch("submit", ticket, state)) errors.push(error.message);
    }).finally(() => { state.analysisPending = false; });
    return { ...request, settled };
  }
  return { state, availability, calls, visible, errors, cached, refresh, submit };
}

// Only a selected research or frozen result set changes the logical view.
assert.equal(researchViewIdentity("/analysis/monthly", "a=old"),
  researchViewIdentity("/analysis/monthly", "ticker=AAPL&a=old&current_year=2026&cutoff_date=2026-09-21&study=origin"));
assert.equal(researchViewIdentity("/analysis/monthly", "a=old&result_ids=b,a"),
  researchViewIdentity("/analysis/monthly", "result_ids=a,b,a&a=old"));
assert.notEqual(researchViewIdentity("/analysis/monthly", "a=old"),
  researchViewIdentity("/analysis/monthly", "a=old&result_ids=snapshot"));

// Deferred old refresh must not undo a fixed snapshot, edits, submission or navigation.
for (const [name, change] of [
  ["freeze same analysis", h => { h.state.view = researchViewIdentity("/analysis/monthly", "a=old&result_ids=fixed"); h.availability.frozen = true; }],
  ["edit draft", h => { h.state.edited = true; }],
  ["edit then revert", h => { h.state.epoch += 2; h.state.edited = false; }],
  ["submit new conditions", h => { h.state.epoch += 1; h.state.analysisPending = true; }],
  ["pending submission safety", h => { h.state.analysisPending = true; }],
  ["change analysis", h => { h.state.view = researchViewIdentity("/analysis/monthly", "a=other"); }],
  ["change page", h => { h.state.view = researchViewIdentity("/analysis/interval", "a=old"); }],
  ["explicit same URL navigation", h => { h.state.navigation = "explicit-navigation-2"; }],
  ["unmount", h => { h.state.mounted = false; }],
]) {
  const h = harness();
  const waiting = h.refresh();
  change(h);
  h.calls[0].resolve("late-old-result");
  await waiting;
  assert.deepEqual(h.visible, ["old-readable"], name);
  assert.deepEqual(h.cached, ["late-old-result"], `${name}: caching need not replace visible intent`);
  assert.equal(h.availability.busy, false, `${name}: request settles even when publication is rejected`);
}

// Automatic canonical replacements preserve publication and do not freeze the view.
{
  const h = harness();
  const waiting = h.refresh();
  h.state.view = researchViewIdentity("/analysis/monthly", "a=old&tickers=AAPL&historical_years=8&study=origin&current_year=2026");
  h.calls[0].resolve("fresh-result");
  await waiting;
  assert.deepEqual(h.visible, ["old-readable", "fresh-result"]);
}

// A submission may publish while its own mutation is pending; refresh may not.
{
  const h = harness();
  const old = h.refresh();
  const submission = h.submit();
  await h.refresh();
  assert.equal(h.calls.length, 1, "no old-research refresh while new conditions submit");
  h.calls[0].resolve("late-automatic");
  await old;
  assert.deepEqual(h.visible, ["old-readable"]);
  submission.resolve("new-conditions");
  await submission.settled;
  assert.deepEqual(h.visible, ["old-readable", "new-conditions"]);
}
for (const change of [
  h => { h.state.epoch += 1; h.state.edited = true; },
  h => { h.state.view = researchViewIdentity("/analysis/monthly", "a=old&result_ids=fixed"); },
  h => { h.state.navigation = "explicit-navigation-2"; },
  h => { h.state.mounted = false; },
]) {
  const h = harness();
  const request = h.submit();
  change(h);
  request.resolve("late-submission");
  await request.settled;
  assert.deepEqual(h.visible, ["old-readable"]);
}

// Coalesce focus/timer signals, release on failure, preserve old content, allow retry.
{
  const h = harness();
  const first = h.refresh();
  await h.refresh();
  await h.refresh();
  assert.equal(h.calls.length, 1);
  h.calls[0].reject(new Error("source unavailable"));
  await first;
  assert.deepEqual(h.visible, ["old-readable"]);
  assert.deepEqual(h.errors, ["source unavailable"]);
  assert.equal(h.availability.busy, false);
  const retry = h.refresh();
  assert.equal(h.calls.length, 2);
  h.calls[1].resolve("recovered");
  await retry;
  assert.deepEqual(h.visible, ["old-readable", "recovered"]);
}
// A failure from an abandoned view must not decorate another research with its error.
{
  const h = harness();
  const waiting = h.refresh();
  h.state.view = researchViewIdentity("/analysis/interval", "a=other");
  h.calls[0].reject(new Error("old request failed"));
  await waiting;
  assert.deepEqual(h.errors, []);
  assert.equal(h.availability.busy, false);
}
for (const change of [
  h => { h.availability.frozen = true; },
  h => { h.availability.visible = false; },
  h => { h.availability.analysisId = null; },
  h => { h.state.edited = true; },
  h => { h.state.analysisPending = true; },
  h => { h.state.mounted = false; },
]) {
  const h = harness();
  change(h);
  await h.refresh();
  assert.equal(h.calls.length, 0);
}
assert.equal(canPublishResearch("refresh", undefined, harness().state), false);
console.log("Research publication guards passed: deferred snapshot/edit/submit/navigation races, canonical URL identity, pending distinction, request coalescing, failure release and retry.");
