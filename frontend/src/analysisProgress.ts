import type { AnalysisOutput } from "./api";

type Item = AnalysisOutput["results"][number];

// Research conditions define a reading session. Data revisions do not change it.
export function analysisConditions(params: AnalysisOutput["params"]): string {
  return JSON.stringify(
    Object.keys(params)
      .filter((key) => !["request_id", "cutoff_date", "price_range"].includes(key))
      .sort()
      .map((key) => [key, params[key]]),
  );
}

export function resultSet(results: Item[]): string {
  return results
    .map((item) => item.result_id)
    .sort()
    .join(",");
}

export function matchesAnalysis(
  value: AnalysisOutput | null | undefined,
  id: string | null,
  frozen = "",
): boolean {
  return (
    !!value &&
    value.id === id &&
    (!frozen || resultSet(value.results) === frozen.split(",").sort().join(","))
  );
}

// Each item is a backend-calculated, immutable result. Only select versions;
// never combine prices, financial statistics, or different research conditions.
export function mergeAnalysisResults(
  previous: AnalysisOutput | undefined,
  incoming: AnalysisOutput,
  frozen = "",
): AnalysisOutput {
  if (
    !previous ||
    previous.id !== incoming.id ||
    analysisConditions(previous.params) !== analysisConditions(incoming.params)
  )
    return incoming;
  // Explicit historical snapshots are authoritative, even when older.
  if (frozen) return incoming;
  const bySymbol = new Map(previous.results.map((item) => [item.symbol, item]));
  for (const item of incoming.results) {
    const old = bySymbol.get(item.symbol);
    if (old && old.result_id !== item.result_id) {
      const oldData = Date.parse(old.data_published_at ?? old.created_at ?? "");
      const nextData = Date.parse(
        item.data_published_at ?? item.created_at ?? "",
      );
      if (
        oldData > nextData ||
        (oldData === nextData &&
          Date.parse(old.created_at ?? "") > Date.parse(item.created_at ?? ""))
      )
        continue;
    }
    bySymbol.set(
      item.symbol,
      old?.result_id === item.result_id
        ? (old.is_current === item.is_current && old.coverage_basis === item.coverage_basis &&
            JSON.stringify(old.coverage) === JSON.stringify(item.coverage) &&
            old.result_cutoff === item.result_cutoff && old.carried_from === item.carried_from
            ? old : {...item, data: old.data})
        : item,
    );
  }
  const tickers = incoming.params.tickers as string[];
  const results = tickers.flatMap((symbol) =>
    bySymbol.has(symbol) ? [bySymbol.get(symbol)!] : [],
  );
  const progress =
    Date.parse(previous.batch.updated_at) >
    Date.parse(incoming.batch.updated_at)
      ? previous
      : incoming;
  return { ...progress, results };
}

// Result publication can precede the next scope-status update. A current,
// complete published result is already done even if its old scope says RUNNING.
export function remainingAnalysisProgress(value: AnalysisOutput) {
  const completed = new Set(value.results
    .filter((item) => item.is_current === true && item.coverage?.complete === true)
    .map((item) => item.symbol));
  const remaining = (value.params.tickers as string[]).filter((symbol) => !completed.has(symbol));
  const scopes = value.batch.items.filter((item) => remaining.includes(item.symbol) &&
    !["SUCCEEDED", "READY", "CANCELLED", "PAUSED"].includes(item.status));
  const stageFor = (item: AnalysisOutput["batch"]["items"][number]) => {
    const stage = item.progress?.stage;
    return typeof stage === "string" && stage.trim() &&
      !["等待可执行任务", "等待依赖", "等待执行", "QUEUED", "WAITING"].includes(stage)
      ? stage : null;
  };
  const scope = scopes.find((item) => stageFor(item)) ?? scopes[0];
  const stage = scope && stageFor(scope);
  return {
    completedSymbols: (value.params.tickers as string[]).filter((symbol) => completed.has(symbol)),
    remainingSymbols: remaining,
    unknownSymbols: value.results.filter((item) => !item.coverage).map((item) => item.symbol),
    gapSymbols: value.results.filter((item) => item.coverage && !item.coverage.complete).map((item) => item.symbol),
    scope,
    stage: stage ? `${scope.symbol} · ${stage}` : remaining.length
      ? `${remaining.join("、")} · 正在确认其余结果`
      : "结果已就绪，正在确认任务完成",
  };
}
