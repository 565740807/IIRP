import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/chartExportContext.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { chartExportContext } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const frozen = { metadata: { symbol: "AAPL", price_basis: "split_only", cutoff_date: "2026-09-21", historical_years: [2024], params: { kind: "monthly", month: 1 } }, summary: { n: 1, path: [{ x: 0, n: 1 }, { x: 1, n: 0 }], months: [{ month: 1, n: 1 }, { month: 2, n: 0 }] } };
const lines = chartExportContext(frozen, "结果 frozen-v1", ["实际 D0 2024-05-02"]);
assert.match(lines.join("\n"), /AAPL/);
assert.match(lines.join("\n"), /2024-05-02/);
assert.match(lines.join("\n"), /仅单一样本/);
assert.match(lines.join("\n"), /路径各位置有效 N=0—1/);
assert.match(lines.join("\n"), /1:1  2:0/);
assert.match(lines.join("\n"), /frozen-v1/);
assert.match(chartExportContext({ ...frozen, summary: { n: 0 } }, "").join("\n"), /没有有效历史样本/);
const interval = chartExportContext({ metadata:{symbol:"AAPL",price_basis:"split_only",cutoff_date:"2026-09-23",params:{kind:"interval",start_mmdd:"05-01",end_mmdd:"05-10",month:9,years:[2018,2019]}},summary:{n:2}}, "结果 interval-v1");
assert.match(interval.join("\n"),/研究窗口 05-01 → 05-10/);
assert.doesNotMatch(interval.join("\n"),/研究窗口.*9月/);
assert.match(interval.join("\n"),/历史年份 2018、2019/);
console.log("chart export frozen context passed");

// A selected year's three comparison lines are one observation, not the
// population behind the whole-history summary and pointwise sample counts.
const selectedYear = chartExportContext({metadata:{params:{kind:"interval",years:[2018,2019]}},summary:{n:8,path:[{n:6},{n:8}]}}, "结果 selected-v1", [], "2026").join("\n");
assert.match(selectedYear, /当前图仅展示：2026/);
assert.match(selectedYear, /三条曲线不代表三个独立样本/);
assert.match(selectedYear, /全研究完整历史有效 N=8/);
assert.match(selectedYear, /全研究历史路径各位置有效 N=6—8/);
assert.match(selectedYear, /不代表当前单一样本曲线的 N/);
assert.doesNotMatch(lines.join("\n"), /当前图仅展示/);

const pairing = {stock:{n:8},paired_stock:{n:6}};
const pairedInterval = chartExportContext({kind:"interval",metadata:{params:{kind:"interval",month:9}},distributions:[{group:"interval",metric:"endpoint",...pairing}]}, "").join("\n");
assert.match(pairedInterval, /所选组股票 N=8 · 与基准同日配对 N=6/);
