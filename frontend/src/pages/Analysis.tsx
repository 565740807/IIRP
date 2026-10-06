import { ResearchReadStatus, ResearchVersionActions } from "../components/ResearchReading";
import { useNativeResearch, useRecentResearch, useResearchBatch, nativeResearchOptions } from "../researchQueries";
import { researchKeys } from "../queryIdentity";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import {
  api,
  activeStatuses,
  display,
  facts,
  percent,
  requestId,
  rows,
  sourceLabel,
  usePreferences,
  type AnalysisInput,
  type AnalysisOutput,
  type AnalysisRefreshOutput,
  type Fact,
  type PreferenceInput,
} from "../api";
import { BatchActions, BatchScopes } from "../components/Batches";
import { PriceRangePreview } from "../components/PriceRange";
import {
  Button,
  EmptyState,
  ErrorNotice,
  useNotice,
} from "../components/ui";
import { ResearchChart } from "../components/ResearchChart";
import { ResearchTable } from "../components/ResearchTable";
import { comparableResults } from "../comparableResults";
import {
  BenchmarkPicker,
  StatisticsPanel,
} from "../components/StatisticsPanel";
import {
  rememberResearch,
  researchHref,
  serializeResearch,
  yearList,
  useReadingState,
  useContextSearchParams,
} from "../researchStorage";
import {
  analysisConditions,
  matchesAnalysis,
  mergeAnalysisResults,
  remainingAnalysisProgress,
} from "../analysisProgress";
import { researchViewIdentity, captureResearchPublication, canPublishResearch, canStartResearchRefresh } from "../researchPublication";
import { StatisticsPanel as ResearchEvidence } from "../components/ResearchEvidence";
import "../analysis.css";
const tabs = [
  { id: "monthly", label: "月度分析" },
  { id: "interval", label: "区间分析" },
  { id: "earnings", label: "财报行情" },
  { id: "events", label: "自定义事件分析" },
];
function marketCalendar() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "numeric",
  }).formatToParts(new Date());
  return {
    year: Number(parts.find((part) => part.type === "year")!.value),
    month: Number(parts.find((part) => part.type === "month")!.value),
  };
}
// Conditions describe the requested sample; they never imply the effective N.
function researchConditionSummary(value: Record<string, unknown>): string {
  const names = (items: unknown) => Array.isArray(items)
    ? [...new Set(items.map(String))].join("、") : "";
  const years = names(value.years);
  const excluded = names(value.excluded_years);
  const parts = [names(value.tickers) || "待选择证券",
    years ? `指定历史年份 ${years}` : `${value.historical_years ?? 8} 个完整历史年`];
  if (excluded) parts.push(`排除年份 ${excluded}`);
  parts.push(value.current_year != null
    ? `当前对照年 ${value.current_year}（另列）` : "当前对照年自动确定（另列）");
  parts.push(value.kind === "interval"
    ? `区间 ${value.start_mmdd ?? "01-01"}→${value.end_mmdd ?? "12-31"}${value.cross_year === true ? "（跨年）" : ""}`
    : `${value.month ?? 1} 月`);
  parts.push(value.comparison === "complete" ? "完整历史范围" : "截至同期",
    value.alignment === "trading" ? "按交易日序号对齐" : "按日历位置对齐");
  parts.push(value.benchmark ? `基准 ${value.benchmark}` : "仅查看股票");
  return parts.join(" · ");
}
function initial(
  p: URLSearchParams,
  kind: AnalysisInput["kind"],
): AnalysisInput {
  const calendar = marketCalendar();
  return {
    request_id: requestId(),
    research_label: p.get("research_label") || undefined,
    kind,
    tickers: (p.get("tickers") ?? p.get("ticker") ?? "AAPL")
      .split(/[\s,，]+/)
      .filter(Boolean),
    historical_years: Number(p.get("historical_years")) || 8,
    years: yearList(p.get("years")).length
      ? yearList(p.get("years"))
      : undefined,
    excluded_years: yearList(p.get("excluded_years")),
    current_year:
      Number(p.get("current_year")) ||
      (kind === "monthly" ? calendar.year : undefined),
    month: Number(p.get("month")) || calendar.month,
    comparison:
      p.get("comparison") === "complete" ? "complete" : "same_progress",
    alignment: p.get("alignment") === "trading" ? "trading" : "calendar",
    start_mmdd: p.get("start_mmdd") ?? "01-01",
    end_mmdd: p.get("end_mmdd") ?? "12-31",
    cross_year: p.has("cross_year")
      ? p.get("cross_year") === "true"
      : undefined,
    benchmark: p.get("benchmark") || undefined,
  };
}
export function AnalysisPage() {
  const location = useLocation();
  const navigationRef = useRef(location.key);
  const mutationNavigation = useRef<string | null>(null);
  const mutationEpoch = useRef<number | null>(null);
  const refreshEpoch = useRef(0);
  const renderedLocation = useRef(location.key);
  const previousLocation = useRef(location.key);
  const automaticLocation = useRef<{ from: string; to: string } | null>(null);
  if (renderedLocation.current !== location.key) {
    const automatic = automaticLocation.current?.from === renderedLocation.current &&
      automaticLocation.current.to === location.pathname + location.search;
    if (!automatic) {
      navigationRef.current = location.key;
      refreshEpoch.current += 1;
    }
    renderedLocation.current = location.key;
  }
  const editedRef = useRef(false);
  const [edited, setEdited] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const kind = (location.pathname.split("/").pop() ??
    "monthly") as AnalysisInput["kind"];
  const [params, setParams] = useContextSearchParams();
  // Canonical result URLs are background reads, not a request to discard edits.
  // The marker applies to this one replacement only; explicit navigation (even
  // to the same saved research) still restores that destination's conditions.
  const replaceResearch = useCallback(
    (next: URLSearchParams) => {
      const to = `${location.pathname}?${next}`;
      if (to === location.pathname + location.search) return;
      automaticLocation.current = { from: location.key, to };
      setParams(next, { replace: true });
    },
    [location.key, location.pathname, location.search, setParams],
  );
  const [draft, setDraft] = useState(() => initial(params, kind));
  const [customBenchmark, setCustomBenchmark] = useState(false);
  const [conditionsOpen, setConditionsOpen] = useState(!params.has("a"));
  const collapseConditionsAfterSubmission = useRef<number | null>(null);
  const [tickerText, setTickerText] = useState(draft.tickers.join(", "));
  const [yearText, setYearText] = useState(draft.years?.join(", ") ?? "");
  const [excludeText, setExcludeText] = useState(
    draft.excluded_years?.join(", ") ?? "",
  );
  const [error, setError] = useState<Error | null>(null);
  const [refreshFailure, setRefreshFailure] = useState<Error | null>(null);
  const [selected, setSelected] = useReadingState(
    `analysis:${params.get("study") ?? params.get("a") ?? kind}:selected`,
    "",
  );
  const [compare, setCompare] = useReadingState(
    `analysis:${params.get("study") ?? params.get("a") ?? kind}:compare`,
    "single",
  );
  const [chartTickers, setChartTickers] = useReadingState<string[] | null>(
    `analysis:${params.get("study") ?? params.get("a") ?? kind}:tickers`,
    null,
  );
  const [previous, setPrevious] = useState<AnalysisOutput | null>(null);
  const preferences = usePreferences();
  const notice = useNotice();
  const client = useQueryClient();
  const recent = useRecentResearch(kind, params.get("ticker") ?? params.get("tickers") ?? "");
  const advanced = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (params.has("a")) {
      rememberResearch(kind, params.toString());
      return;
    }
    if ([...params.keys()].some((key) => !["ticker", "tickers"].includes(key)))
      return;
    if (!params.toString()) {
      const href = researchHref(kind);
      if (href.includes("?")) {
        replaceResearch(new URLSearchParams(href.split("?")[1]));
        return;
      }
    }
    // A ticker search is an explicit new target. Recent research is an optional
    // link, and may contain additional tickers or different conditions.
    if (params.has("ticker") || params.has("tickers")) return;
    const last = rows(recent.data?.items)[0];
    if (last) replaceResearch(new URLSearchParams({ a: last.id }));
  }, [params, kind, recent.data, replaceResearch]);
  useEffect(() => {
    const automatic =
      automaticLocation.current?.from === previousLocation.current &&
      automaticLocation.current.to === location.pathname + location.search;
    automaticLocation.current = null;
    previousLocation.current = location.key;
    if (automatic && editedRef.current) return;
    if (!automatic) {
      analysisPending.current = false;
      submissionSerial.current += 1;
      navigationRef.current = location.key;
      editedRef.current = false;
      setEdited(false);
      // Only an in-place pending submission may keep the previous result.
      // Explicitly selecting another research must wait for that destination.
      setPrevious(null);
      setConditionsOpen(!new URLSearchParams(location.search).has("a"));
    }
    const restored = initial(new URLSearchParams(location.search), kind);
    setDraft(restored);
    setCustomBenchmark(false);
    setTickerText(restored.tickers.join(", "));
    setYearText(restored.years?.join(", ") ?? "");
    setExcludeText(restored.excluded_years?.join(", ") ?? "");
    setError(null);
    setRefreshFailure(null);
    if (!new URLSearchParams(location.search).has("a")) setPrevious(null);
  }, [location.key, location.pathname, location.search, kind]);
  useEffect(() => {
    if (!params.has("historical_years") && preferences.data)
      setDraft((d) =>
        editedRef.current
          ? d
          : {
              ...d,
              historical_years: preferences.data.values.historical_years,
              comparison: preferences.data.values.comparison,
            },
      );
  }, [preferences.data, params]);
  const id = params.get("a");
  const frozenResults = params.get("result_ids") ?? "";
  const content = useNativeResearch(id, frozenResults);
  const task = useResearchBatch(frozenResults ? content.data?.batch_id ?? "" : "");
  const resultData = useMemo(() => {
    const value = content.data, batch = task.data?.batch;
    return value && batch?.id === value.batch_id
      ? { ...value, status: batch.status, batch } : value;
  }, [content.data, task.data]);
  const result = { ...content, data: resultData };
  useEffect(() => {
    if (!id || result.data?.id !== id || result.data.params.kind !== kind)
      return;
    const canonical = serializeResearch(result.data.params);
    const savedYear = facts(result.data.results[0]?.data.metadata).current_year;
    const yearKey = "current_year";
    if (!canonical.has(yearKey) && savedYear != null)
      canonical.set(yearKey, String(savedYear));
    canonical.set("a", result.data.id);
    if (frozenResults) canonical.set("result_ids", frozenResults);
    canonical.set("study", params.get("study") ?? result.data.freshness?.origin_id ?? id);
    if (canonical.toString() !== params.toString()) {
      // Freezing the exact displayed set needs no duplicate download and must
      // not unmount the chart while an equivalent query key is being created.
      const snapshot = canonical.get("result_ids");
      if (snapshot && snapshot !== frozenResults)
        client.setQueryData(researchKeys.native(id, snapshot), result.data);
      replaceResearch(canonical);
    }
  }, [result.data, id, kind, params, replaceResearch, frozenResults, client]);
  // Reading a newly frozen URL can temporarily have no query data. A pending
  // submission's fallback must never impersonate a different research/version.
  const previousMatches = matchesAnalysis(previous, id, frozenResults);
  const shown = result.data ?? (previousMatches ? previous : null);
  const activeId = useRef(id);
  activeId.current = id;
  const analysisPending = useRef(false);
  const submissionSerial = useRef(0);
  const refreshBusy = useRef(false);
  const currentView = useRef(researchViewIdentity(location.pathname, location.search));
  currentView.current = researchViewIdentity(location.pathname, location.search);
  const publicationState = () => ({epoch: refreshEpoch.current, view: currentView.current,
    navigation: navigationRef.current, mounted: mounted.current, edited: editedRef.current,
    analysisPending: analysisPending.current});
  const refresh = useMutation({
    onMutate: () => { setRefreshFailure(null); return captureResearchPublication(publicationState()); },
    mutationFn: async (command: {id: string; force?: boolean}) => {
      const receipt = await api<AnalysisRefreshOutput>(`/analyses/${encodeURIComponent(command.id)}/refresh?force=${command.force === true}`, "POST");
      await client.cancelQueries({ queryKey: researchKeys.native(receipt.id), exact: true });
      return client.fetchQuery({ ...nativeResearchOptions(receipt.id), staleTime: 0 });
    },
    onSuccess: (updated, command, context) => {
      const origin = command.id;
      client.setQueryData(researchKeys.native(updated.id), (old: AnalysisOutput | undefined) => mergeAnalysisResults(old, updated));
      if (activeId.current !== origin || !canPublishResearch("refresh", context, publicationState())) return;
      if (updated.id !== origin) {
        const next = serializeResearch(updated.params);
        next.set("a", updated.id);
        next.set("study", params.get("study") ?? updated.freshness?.origin_id ?? origin);
        replaceResearch(next);
      }
      void client.invalidateQueries({ queryKey: ["recent-analyses"] });
    },
    onError: (failure, _command, context) => {
      if (canPublishResearch("refresh", context, publicationState())) setRefreshFailure(failure);
    },
    onSettled: () => { refreshBusy.current = false; },
  });
  function launchRefresh(command: {id: string; force?: boolean}) {
    if (!canStartResearchRefresh(publicationState(), {analysisId: command.id, frozen: !!frozenResults, visible: !document.hidden, busy: refreshBusy.current})) return;
    refreshBusy.current = true;
    refresh.mutate(command);
  }
  const refreshCommand = useRef(launchRefresh);
  refreshCommand.current = launchRefresh;
  useEffect(() => {
    if (!id || frozenResults || result.data?.id !== id) return;
    const check = () => {
      if (!document.hidden && !editedRef.current && !analysisPending.current) refreshCommand.current({id});
    };
    check();
    const timer = window.setInterval(check, 60000);
    window.addEventListener("focus", check);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", check); };
  }, [id, frozenResults, result.data?.id]);
  const mutation = useMutation({
    onMutate: () => {
      analysisPending.current = true;
      mutationNavigation.current = navigationRef.current;
      mutationEpoch.current = refreshEpoch.current;
      return {...captureResearchPublication(publicationState()), submission: submissionSerial.current};
    },
    mutationFn: (input: AnalysisInput) =>
      api<AnalysisOutput>("/analyses", "POST", input),
    onSuccess: (r, input, context) => {
      client.setQueryData(researchKeys.native(r.id), r);
      void client.invalidateQueries({ queryKey: ["recent-analyses"] });
      if (!canPublishResearch("submit", context, publicationState())) return;
      // Only an applied form's own publishable response may collapse it.
      // Background refreshes and abandoned submissions preserve reading intent.
      if (context?.submission === collapseConditionsAfterSubmission.current)
        setConditionsOpen(false);
      const p = serializeResearch(input);
      p.set("a", r.id);
      replaceResearch(p);
      void client.invalidateQueries({ queryKey: ["batches"] });
      void client.invalidateQueries({ queryKey: ["recent-analyses"] });
      setError(null);
    },
    onError: (error, _input, context) => {
      if (canPublishResearch("submit", context, publicationState()))
        setError(error);
    },
    onSettled: (_data, _error, _input, context) => {
      if (context?.submission === submissionSerial.current) analysisPending.current = false;
    },
  });
  // A pending command belongs to the navigation where it was submitted.
  // Explicitly choosing a different research leaves that command in history.
  const isApplying =
    mutation.isPending && mutationNavigation.current === navigationRef.current &&
    mutationEpoch.current === refreshEpoch.current;
  const save = useMutation({
    mutationFn: (v: PreferenceInput) => api("/preferences", "PATCH", v),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["preferences"] });
      notice({ text: "默认研究范围已保存。" });
    },
    onError: setError,
  });
  function input(override: Partial<AnalysisInput> = {}): AnalysisInput | null {
    if (customBenchmark && !draft.benchmark) {
      setError(new Error("请输入行业ETF代码，或选择仅查看股票。"));
      return null;
    }
    const tickers = [
      ...new Set(
        tickerText
          .toUpperCase()
          .split(/[\s,，]+/)
          .filter(Boolean),
      ),
    ];
    if (
      !tickers.length ||
      tickers.length > 20 ||
      tickers.some((t) => !/^[A-Z0-9^][A-Z0-9.^=\-]{0,19}$/.test(t))
    ) {
      setError(new Error("请输入 1–20 个有效证券代码，以逗号分隔。"));
      return null;
    }
    if (
      !Number.isInteger(draft.historical_years) ||
      draft.historical_years! < 1
    ) {
      setError(new Error("历史年数需为正整数。"));
      return null;
    }
    const years = yearText.trim() ? yearList(yearText) : undefined;
    const excluded = excludeText.trim() ? yearList(excludeText) : [];
    if (
      [...(years ?? []), ...excluded].some(
        (y) => !Number.isInteger(y) || y < 1 || y > 9998,
      )
    ) {
      setError(new Error("年份需为 1–9998 的整数。"));
      if (advanced.current) advanced.current.open = true;
      return null;
    }
    return {
      ...draft,
      kind,
      tickers,
      years,
      excluded_years: excluded,
      request_id: requestId(),
      ...override,
    };
  }
  function apply(override: Partial<AnalysisInput> = {}) {
    const v = input(override);
    if (v) {
      refreshEpoch.current += 1;
      submissionSerial.current += 1;
      collapseConditionsAfterSubmission.current = submissionSerial.current;
      analysisPending.current = true;
      editedRef.current = false;
      setEdited(false);
      if (shown) setPrevious(shown);
      setDraft(v);
      mutation.mutate(v);
    }
  }
  function selectResult(override: Partial<AnalysisInput>) {
    if (!shown || isApplying) return;
    refreshEpoch.current += 1;
    submissionSerial.current += 1;
    analysisPending.current = true;
    editedRef.current = false;
    setEdited(false);
    setPrevious(shown);
    mutation.mutate({
      ...initial(serializeResearch(shown.params), kind),
      ...override,
      request_id: requestId(),
    } as AnalysisInput);
  }
  const selectedItem =
    shown?.results.find((r) => r.symbol === selected) ?? shown?.results[0];
  useEffect(() => {
    if (!selected && shown?.results[0]) setSelected(shown.results[0].symbol);
  }, [selected, shown?.results, setSelected]);
  const data = facts(selectedItem?.data);
  const meta = facts(data.metadata);
  const preciseData = data;
  const summary = facts(preciseData.summary);
  const current = facts(summary.current);
  const displayedTickers = useMemo(
    () => chartTickers ?? shown?.results.slice(0, 5).map((r) => r.symbol) ?? [],
    [chartTickers, shown?.results],
  );
  const comparisonItems = useMemo(
    () => comparableResults(shown?.results ?? [], selectedItem, displayedTickers),
    [shown?.results, selectedItem, displayedTickers],
  );
  const comparisonData: Fact = useMemo(
    () => ({
      kind,
      metadata: meta,
      series:
        comparisonItems
          .flatMap((r) => {
            const d = facts(r.data);
            if (compare === "current")
              return rows(d.series)
                .filter((s) => s.group === "current")
                .slice(0, 1)
                .map((s) => ({ ...s, label: `${r.symbol} · ${s.label}` }));
            return [
              {
                key: r.symbol,
                label: `${r.symbol} · 历史中位路径`,
                group: "current",
                points: rows(facts(d.summary).path).map((p) => ({
                  ...p,
                  value: p.median,
                })),
              },
            ];
          }) ?? [],
    }),
    [kind, meta, comparisonItems, compare],
  );
  const readingKey = `analysis:${params.get("study") ?? shown?.freshness?.origin_id ?? shown?.id}:${selectedItem?.symbol}`;
  const conditionKey = shown ? analysisConditions(shown.params) : kind;
  const conditionSummary = researchConditionSummary(
    edited || !shown
      ? {...draft, kind, tickers: tickerText.toUpperCase().split(/[\s,，]+/).filter(Boolean),
          years: yearList(yearText), excluded_years: yearList(excludeText)}
      : isApplying && mutation.variables ? mutation.variables : shown.params,
  );
  const progress = shown ? remainingAnalysisProgress(shown) : null;
  const completedSymbols = progress?.completedSymbols ?? [];
  const stage = progress?.stage ?? "正在准备所选范围";
  const resultIds = shown?.results.map((r) => r.result_id).join(",") ?? "";
  const snapshotUrl = new URL(window.location.href);
  if (shown) {
    snapshotUrl.search = serializeResearch(shown.params).toString();
    snapshotUrl.searchParams.set("a", shown.id);
  }
  const provenance = `${selectedItem?.symbol ?? ""} · ${sourceLabel(meta.price_basis)} · 截至 ${display(meta.cutoff_date)} · N=${display(data.effective_n)}\n${kind === "monthly" ? `${shown?.params.month}月` : `${shown?.params.start_mmdd}–${shown?.params.end_mmdd}`} · ${sourceLabel(meta.comparison)} · 来源 ${display(meta.source ?? meta.provider)} · 数据 ${selectedItem?.input_version?.slice(0, 12) ?? "—"} · 计算 ${display(meta.calculation_version)}\n结果 ${selectedItem?.result_id ?? "—"}`;
  const annualProvenance = `${selectedItem?.symbol ?? ""} · ${sourceLabel(meta.price_basis)} · 截至 ${display(meta.cutoff_date)}\n1—12月 · 完整历史月；导出图附各月有效N，当前年度另列 · 来源 ${display(meta.source ?? meta.provider)} · 计算 ${display(meta.calculation_version)}\n数据 ${selectedItem?.input_version?.slice(0, 12) ?? "—"} · 结果 ${selectedItem?.result_id ?? "—"}`;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>分析中心</h1>
          <p>比较同一时间范围的真实价格路径，缺失和排除原因随结果呈现。</p>
        </div>
        <Button
          onClick={() => {
            refreshEpoch.current += 1;
            setPrevious(null);
            setParams({ new: requestId() });
          }}
        >
          新建分析
        </Button>
      </div>
      {!!rows(recent.data?.items).length && (
        <details className="result-notes">
          <summary>
            最近研究 · {tabs.find((tab) => tab.id === kind)?.label}
          </summary>
          {rows(recent.data?.items).map((item) => (
            <p key={item.id}>
              <Link
                className="text-link"
                to={`/analysis/${kind}?a=${encodeURIComponent(item.id)}`}
              >
                {researchConditionSummary({...facts(item.params), kind})} ·{" "}
                {sourceLabel(item.status)}
              </Link>
            </p>
          ))}
        </details>
      )}
      <div className="analysis-tabs">
        {tabs.map((t) => (
          <Link
            key={t.id}
            to={
              t.id === "earnings" || t.id === "events"
                ? `/analysis/${t.id}`
                : params.has("ticker") || params.has("tickers")
                ? `/analysis/${t.id}?${new URLSearchParams([...params].filter(([key]) => !["a", "result_ids", "kind", ...(t.id === kind ? [] : ["current_year"])].includes(key)))}`
                : researchHref(
                    t.id,
                    new URLSearchParams(
                      [...params].filter(
                        ([k]) =>
                          k !== "a" &&
                          (t.id === kind ||
                            k !== "current_year"),
                      ),
                    ).toString(),
                  )
            }
            className={kind === t.id ? "selected" : ""}
          >
            {t.label}
          </Link>
        ))}
      </div>
      <details
        className="panel analysis-conditions"
        open={conditionsOpen}
        onToggle={(event) => setConditionsOpen(event.currentTarget.open)}
      >
        <summary>
          <strong>{edited ? "未提交条件" : isApplying ? "提交中的条件" : "研究条件"}</strong>
          <span>{conditionSummary}</span>
          <span className="text-link">
            {conditionsOpen ? "收起条件" : "修改条件"}
          </span>
        </summary>
        <form
          className="analysis-form"
          onChangeCapture={() => {
            refreshEpoch.current += 1;
            editedRef.current = true;
            setEdited(true);
          }}
          onSubmit={(e) => {
            e.preventDefault();
            apply();
          }}
        >
          <label>
            研究标签（可选）
            <input value={draft.research_label ?? ""} onChange={(e) => setDraft({ ...draft, research_label: e.target.value })} placeholder="例如：验收20260922 · 月度" />
          </label>
          <label className="ticker-field">
            证券代码（最多 20 个）
            <input
              value={tickerText}
              onChange={(e) => setTickerText(e.target.value)}
              placeholder="AAPL, MSFT"
            />
          </label>
          <label>
            历史年数
            <input
              type="number"
              min="1"
              required
              value={draft.historical_years}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  historical_years: e.target.valueAsNumber,
                }))
              }
            />
          </label>
          <label
            className={kind === "interval" ? "interval-year-field" : undefined}
          >
            当前对照年
            <input
              type="number"
              min="1"
              max="9998"
              placeholder={
                kind === "interval"
                  ? "按区间与日期自动确定"
                  : String(marketCalendar().year)
              }
              value={draft.current_year ?? ""}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  current_year: e.target.value
                    ? e.target.valueAsNumber
                    : undefined,
                }))
              }
            />
          </label>
          {kind === "interval" && (
            <>
              <label>
                每年开始（月-日）
                <input
                  pattern="\d{2}-\d{2}"
                  value={draft.start_mmdd}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, start_mmdd: e.target.value }))
                  }
                />
              </label>
              <label>
                每年结束（月-日）
                <input
                  pattern="\d{2}-\d{2}"
                  value={draft.end_mmdd}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, end_mmdd: e.target.value }))
                  }
                />
              </label>
            </>
          )}
                      <label>
              {kind === "monthly" ? "单月走势比较口径" : "比较口径"}
              <select
                value={draft.comparison}
                onChange={(e) =>
                  setDraft((d) => ({
                    ...d,
                    comparison: e.target.value as AnalysisInput["comparison"],
                  }))
                }
              >
                <option value="same_progress">截至同期</option>
                <option value="complete">完整历史范围</option>
              </select>
            </label>
          <div className="button-row">
            <Button type="submit" variant="primary" busy={isApplying}>
              查看分析
            </Button>
          </div>
          <p className="context-baseline">
            查看分析会复用已有行情，按当前条件与所选基准补齐缺口。已有结果保留原版本。
          </p>
                      <PriceRangePreview
              request={{
                historical_years: draft.historical_years,
                analysis: {
                  ...draft,
                  kind,
                  request_id: "range-preview",
                  tickers: tickerText
                    .toUpperCase()
                    .split(/[\s,，]+/)
                    .filter(Boolean),
                  years: yearText.trim() ? yearList(yearText) : undefined,
                  excluded_years: excludeText.trim()
                    ? yearList(excludeText)
                    : [],
                },
              }}
            />
          <div className="analysis-form">
            <BenchmarkPicker
              value={draft.benchmark ?? ""}
              custom={customBenchmark}
              onChange={(value, custom) => {
                setCustomBenchmark(custom);
                setDraft((d) => ({ ...d, benchmark: value || undefined }));
                setError(null);
              }}
            />
          </div>
          <details className="advanced-options" ref={advanced}>
            <summary>指定历史年份与显示条件</summary>
            <div className="analysis-form nested-form">
              <label>
                指定历史年份（留空使用年数）
                <input
                  value={yearText}
                  onChange={(e) => setYearText(e.target.value)}
                  placeholder="2018, 2019, 2020"
                />
              </label>
              <label>
                排除年份
                <input
                  value={excludeText}
                  onChange={(e) => setExcludeText(e.target.value)}
                  placeholder="2020, 2022"
                />
              </label>
                              <label>
                  横轴对齐
                  <select
                    value={draft.alignment}
                    onChange={(e) =>
                      setDraft((d) => ({
                        ...d,
                        alignment: e.target.value as AnalysisInput["alignment"],
                      }))
                    }
                  >
                    <option value="calendar">日历位置</option>
                    <option value="trading">交易日序号</option>
                  </select>
                </label>
              <Button
                type="button"
                busy={save.isPending}
                onClick={() => {
                  const v = input();
                  if (v)
                    save.mutate({
                      historical_years: v.historical_years ?? 8,
                      automatic_history:
                        preferences.data?.values.automatic_history ?? true,
                      history_months:
                        preferences.data?.values.history_months ?? 3,
                      comparison: v.comparison ?? "same_progress",
                    });
                }}
              >
                保存为默认
              </Button>
            </div>
          </details>
        </form>
        <ErrorNotice error={error} />
      </details>
      {edited && (
        <p className="notice notice-info" role="status">
          研究条件已修改，尚未应用。
          {shown
            ? `下方仍显示 ${display(shown.params.tickers)} 的已保存结果；`
            : ""}
          点击“查看分析”后按新条件更新结果。
        </p>
      )}
      <ResearchReadStatus error={result.error} retry={result.refetch}
        taskError={task.error} retryTask={task.refetch}
        pending={!!id && result.isPending && !shown} label="读取本地分析结果…" />
      {isApplying && (
        <div className="notice notice-info">
          正在保存研究并核对可用结果。已保存内容继续可读；每只股票准备好后，图、表与导出按同一版本更新。
        </div>
      )}
      {shown && (
        <section className="research-freshness" aria-label="研究版本与新鲜度">
          <div className="section-heading">
            <strong>{shown.batch.title}</strong>
            <ResearchVersionActions busy={refresh.isPending}
              refreshLabel={refresh.isPending ? "正在重新获取…" : "重新获取"}
              refresh={() => {
                if (frozenResults) {
                  client.setQueryData(researchKeys.native(id), shown);
                  const next = new URLSearchParams(params); next.delete("result_ids"); replaceResearch(next);
                } else if (id) launchRefresh({id, force:true});
              }} />
          </div>
          <p>显示结果截至 <strong>{selectedItem?.result_cutoff ?? display(meta.cutoff_date)}</strong> · 最近已完成交易日 <strong>{shown.freshness?.latest_completed_session ?? "待核对"}</strong> · 行情获取于 <strong>{shown.freshness?.price_fetched_at ? new Date(shown.freshness.price_fetched_at).toLocaleString() : "—"}</strong>，有效至 {shown.freshness?.price_expires_at ? new Date(shown.freshness.price_expires_at).toLocaleString() : "—"}</p>
          <details className="research-version-detail">
            <summary>关于行情缓存</summary>
            <small>行情为 24 小时缓存：获取后 24 小时内重复打开不再下载，过期后打开会自动重新获取；“重新获取”立即按最近已完成交易日取数。结果 {selectedItem?.result_id?.slice(0, 8) ?? "准备中"}。<Link to="/data">查看任务历史</Link></small>
          </details>
          <ErrorNotice error={refreshFailure} retry={() => id && launchRefresh({id, force:true})} />
        </section>
      )}
      {shown && (!shown.results.length || shown.status !== "SUCCEEDED") && (
        <div
          className="analysis-progress notice notice-info"
          role="status"
          aria-live="polite"
        >
          <strong>
            {shown.results.length
              ? `已有 ${shown.results.length}/${(shown.params.tickers as string[]).length} 只股票的结果可读`
              : stage}
          </strong>
          {shown.results.length > 0 && (
            <span>
              {" "}
              · 当前数据且覆盖完整 {completedSymbols.length} 只
              {progress?.unknownSymbols.length ? ` · 覆盖未知 ${progress.unknownSymbols.join("、")}（旧版本未记录，未虚构缺口）` : ""}
              {progress?.gapSymbols.length ? ` · 有实际缺口 ${progress.gapSymbols.join("、")}` : ""}
              · 任务 {sourceLabel(shown.status)}。
            </span>
          )}
          {shown.results.length > 0 && activeStatuses.includes(shown.status) && (
            <span> {stage}。可展开查看逐只进展。</span>
          )}
        </div>
      )}
      {shown && (!shown.results.length || shown.status !== "SUCCEEDED") && (
        <details className="panel analysis-task-progress">
          <summary className="section-heading">
            {display(shown.params.tickers)} · {sourceLabel(shown.status)} ·
            查看行情与任务进度
          </summary>
          <div className="section-heading">
            <h2>行情与计算进度</h2>
            <span>{sourceLabel(shown.status)}</span>
            <div className="button-row">
              <BatchActions batch={shown.batch} />
            </div>
          </div>
          <BatchScopes batch={shown.batch} />
        </details>
      )}
      {!shown && !id && (
        <section className="panel">
          <EmptyState
            title="选择股票与范围，开始比较"
            description="应用条件后先复用本地数据，后台仅获取缺少的范围。历史年数默认 8，可按研究需要修改。"
          />
        </section>
      )}
      {shown && !shown.results.length && (
        <section className="panel">
          <EmptyState
            title="所选范围正在准备"
            description="先复用本地行情，缺少的范围在后台获取。任一股票或安全的部分结果准备好后立即显示，其他工作继续。"
          />
        </section>
      )}
      {shown && shown.results.length > 0 && (
        <>
          <section
            className="panel analysis-results"
            data-analysis-id={shown.id}
            data-result-ids={resultIds}
          >
            <div className="section-heading">
              <div>
                <h2>
                  {selectedItem?.symbol} ·{" "}
                  {tabs.find((t) => t.id === kind)?.label}
                </h2>
                <p className="research-question">研究问题：{kind === "monthly"
                  ? `${shown.params.month} 月历史价格表现如何？`
                  : `${shown.params.start_mmdd} → ${shown.params.end_mmdd} 区间历史价格表现如何？`}</p>
              </div>
              <details className="research-exports"><summary>导出与链接</summary><div className="button-row">
                <a
                  className="button button-secondary"
                  href={`/api/v1/analyses/${shown.id}/export?result_ids=${encodeURIComponent(resultIds)}`}
                  download
                >
                  导出当前结果 CSV
                </a>
                <a
                  className="button button-secondary"
                  href={`/api/v1/analyses/${shown.id}/export?format=json&result_ids=${encodeURIComponent(resultIds)}`}
                  download
                >
                  导出当前结果 JSON
                </a>
                <Button
                  variant="ghost"
                  onClick={() =>
                    navigator.clipboard
                      .writeText(snapshotUrl.toString())
                      .then(() => notice({ text: "研究链接已复制；行情过期后打开会自动重新获取。" }))
                      .catch(() =>
                        notice({
                          text: "无法访问剪贴板，请复制浏览器地址。",
                          error: true,
                        }),
                      )
                  }
                >
                  复制链接
                </Button>
              </div></details>
            </div>
            {shown.results.length > 1 && (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>股票</th>
                      <th>
                        {kind === "monthly"
                          ? `${shown.params.month} 月走势 N`
                          : "有效样本 N"}
                      </th>
                      <th>
                        {kind === "monthly" ? "细看月份历史中位" : "历史中位"}
                      </th>
                      <th>
                        {kind === "monthly" ? "细看月份当前对照" : "当前对照"}
                      </th>
                      <th>覆盖状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.results.map((r) => (
                      <tr
                        key={r.symbol}
                        className={r === selectedItem ? "selected-row" : ""}
                      >
                        <td>
                          <button
                            className="text-link"
                            onClick={() => { refreshEpoch.current += 1; setSelected(r.symbol); }}
                          >
                            {r.symbol}
                          </button>
                        </td>
                        <td>{display(r.data.effective_n)}</td>
                        <td>{percent(facts(r.data.summary).median)}</td>
                        <td>
                          {percent(facts(facts(r.data.summary).current).endpoint)}
                        </td>
                        <td>
                          {r.coverage
                            ? r.coverage.complete
                              ? "本版本目标行情已覆盖"
                              : `${display(r.coverage.valid_sessions)}/${display(r.coverage.expected_sessions)} 交易日 · 部分结果`
                            : "原版本未记录目标覆盖"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="result-meta">
              <strong>{selectedItem?.symbol}</strong>
              <span>截至 {display(meta.cutoff_date)}</span>
              <span>{sourceLabel(meta.price_basis)}</span>
              <span>
                {selectedItem?.coverage
                  ? selectedItem.coverage.complete
                    ? "目标行情已覆盖"
                    : "部分结果 · 行情尚未完整"
                  : "已保存结果 · 原版本未记录目标覆盖"}
              </span>
              <span>
                有效 N={display(data.effective_n)} / 目标{" "}
                {display(meta.target_n)}
              </span>
            </div>
            {kind === "monthly" && meta.comparison === "same_progress" && (
              <p className="context-baseline">截至同期走势只计算到各历史年与当前研究相同的月内位置；下方 1—12 月分布与排名使用各年完整月份。两种终点的中位数可以不同。</p>
            )}
            <details className="analysis-version" data-result-id={selectedItem?.result_id}>
              <summary>覆盖明细、方法与技术版本</summary>
              {selectedItem?.coverage && (
                <p>
                  本结果覆盖 {selectedItem.coverage.valid_sessions}/
                  {selectedItem.coverage.expected_sessions} 个目标交易日 · 目标{" "}
                  {selectedItem.coverage.start_date} →{" "}
                  {selectedItem.coverage.end_date} · 已有{" "}
                  {selectedItem.coverage.first_valid_date ?? "尚无"} →{" "}
                  {selectedItem.coverage.last_valid_date ?? "尚无"} · 剩余缺口{" "}
                  {(selectedItem.coverage.missing_dates ?? []).length} 个交易日
                </p>
              )}
              <small>
                结果版本 {selectedItem?.result_id} · 数据版本{" "}
                {selectedItem?.input_version.slice(0, 12)} · 计算{" "}
                {display(meta.calculation_version)}。图、表及导出采用此版本。
              </small>
              {selectedItem?.coverage?.missing_dates?.length ? (
                <details>
                  <summary>查看本版本剩余行情缺口</summary>
                  <p>{(selectedItem.coverage.missing_dates ?? []).join("、")}</p>
                </details>
              ) : null}
            </details>
            {kind === "monthly" && rows(data.monthly_rankings).length > 0 && (
              <p className="context-baseline">
                全年 1—12
                月完整历史分布与排名见下表；当前月份高亮，当前年度不计入历史排名。
              </p>
            )}
            {kind !== "monthly" && (
              <div className="summary-grid">
                <div>
                  <span>历史中位变化</span>
                  <strong>{percent(summary.median)}</strong>
                </div>
                <div>
                  <span>上涨年数 / N</span>
                  <strong>
                    {display(summary.up)} / {display(summary.n)}
                  </strong>
                </div>
                <div>
                  <span>历史最差表现</span>
                  <strong>{percent(summary.worst)}</strong>
                </div>
                <div>
                  <span>当前对照</span>
                  <strong>{percent(current.endpoint)}</strong>
                  <small>{sourceLabel(current.status)}</small>
                </div>
              </div>
            )}
            {selectedItem && <ResearchEvidence data={preciseData} readingId={readingKey} defaultMetric="endpoint" />}
            <StatisticsPanel
              key={`statistics:${readingKey}:${conditionKey}`}
              readingId={readingKey}
              data={preciseData}
              defaultMetric="endpoint"
              title={
                kind === "monthly"
                  ? "1—12 月历史涨跌幅与排名"
                  : "历史窗口涨跌幅分布"
              }
              busy={isApplying}
              onSelect={({ group }) => {
                if (group && kind === "monthly") selectResult({ month: Number(group) });
              }}
            />
            {kind === "monthly" && (
              <ResearchChart
                data={preciseData}
                title="年份 × 月份总览"
                heatmap
                key={`${readingKey}:overview`}
                readingId={`${readingKey}:overview`}
                provenance={annualProvenance}
                onCell={(cell) => selectResult({ month: Number(cell.month) })}
              />
            )}
            {kind === "monthly" && (
              <div className="section-heading">
                <h3>单月走势细看</h3>
                <label>
                  查看月份{" "}
                  <select
                    aria-label="查看月份"
                    value={Number(shown.params.month)}
                    disabled={isApplying}
                    onChange={(e) =>
                      selectResult({ month: Number(e.target.value) })
                    }
                  >
                    {Array.from({ length: 12 }, (_, i) => (
                      <option key={i} value={i + 1}>
                        {i + 1} 月
                        {i + 1 === marketCalendar().month ? " · 当前月份" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            <ResearchChart
              data={preciseData}
              title={`${selectedItem?.symbol} · ${kind === "monthly" ? `${shown.params.month} 月` : `${shown.params.start_mmdd} → ${shown.params.end_mmdd}`} 价格路径`}
              key={`${readingKey}:path`}
              readingId={`${readingKey}:path`}
              provenance={provenance}
            />
            <div className="section-heading">
              <h2>逐次明细</h2>
              <span>与图表及导出使用同一结果</span>
            </div>
            <ResearchTable
              key={`${readingKey}:${conditionKey}`}
              data={rows(preciseData.rows)}
            />
            <details className="result-notes">
              <summary>数据口径、来源与排除原因</summary>
              <p>
                数据版本：{selectedItem?.input_version} · 结果：
                {selectedItem?.result_id}
              </p>
              <p>
                历史组：{display(meta.historical_years)} · 当前对照：
                {display(meta.current_year)}
              </p>
              <p>
                交易日历：{display(meta.calendar)} ·{" "}
                {display(meta.calendar_version)}
              </p>
              <p>{display(meta.warnings)}</p>
              {rows(data.exclusions).map((x, i) => (
                <p key={i}>{display(x)}</p>
              ))}
              <p>
                历史中间 50%
                为样本分位范围，不是预测区间。缺失交易日不会按休市日填充。
              </p>
            </details>
          </section>
          {shown.results.length > 1 && (
            <section className="panel">
              <div className="section-heading">
                <h2>跨股票比较</h2>
                <label>
                  显示
                  <select
                    value={compare}
                    onChange={(e) => setCompare(e.target.value)}
                  >
                    <option value="single">选择比较方式</option>
                    <option value="current">各股当前对照</option>
                    <option value="median">各股历史中位路径</option>
                  </select>
                </label>
              </div>
              <div className="ticker-checkboxes">
                {shown.results.map((r) => (
                  <label key={r.symbol}>
                    <input
                      type="checkbox"
                      checked={displayedTickers.includes(r.symbol)}
                      disabled={
                        !displayedTickers.includes(r.symbol) &&
                        displayedTickers.length >= 5
                      }
                      onChange={(e) =>
                        setChartTickers(
                          e.target.checked
                            ? [...displayedTickers, r.symbol]
                            : displayedTickers.filter((t) => t !== r.symbol),
                        )
                      }
                    />
                    {r.symbol}
                  </label>
                ))}
                <span>最多 5 条曲线</span>
              </div>
              {compare !== "single" && comparisonItems.length < 2 && (
                <p className="notice notice-info" role="status">至少两只证券的当前结果具备同一计算版本、研究条件、价格口径和截止日后，才显示共同对比图。旧结果仍可逐只查看。</p>
              )}
              {compare !== "single" && comparisonItems.length >= 2 && (
                <ResearchChart
                  data={comparisonData}
                  readingId={`analysis:${shown.id}:comparison:${compare}`}
                  title={
                    compare === "current" ? "各股当前对照" : "各股历史中位路径"
                  }
                  provenance={`${comparisonItems.map((r) => r.symbol).join(", ")} · ${sourceLabel(meta.price_basis)} · ${kind === "monthly" ? `${shown.params.month}月` : `${shown.params.start_mmdd}–${shown.params.end_mmdd}`} · ${sourceLabel(meta.comparison)}\n分析 ${shown.id} · ${display(meta.calculation_version)}\n${comparisonItems
                    .map(
                      (r) =>
                        `${r.symbol}: N=${r.data.effective_n} 截至${display(facts(r.data.metadata).cutoff_date)} ${display(facts(r.data.metadata).source)} 结果${r.result_id}`,
                    )
                    .join("\n")}`}
                />
              )}
            </section>
          )}
        </>
      )}
    </>
  );
}
