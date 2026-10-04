import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../src/earningsPresentation.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText;
const { preciseEarningsView, selectedDateObservationN } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const dateOnly = {
  event_id: "dated", group: "historical", quarter: 2, precise: false,
  eligible: false, endpoint: "0.079385718837212",
};
const current = {
  event_id: "current", group: "current", quarter: 2, precise: false,
  eligible: false, endpoint: "-0.076470588235294",
};
const precise = {
  event_id: "timed", group: "historical", quarter: 2, precise: true,
  eligible: true, endpoint: "0.0667",
};
const data = {
  kind: "earnings", effective_n: 1,
  rows: [dateOnly, current, precise],
  cells: [dateOnly, precise],
  series: [{ key: "dated", points: [{ value: "0.079385718837212" }] },
    { key: "timed", points: [{ value: "0.0667" }] }],
  summary: { n: 1, median: "0.0667", current },
  distributions: [{ key: "Q2:5", samples: [
    { key: "dated", stock: "0.079385718837212", eligible: false },
    { key: "timed", stock: "0.0667", eligible: true },
  ] }],
  date_observation: { effective_n: 4, distributions: [
    { group: "Q1", metric: "after5", stock: { n: 1 } },
    { group: "Q2", metric: "after5", stock: { n: 1 } },
    { group: "Q2", metric: "day0", stock: { n: 0 } },
    { group: "Q3", metric: "after5", stock: { n: 2 } },
  ] },
};
const shown = preciseEarningsView(data);
assert.deepEqual(shown.rows, [precise]);
assert.deepEqual(shown.cells, [precise]);
assert.deepEqual(shown.series.map(item => item.key), ["timed"]);
assert.deepEqual(shown.distributions[0].samples.map(item => item.key), ["timed"],
  "expanded precise statistics must not leak date-only numeric rows");
assert.equal(shown.summary.current, null);
assert.equal(shown.summary.n, 1);
assert.equal(data.rows.length, 3, "saved result remains immutable");
assert.equal(data.summary.current, current);
assert.equal(selectedDateObservationN(data, 2, "after5"), 1,
  "selected fiscal quarter N is not all-quarter effective_n");
assert.equal(selectedDateObservationN(data, 3, "after5"), 2);
assert.equal(selectedDateObservationN(data, 2, "day0"), 0);
assert.equal(selectedDateObservationN(data, 4, "after5"), null);
const noPrecise = preciseEarningsView({ ...data, effective_n: 0, rows: [dateOnly, current] });
assert.deepEqual(noPrecise.rows, []);
assert.deepEqual(noPrecise.distributions[0].samples, []);
assert.deepEqual(noPrecise.series.map(item => item.key), [],
  "old date-only path must not appear under precise reaction");
assert.equal(noPrecise.summary.current, null);

const page = readFileSync(new URL("../src/pages/Analysis.tsx", import.meta.url), "utf8");
assert.match(page, /<EventDatesResult[\s\S]*?data=\{facts\(data\.date_observation\)\}/);
assert.match(page, /<ResearchChart\s+data=\{preciseData\}/);
assert.match(page, /<ResearchTable[\s\S]*?data=\{rows\(preciseData\.rows\)\}/);
assert.match(page, /所选财季日期观察历史 N=\{display\(dateObservationN\)\}/);
console.log("earnings presentation boundaries passed");
