import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const analysis = await load("lib/analysis.ts", true);
const events = await load("lib/events.ts", true);
const period = { month: 1, stats: { n: 3, up: 2, up_ratio: ".6667", median: ".04", paired_n: 2, beat: 1, median_excess: ".01", sector_paired_n: 2, sector_beat: 2, sector_median_excess: ".03" } };

test("conclusions mention the sector only when both references have paired data", () => {
  assert.match(analysis.conclusion("NVDA", period, "^GSPC"), /beat its sector in 2\/2/);
  assert.doesNotMatch(analysis.conclusion("NVDA", period, null), /its sector/);
  assert.doesNotMatch(analysis.conclusion("NVDA", { ...period, stats: { ...period.stats, paired_n: 0 } }, "^GSPC"), /its sector/);
  const result = { summary: { reaction: period.stats } };
  assert.match(events.context(result, 5, "^GSPC"), /beat its sector in 2\/2/);
  assert.doesNotMatch(events.context(result, 5, null), /its sector/);
});

test("distribution basis uses exact server boxes and their matching observations", () => {
  const market = { n: 2, median: ".0137", q25: ".0001234", q75: ".0887654" };
  const sector = { n: 1, median: "-.0234", q25: "-.0234", q75: "-.0234" };
  const stats = { ...period.stats, benchmark_excess_box: market, sector_excess_box: sector };
  assert.equal(analysis.distribution(stats, "absolute"), stats);
  assert.equal(analysis.distribution(stats, "market"), market);
  assert.equal(analysis.distribution(stats, "sector"), sector);
  assert.equal(analysis.distribution(period.stats, "sector").n, 0);
  const candle = { change: ".01234", excess: ".0034", sector_excess: "-.0066" };
  assert.equal(analysis.candleValue(candle, "absolute"), ".01234");
  assert.equal(analysis.candleValue(candle, "market"), ".0034");
  assert.equal(analysis.candleValue(candle, "sector"), "-.0066");
});

test("reopening no sector comparison preserves null and legacy results default to auto", () => {
  const result = { sector_comparison_included: true };
  assert.equal(events.selectedSectorBenchmark({ sector_benchmark: null, tickers: [{ result }] }), null);
  assert.equal(events.selectedSectorBenchmark({ sector_benchmark: "industry_group", tickers: [{ result }] }), "industry_group");
  assert.equal(events.selectedSectorBenchmark({ sector_benchmark: null, tickers: [] }), null);
  assert.equal(events.selectedSectorBenchmark({ tickers: [{ result: {} }] }), "auto");
});

test("market presets use Nasdaq 100 and still display older custom symbols", () => {
  assert.deepEqual(analysis.BENCHMARKS, ["^GSPC", "^NDX"]);
  assert.equal(analysis.benchmarkName("^NDX"), "Nasdaq 100");
  assert.equal(analysis.benchmarkName("^IXIC"), "^IXIC");
});


test("quarter ranks follow the server's selected pairing when its order reverses", () => {
  const ranks = { reaction_median_desc: 1, reaction_median_excess_desc: 2, reaction_sector_median_excess_desc: 3, reaction_sector_median_excess_asc: 1, reaction_up_ratio_desc: 2 };
  const median = { id: "median", desc: true };
  assert.equal(events.quarterRank(ranks, "reaction", "absolute", median), 1);
  assert.equal(events.quarterRank(ranks, "reaction", "market", median), 2);
  assert.equal(events.quarterRank(ranks, "reaction", "sector", median), 3);
  assert.equal(events.quarterRank(ranks, "reaction", "sector", { ...median, desc: false }), 1);
  assert.equal(events.quarterRank(ranks, "reaction", "absolute", { id: "sector_excess", desc: true }), 3);
  assert.equal(events.quarterRank(ranks, "reaction", "sector", { id: "up", desc: true }), 2);
  assert.equal(events.quarterRank(ranks, "reaction", "sector", { id: "mean", desc: true }), undefined);
  assert.equal(events.quarterRank({}, "reaction", "sector", median), undefined);
});
