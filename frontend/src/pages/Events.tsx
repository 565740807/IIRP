import { ResearchReadStatus, ResearchVersionActions } from "../components/ResearchReading";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import type { components } from "../generated/api";
import { eventScopeConflict, eventAnalysisForImportedScope } from "../eventScopeCompatibility";
import {
  createEventResearchRefresher,
  eventFollowParams,
} from "../eventResearchRefresh";
import {
  useStoredDraft,
  writeDraft,
  initializeDraft,
  patchDraft,
  yearList,
  serializeResearch,
  researchHref,
  useContextSearchParams,
  sourceHref,
} from "../researchStorage";
import { useEventResearch, useEventSet, useEventSets, useResearchBatch, eventResearchOptions, type EventAnalysisOutput, type EventSetOutput } from "../researchQueries";
import { researchKeys } from "../queryIdentity";
import { BenchmarkPicker } from "../components/StatisticsPanel";
import {
  api,
  display,
  facts,
  formatTime,
  requestId,
  rows,
  type Fact,
  type ApiFailure,
} from "../api";
import { formatRequestedQuarters, importGuidance, importedScopeHistoricalYears, importedScopeDateCategory, requestedQuarterError } from "../eventImportGuidance";
import { eventPromptConditions, eventPromptDraftKey, eventConditionsForLanguage, eventCommonYearsLabel, restoreEventPromptConditions } from "../eventPromptConditions";
import { BatchPanel } from "../components/Batches";
import {
  EventDateSources,
  EventDatesResult,
  eventLabel,
} from "../components/EventDatesResult";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  useNotice,
} from "../components/ui";

type PreviewInput = components["schemas"]["EventPreviewInput"];
type ConfirmInput = components["schemas"]["EventConfirmInput"];
type ReviewInput = components["schemas"]["EventReviewInput"];
type EventAnalysisInput = components["schemas"]["EventAnalysisInput"];
type PendingStart = {
  id: string;
  selectedVersion: number;
  request?: EventAnalysisInput;
  error?: string;
  auto?: boolean;
  navigation?: string;
  preserveDraft?: boolean;
  epoch?: number;
  draftSignature?: string;
  origin?: { analysisId: string; version: string; resultId: string };
};
const commandFields = ["pendingStart"] as const;
function researchDraftSignature(value: Record<string, unknown>) {
  return JSON.stringify(["cutoff", "fiscalYear", "includeUnverified", "benchmark", "customBenchmark",
    "historicalYears", "commonYears", "dateWindow", "dateCategory", "researchYears", "excludeYears"]
    .map(key => value[key]));
}
const json = (value: unknown) => JSON.stringify(value, null, 2);
const eventIdentity = (event: Fact) =>
  `${event.event_type}|${event.event_name}|${event.event_date}`;

function CoverageTable({ document }: { document: Fact }) {
  return (
    <details className="result-notes">
      <summary>
        逐年查询覆盖与未找到的资料（{rows(document.coverage).length} 项）
      </summary>
      <p>
        “已查询”是导入资料的声明，不证明该年没有遗漏。未找到资料与明确未举办分别记录。
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>年份 / 财季</th>
              <th>类别</th>
              <th>查询状态</th>
              <th>查询结果</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {rows(document.coverage).map((item, index) => (
              <tr key={index}>
                <td>
                  {display(item.year ?? item.fiscal_year)}
                  {item.fiscal_quarter ? ` Q${item.fiscal_quarter}` : ""}
                </td>
                <td>{eventLabel(item.event_type ?? "earnings_release")}</td>
                <td>{eventLabel(item.search_status)}</td>
                <td>{eventLabel(item.result_status)}</td>
                <td>
                  {display(item.notes)}
                  <EventDateSources
                    sources={
                      Array.isArray(item.source_urls)
                        ? item.source_urls.map((url: string, i: number) => ({
                            url,
                            title: `来源 ${i + 1}`,
                          }))
                        : []
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
function EventEditor({
  event,
  earnings,
  onChange,
}: {
  event: Fact;
  earnings: boolean;
  onChange: (event: Fact) => void;
}) {
  const patch = (key: string, value: unknown) =>
    onChange({ ...event, [key]: value });
  const editSource = (index: number, key: string, value: unknown) =>
    patch(
      "sources",
      rows(event.sources).map((source, i) =>
        i === index ? { ...source, [key]: value } : source,
      ),
    );
  const supportedFields = [
    "event_date",
    "event_time",
    "timezone",
    "event_status",
    ...(earnings
      ? [
          "release_session",
          "fiscal_year",
          "fiscal_quarter",
          "period_end",
          "period_start",
          "period_kind",
        ]
      : ["event_type"]),
  ];
  const fieldLabel: Record<string, string> = {
    event_date: "事件日期",
    event_time: "时刻",
    timezone: "时区",
    event_status: "实际发生状态",
    event_type: "事件类型",
    release_session: "公告时段",
    fiscal_year: "财年",
    fiscal_quarter: "财季",
    period_end: "财务期末",
    period_start: "财季实际起点",
    period_kind: "财期类型",
  };
  return (
    <details className="result-notes">
      <summary>修改事件日期、类别或来源</summary>
      <div className="analysis-form">
        <label>
          活动名称
          <input
            value={event.event_name ?? ""}
            onChange={(e) => patch("event_name", e.target.value)}
          />
        </label>
        <label>
          事件类型
          <input
            value={event.event_type ?? ""}
            disabled={earnings}
            onChange={(e) => patch("event_type", e.target.value)}
          />
        </label>
        <label>
          事件当地日期
          <input
            type="date"
            value={event.event_date ?? ""}
            onChange={(e) => patch("event_date", e.target.value || null)}
          />
        </label>
        <label>
          活动自然年
          <input
            type="number"
            min="1"
            max="9998"
            value={event.event_year ?? ""}
            onChange={(e) =>
              patch(
                "event_year",
                e.target.value ? Number(e.target.value) : null,
              )
            }
          />
        </label>
        <label>
          来源明确的时刻
          <input
            type="time"
            value={event.event_time ?? ""}
            onChange={(e) => patch("event_time", e.target.value || null)}
          />
        </label>
        <label>
          IANA 时区
          <input
            value={event.timezone ?? ""}
            placeholder="未知留空"
            onChange={(e) => patch("timezone", e.target.value || null)}
          />
        </label>
        <label>
          时间精度
          <select
            value={event.time_precision}
            onChange={(e) => patch("time_precision", e.target.value)}
          >
            {["minute", "date", "unknown"].map((value) => (
              <option key={value} value={value}>
                {eventLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          时刻依据
          <select
            value={event.time_basis}
            onChange={(e) => patch("time_basis", e.target.value)}
          >
            {["reported_actual", "official_schedule", "unknown"].map(
              (value) => (
                <option key={value} value={value}>
                  {eventLabel(value)}
                </option>
              ),
            )}
          </select>
        </label>
        <label>
          活动状态
          <select
            value={event.event_status}
            onChange={(e) => patch("event_status", e.target.value)}
          >
            {["occurred", "scheduled", "cancelled", "unknown"].map((value) => (
              <option key={value} value={value}>
                {eventLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          资料日期状态
          <select
            value={event.date_status}
            onChange={(e) => patch("date_status", e.target.value)}
          >
            {["supported", "conflicting", "unverified"].map((value) => (
              <option key={value} value={value}>
                {eventLabel(value)}
              </option>
            ))}
          </select>
        </label>
        {earnings && (
          <>
            <label>
              财年
              <input
                type="number"
                value={event.fiscal_year ?? ""}
                onChange={(e) =>
                  patch(
                    "fiscal_year",
                    e.target.value ? Number(e.target.value) : null,
                  )
                }
              />
            </label>
            <label>
              财季
              <select
                value={event.fiscal_quarter ?? ""}
                onChange={(e) =>
                  patch(
                    "fiscal_quarter",
                    e.target.value ? Number(e.target.value) : null,
                  )
                }
              >
                <option value="">待核对</option>
                {[1, 2, 3, 4].map((quarter) => (
                  <option key={quarter} value={quarter}>
                    Q{quarter}
                  </option>
                ))}
              </select>
            </label>
            <label>
              报告期末
              <input
                type="date"
                value={event.period_end ?? ""}
                onChange={(e) => patch("period_end", e.target.value || null)}
              />
            </label>
            <label>
              财季实际起点（有来源才填写）
              <input
                type="date"
                value={event.period_start ?? ""}
                onChange={(e) => patch("period_start", e.target.value || null)}
              />
            </label>
            <label>
              财期类型
              <select
                value={event.period_kind}
                onChange={(e) => patch("period_kind", e.target.value)}
              >
                {["regular", "transition", "unknown"].map((value) => (
                  <option key={value} value={value}>
                    {eventLabel(value)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              有依据的公告时段
              <select
                value={event.release_session}
                onChange={(e) => patch("release_session", e.target.value)}
              >
                {[
                  "before_open",
                  "during_session",
                  "after_close",
                  "unknown",
                ].map((value) => (
                  <option key={value} value={value}>
                    {eventLabel(value)}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>
      {rows(event.sources).map((source, index) => (
        <div className="panel-body" key={index}>
          <h4>来源 {index + 1}</h4>
          <div className="analysis-form">
            <label>
              来源链接
              <input
                type="url"
                value={source.url ?? ""}
                onChange={(e) => editSource(index, "url", e.target.value)}
              />
            </label>
            <label>
              资料标题
              <input
                value={source.title ?? ""}
                onChange={(e) => editSource(index, "title", e.target.value)}
              />
            </label>
            <label>
              发布方
              <input
                value={source.publisher ?? ""}
                onChange={(e) => editSource(index, "publisher", e.target.value)}
              />
            </label>
            <label>
              资料发布日期
              <input
                type="date"
                value={source.published_date ?? ""}
                onChange={(e) =>
                  editSource(index, "published_date", e.target.value || null)
                }
              />
            </label>
            <label>
              来源类别
              <select
                value={source.source_kind}
                onChange={(e) =>
                  editSource(index, "source_kind", e.target.value)
                }
              >
                <option value="primary">一手来源</option>
                <option value="secondary">二手来源</option>
                <option value="unknown">来源类型待核对</option>
              </select>
            </label>
            <label className="csv-field">
              该来源支持的事实
              <textarea
                value={source.evidence_note ?? ""}
                onChange={(e) =>
                  editSource(index, "evidence_note", e.target.value)
                }
              />
            </label>
          </div>
          <div className="ticker-checkboxes">
            {supportedFields.map((field) => (
              <label key={field}>
                <input
                  type="checkbox"
                  checked={
                    Array.isArray(source.supports) &&
                    source.supports.includes(field)
                  }
                  onChange={(e) =>
                    editSource(
                      index,
                      "supports",
                      e.target.checked
                        ? [...(source.supports ?? []), field]
                        : (source.supports ?? []).filter(
                            (item: string) => item !== field,
                          ),
                    )
                  }
                />
                {fieldLabel[field]}
              </label>
            ))}
          </div>
        </div>
      ))}
      <div className="panel-body">
        <Button
          variant="ghost"
          onClick={() =>
            patch("sources", [
              ...rows(event.sources),
              {
                url: "",
                title: "",
                publisher: "",
                published_date: null,
                source_kind: "primary",
                supports: ["event_date"],
                evidence_note: "",
              },
            ])
          }
        >
          添加来源
        </Button>
        <p>
          资料修改后需重新检查，人工核对标记会重置。若改变年份或类别，请同步更新原始
          JSON 中的查询范围与覆盖，或让 AI 重新输出完整资料。
        </p>
      </div>
    </details>
  );
}

export function EventsPage() {
  const [params] = useContextSearchParams();
  return (
    <EventsWorkspace
      key={eventPromptDraftKey(params, params.get("kind") === "earnings" ? "earnings" : "custom")}
    />
  );
}

function EventsWorkspace() {
  const location = useLocation();
  const [params, setParams] = useContextSearchParams();
  const kind = params.get("kind") === "earnings" ? "earnings" : "custom";
  const setId = params.get("set") ?? "";
  const version = params.get("version") ?? "";
  const resultId =
    params.get("result") ?? params.get("result_ids")?.split(",")[0] ?? "";
  const editEpoch = useRef(0);
  const navigationRef = useRef(location.key);
  const previousLocation = useRef(location.key);
  const automaticLocation = useRef<{ from: string; to: string } | null>(null);
  const explicitNavigation = useRef(0);
  if (previousLocation.current !== location.key) {
    const automatic = automaticLocation.current?.from === previousLocation.current &&
      automaticLocation.current.to === location.pathname + location.search;
    if (!automatic) {
      navigationRef.current = location.key;
      editEpoch.current++;
      explicitNavigation.current++;
    }
    previousLocation.current = location.key;
    automaticLocation.current = null;
  }
  const replaceParams = useCallback((next: URLSearchParams, replace = true) => {
    const to = `${location.pathname}?${next}`;
    if (to === location.pathname + location.search) return;
    automaticLocation.current = { from: location.key, to };
    setParams(next, { replace });
  }, [location.key, location.pathname, location.search, setParams]);
  const client = useQueryClient();
  const notice = useNotice();
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [importOpen, setImportOpen] = useState(!setId);
  const [text, setText] = useState("");
  const initialPromptLanguage = params.get("language") === "en" ? "en" : "zh";
  const [conditions, setConditions] = useState(() => eventPromptConditions(params, kind, initialPromptLanguage));
  const [promptLanguage, setPromptLanguage] = useState<"zh" | "en">(initialPromptLanguage);
  const [importRepair, setImportRepair] = useState("");
  const [preview, setPreview] = useState<Fact | null>(null);
  const [edited, setEdited] = useState<Fact | null>(null);
  const [reviews, setReviews] = useState<Record<string, ReviewInput>>({});
  const [security, setSecurity] = useState("");
  const [title, setTitle] = useState("");
  const [revision, setRevision] = useState<{
    set_id: string;
    version: number;
  } | null>(null);
  const [revisionNote, setRevisionNote] = useState("");
  const [cutoff, setCutoff] = useState(params.get("cutoff_date") ?? "");
  const [fiscalYear, setFiscalYear] = useState(
    params.get("current_fiscal_year") ?? "",
  );
  const [includeUnverified, setIncludeUnverified] = useState(false);
  const [benchmark, setBenchmark] = useState(params.get("benchmark") ?? "");
  const [customBenchmark, setCustomBenchmark] = useState(false);
  const [historicalYears, setHistoricalYears] = useState(
    Number(params.get("historical_years")) || 8,
  );
  const [commonYears, setCommonYears] = useState(
    params.get("common_years") === "true",
  );
  const [dateWindow, setDateWindow] = useState<
    EventAnalysisInput["date_window"]
  >(
    (["before5", "day0", "after5", "through5"].includes(
      params.get("date_window") ?? "",
    )
      ? params.get("date_window")
      : "after5") as EventAnalysisInput["date_window"],
  );
  const [dateCategory, setDateCategory] = useState<string | null>(
    params.get("date_category") ??
      (params.get("quarter") ? `Q${params.get("quarter")}` : null),
  );
  const [researchYears, setResearchYears] = useState(params.get("years") ?? "");
  const [excludeYears, setExcludeYears] = useState(
    params.get("excluded_years") ?? "",
  );
  const [actionError, setActionError] = useState<Error | null>(null);
  const [storageWarning, setStorageWarning] = useState<Error | null>(null);
  const commands = useRef(new Map<string, { payload: string; id: string }>());
  const [pendingStart, setPendingStart] = useState<PendingStart | null>(null);
  const pendingIntent = useRef<{ navigation: string; epoch: number; requestId?: string } | null>(null);
  const activePendingStart = pendingStart && pendingIntent.current?.navigation === navigationRef.current &&
    pendingIntent.current.epoch === editEpoch.current &&
    pendingIntent.current.requestId === pendingStart.request?.request_id ? pendingStart : null;
  const [conditionsDirty, setConditionsDirty] = useState(false);
  const storageKey = eventPromptDraftKey(params, kind);
  const snapshot = () => ({
    text,
    conditions,
    promptLanguage,
    preview,
    edited,
    reviews,
    security,
    title,
    revision,
    revisionNote,
    cutoff,
    fiscalYear,
    includeUnverified,
    pendingStart,
    conditionsDirty,
    benchmark,
    customBenchmark,
    historicalYears,
    commonYears,
    dateWindow,
    dateCategory,
    researchYears,
    excludeYears,
    commands: Array.from(commands.current.entries()),
  });
  const storage = useStoredDraft(storageKey, snapshot(), (stored) => {
    setText(stored.text);
    setConditions(restoreEventPromptConditions(stored.conditions, params, kind, stored.promptLanguage ?? "zh"));
    setPromptLanguage(stored.promptLanguage ?? "zh");
    setPreview(stored.preview);
    setEdited(stored.edited);
    setReviews(stored.reviews);
    setSecurity(stored.security);
    setTitle(stored.title);
    setRevision(stored.revision);
    setRevisionNote(stored.revisionNote);
    setCutoff(stored.cutoff);
    setFiscalYear(stored.fiscalYear);
    setIncludeUnverified(stored.includeUnverified);
    const savedPending = stored.pendingStart ?? null;
    const newerDraft = savedPending?.draftSignature
      ? savedPending.draftSignature !== researchDraftSignature(stored)
      : !!savedPending && !!stored.conditionsDirty;
    const pending = savedPending && newerDraft ? { ...savedPending, auto: false, preserveDraft: true } : savedPending;
    setPendingStart(pending);
    const origin = pending?.origin;
    pendingIntent.current = pending && !newerDraft && (!origin || (
      origin.analysisId === (params.get("a") ?? "") && origin.version === version && origin.resultId === resultId
    )) ? { navigation: navigationRef.current, epoch: editEpoch.current, requestId: pending.request?.request_id } : null;
    setConditionsDirty(stored.conditionsDirty ?? false);
    setBenchmark(stored.benchmark ?? "");
    setCustomBenchmark(stored.customBenchmark ?? false);
    setHistoricalYears(stored.historicalYears ?? 8);
    setCommonYears(stored.commonYears ?? false);
    setDateWindow(stored.dateWindow ?? "after5");
    setDateCategory(stored.dateCategory ?? null);
    setResearchYears(stored.researchYears ?? "");
    setExcludeYears(stored.excludeYears ?? "");
    commands.current = new Map(stored.commands ?? []);
  }, commandFields);
  useEffect(() => {
    const handle = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          key: string;
          value?: ReturnType<typeof snapshot>;
        }>
      ).detail;
      if (detail.key === storageKey && detail.value)
        setPendingStart(detail.value.pendingStart);
    };
    window.addEventListener("iirp:draft-command", handle);
    return () => window.removeEventListener("iirp:draft-command", handle);
  }, [storageKey]);
  async function persistDraft(key: string, value: unknown) {
    try {
      await writeDraft(key, value, commandFields);
    } catch {
      setStorageWarning(
        new Error(
          "浏览器未能保存草稿副本；服务器保存和任务仍可继续。请保留原文，已保存研究可从最近研究重新打开。",
        ),
      );
    }
  }
  async function persistCommand(
    pending: PendingStart | null,
    expectedRequestId?: string,
  ) {
    try {
      await patchDraft<ReturnType<typeof snapshot>>(storageKey, (value) =>
        expectedRequestId &&
        value.pendingStart?.request?.request_id !== expectedRequestId
          ? value
          : {
              ...value,
              pendingStart: pending,
            },
      );
    } catch {
      if (mounted.current)
        setStorageWarning(
          new Error(
            "浏览器未能保存启动状态；服务器任务仍可继续。请保留当前页面，或从已保存事件集重新读取分析。",
          ),
        );
    }
  }
  useEffect(() => {
    try {
      if (setId)
        localStorage.setItem(
          `iirp:last-events:${kind}`,
          `/analysis/events?${eventFollowParams(params)}`,
        );
    } catch {
      /* Server-side event sets remain readable. */
    }
  }, [kind, setId, params]);
  useEffect(() => {
    if (
      setId ||
      params.has("new") ||
      params.has("ticker") ||
      params.has("tickers")
    )
      return;
    try {
      const href = localStorage.getItem(`iirp:last-events:${kind}`);
      if (href?.startsWith("/analysis/events?"))
        setParams(eventFollowParams(new URLSearchParams(href.split("?")[1])), {
          replace: true,
        });
    } catch {
      /* Drafts remain accessible. */
    }
  }, [kind, setId, params, setParams]);
  function modeHref(value: string) {
    try {
      const href = localStorage.getItem(`iirp:last-events:${value}`);
      if (href?.startsWith("/analysis/events?"))
        return `/analysis/events?${eventFollowParams(new URLSearchParams(href.split("?")[1]))}`;
    } catch {
      /* Optional navigation state. */
    }
    return `/analysis/events?kind=${value}`;
  }
  const limits = useQuery({
    queryKey: ["event-import-limits"],
    queryFn: () =>
      api<components["schemas"]["EventImportLimitsOutput"]>("/events/limits"),
    staleTime: Infinity,
  });
  function commandId(key: string, value: unknown) {
    const payload = JSON.stringify(value);
    const previous = commands.current.get(key);
    if (previous?.payload === payload) return previous.id;
    const id = requestId();
    commands.current.set(key, { payload, id });
    return id;
  }
  const prompt = useQuery({
    queryKey: ["event-prompt", kind, promptLanguage],
    queryFn: () =>
      api<components["schemas"]["EventPromptOutput"]>(
        `/events/prompt?kind=${kind}&language=${promptLanguage}`,
      ),
    staleTime: Infinity,
  });
  const sets = useEventSets(kind);
  const current = useEventSet(setId, version);
  const saved: Partial<EventSetOutput> = current.data ?? {};
  const reviewTarget = params.get("review_event");
  useEffect(() => {
    if (reviewTarget == null || !saved.version) return;
    const target =
      window.document.getElementById(`event-source-${reviewTarget}`) ??
      window.document.getElementById("event-source-management");
    if (!target) return;
    for (let node: HTMLElement | null = target; node; node = node.parentElement)
      if (node instanceof HTMLDetailsElement) node.open = true;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "center", behavior: "instant" });
  }, [reviewTarget, saved.version]);
  useEffect(() => {
    const savedKind = saved.kind;
    if (savedKind && savedKind !== kind) {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set("kind", savedKind);
          return next;
        },
        { replace: true },
      );
    }
  }, [saved.kind, kind, setParams]);
  const compatibleAnalyses = rows(saved.analyses).filter(
    (item) =>
      Number(facts(item.params).event_version) === Number(saved.version),
  );
  const analysisId = params.get("a") ?? compatibleAnalyses[0]?.id ?? "";
  const content = useEventResearch(analysisId, resultId);
  const task = useResearchBatch(resultId ? content.data?.batch_id ?? "" : "");
  const analysisData = useMemo(() => {
    const value = content.data, batch = task.data?.batch;
    if (!value || !batch || batch.id !== value.batch_id) return value;
    return { ...value, status: batch.status, requested_action: batch.requested_action ?? null,
      progress: batch.items.map(scope => ({ ...scope.progress, symbol: scope.symbol,
        status: scope.status, wait_reason: scope.wait_reason ?? null })) };
  }, [content.data, task.data]);
  const analysis = { ...content, data: analysisData };
  const requestParams = facts(analysis.data?.params);
  const scopeMatchesRequest = saved.id === requestParams.event_set_id
    && Number(saved.version) === Number(requestParams.event_version);
  const importedScope = facts(facts(saved.document).scope);
  const scopeConflict = scopeMatchesRequest ? eventScopeConflict(requestParams, importedScope) : null;
  const freshness = facts(analysis.data?.freshness);
  const originId = String(freshness.origin_id ?? analysisId);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<Error | null>(null);
  const refreshContext = useRef({
    analysisId,
    originId,
    snapshotId: resultId,
    dirty: conditionsDirty,
    ready: false,
  });
  refreshContext.current = {
    analysisId,
    originId,
    snapshotId: resultId,
    dirty: conditionsDirty,
    ready:
      storage.ready &&
      !!analysis.data &&
      scopeMatchesRequest &&
      !scopeConflict &&
      !activePendingStart &&
      params.get("a") === analysisId &&
      !!version,
  };
  const refresher = useRef<ReturnType<
    typeof createEventResearchRefresher<EventAnalysisOutput>
  > | null>(null);
  const refreshNavigation = useRef({ replaceParams, params });
  refreshNavigation.current = { replaceParams, params };
  useEffect(() => {
    const controller = createEventResearchRefresher<EventAnalysisOutput>({
      current: () => ({
        ...refreshContext.current,
        navigation: navigationRef.current,
        epoch: editEpoch.current,
      }),
      visible: () => !window.document.hidden,
      request: async (id, manual) => {
        const next = await api<components["schemas"]["AnalysisRefreshOutput"]>(
          `/analyses/${encodeURIComponent(id)}/refresh${manual ? "?force=true" : ""}`,
          "POST",
          {},
        );
        await client.cancelQueries({ queryKey: researchKeys.events(next.id), exact: true });
        return client.fetchQuery({ ...eventResearchOptions(next.id), staleTime: 0 });
      },
      started: () => {
        setRefreshing(true);
        setRefreshError(null);
      },
      received: (value, context) => {
        // Cancel an older GET before publishing the refresh response to this cache key.
        void client.cancelQueries({
          queryKey: researchKeys.events(value.id),
          exact: true,
        });
        client.setQueryData(researchKeys.events(value.id), value);
        if (value.id !== context.analysisId || context.snapshotId)
          refreshNavigation.current.replaceParams(
            eventFollowParams(refreshNavigation.current.params, value.id),
          );
      },
      failed: (error) =>
        setRefreshError(
          error instanceof Error
            ? error
            : new Error("更新检查失败；已保存结果仍可读。"),
        ),
      settled: () => setRefreshing(false),
    });
    refresher.current = controller;
    const check = () => {
      void controller.ensure();
    };
    window.addEventListener("focus", check);
    window.document.addEventListener("visibilitychange", check);
    const timer = window.setInterval(check, 60000);
    return () => {
      controller.dispose();
      window.clearInterval(timer);
      window.removeEventListener("focus", check);
      window.document.removeEventListener("visibilitychange", check);
    };
  }, [client]);
  useEffect(() => {
    void refresher.current?.ensure();
  }, [
    analysis.data,
    analysisId,
    resultId,
    storage.ready,
    activePendingStart,
    conditionsDirty,
    version,
    params,
    scopeMatchesRequest,
    scopeConflict,
  ]);
  const restoredAnalysis = useRef("");
  const navigationGeneration = explicitNavigation.current;
  const restoredNavigation = useRef(navigationGeneration);
  useEffect(() => {
    const explicitSelection = restoredNavigation.current !== navigationGeneration;
    if (explicitSelection) {
      restoredNavigation.current = navigationGeneration;
      restoredAnalysis.current = "";
      setConditionsDirty(false);
      setActionError(null);
      setRefreshError(null);
    }
    if (!storage.ready || !analysis.data || activePendingStart) return;
    const values = facts(analysis.data.params);
    if (restoredAnalysis.current !== analysis.data.id && (!conditionsDirty || explicitSelection)) {
      setBenchmark(values.benchmark ?? "");
      setCustomBenchmark(false);
      setHistoricalYears(values.historical_years ?? 8);
      setCommonYears(values.common_years ?? false);
      setDateWindow(values.date_window ?? "after5");
      setDateCategory(values.date_category ?? null);
      setCutoff(values.cutoff_date ?? "");
      setFiscalYear(
        values.current_fiscal_year ? String(values.current_fiscal_year) : "",
      );
      setResearchYears((values.years ?? []).join(","));
      setExcludeYears((values.excluded_years ?? []).join(","));
      setIncludeUnverified(values.include_unverified ?? false);
    }
    restoredAnalysis.current = analysis.data.id;
    const next = new URLSearchParams(params);
    const fields = serializeResearch(
      Object.fromEntries(
        [
          "benchmark",
          "date_window",
          "date_category",
          "common_years",
          "historical_years",
          "cutoff_date",
          "current_fiscal_year",
          "years",
          "excluded_years",
          "include_unverified",
        ].map((key) => [key, values[key]]),
      ),
    );
    [
      "benchmark",
      "date_window",
      "date_category",
      "common_years",
      "historical_years",
      "cutoff_date",
      "current_fiscal_year",
      "years",
      "excluded_years",
      "include_unverified",
    ].forEach((key) => next.delete(key));
    fields.forEach((value, key) => next.set(key, value));
    next.set("kind", kind);
    next.set("set", setId);
    next.set("version", String(values.event_version));
    next.set("a", analysis.data.id);
    next.delete("new");
    next.delete("draft");
    if (next.toString() !== params.toString()) replaceParams(next);
  }, [
    analysis.data,
    storage.ready,
    activePendingStart,
    replaceParams,
    params,
    conditionsDirty,
    navigationGeneration,
  ]);
  function updateParams(values: Record<string, string | undefined>, automatic = false) {
    editEpoch.current++;
    const next = new URLSearchParams(params);
    Object.entries(values).forEach(([key, value]) =>
      value ? next.set(key, value) : next.delete(key),
    );
    if ("result" in values) next.delete("result_ids");
    if (automatic) replaceParams(next, false);
    else setParams(next);
  }
  function analysisCommand(
    id: string,
    selectedVersion: number,
  ): EventAnalysisInput {
    if (customBenchmark && !benchmark)
      throw new Error("请输入行业ETF代码，或选择仅查看股票。");
    const values = {
      version: selectedVersion,
      ...(cutoff ? { cutoff_date: cutoff } : {}),
      ...(fiscalYear ? { current_fiscal_year: Number(fiscalYear) } : {}),
      include_unverified: includeUnverified,
      historical_years: historicalYears,
      common_years: kind === "earnings" && commonYears,
      date_window: dateWindow,
      date_category: dateCategory,
      benchmark: benchmark || null,
      ...(researchYears.trim() ? { years: yearList(researchYears) } : {}),
      excluded_years: yearList(excludeYears),
    };
    return { ...values, request_id: commandId(`analyze:${id}`, values) };
  }
  // Freeze the command before the first await; retries reuse the saved payload.
  function startAnalysis(input: PendingStart) {
    try {
      const request = input.request ?? analysisCommand(input.id, input.selectedVersion);
      const epoch = ++editEpoch.current;
      const navigation = navigationRef.current;
      const signature = researchDraftSignature(snapshot());
      createAnalysis.mutate({ ...input, request, navigation, epoch,
        draftSignature: input.draftSignature ?? signature,
        preserveDraft: input.preserveDraft || (!!input.draftSignature && input.draftSignature !== signature),
        origin: { analysisId: params.get("a") ?? "", version, resultId } });
    } catch (error) {
      setActionError(error instanceof Error ? error : new Error("无法启动分析。"));
    }
  }
  const createAnalysis = useMutation({
    onMutate: async (input: PendingStart) => {
      const context = { navigation: input.navigation!, epoch: input.epoch!, preserveDraft: input.preserveDraft };
      pendingIntent.current = { ...context, requestId: input.request!.request_id };
      const pending = { id: input.id, selectedVersion: input.selectedVersion, request: input.request!,
        preserveDraft: input.preserveDraft, origin: input.origin, draftSignature: input.draftSignature, auto: false };
      setPendingStart(pending);
      await persistCommand(pending);
      return context;
    },
    mutationFn: async (input: PendingStart) => {
      const request = input.request!;
      let result: components["schemas"]["EventAnalysisCreated"];
      try {
        result = await api<components["schemas"]["EventAnalysisCreated"]>(
          `/events/sets/${encodeURIComponent(input.id)}/analyses`, "POST", request,
        );
      } catch (error) {
        const pending = { id: input.id, selectedVersion: input.selectedVersion, request,
          preserveDraft: input.preserveDraft || !mounted.current || input.navigation !== navigationRef.current || input.epoch !== editEpoch.current,
          origin: input.origin, draftSignature: input.draftSignature, auto: false,
          error: error instanceof Error ? error.message : "分析启动未成功，请继续启动重试" };
        await persistCommand(pending, request.request_id);
        // This is only the recovery checkpoint. Visibility is fenced separately.
        if (mounted.current) setPendingStart(current =>
          current?.request?.request_id === request.request_id ? pending : current);
        throw error;
      }
      await persistCommand(null, request.request_id);
      if (mounted.current) setPendingStart(current =>
        current?.request?.request_id === request.request_id ? null : current);
      return result;
    },
    onSuccess: (result, input, context) => {
      void client.invalidateQueries({ queryKey: ["event-set", input.id] });
      void client.invalidateQueries({ queryKey: ["batches"] });
      if (!mounted.current || context?.navigation !== navigationRef.current || context.epoch !== editEpoch.current) return;
      setConditionsDirty(!!context.preserveDraft);
      updateParams({ set: input.id, version: String(input.selectedVersion), a: result.analysis_id, result: undefined }, true);
      setActionError(null);
      notice({ text: "事件窗口分析已保存，已有结果立即可看，缺口在后台补齐。" });
    },
    onError: (error, _input, context) => {
      if (mounted.current && context?.navigation === navigationRef.current && context.epoch === editEpoch.current)
        setActionError(error);
    },
  });
  const autoStarted = useRef(false);
  useEffect(() => {
    if (storage.ready && activePendingStart?.auto && !autoStarted.current) {
      autoStarted.current = true;
      startAnalysis(activePendingStart);
    }
  }, [storage.ready, activePendingStart]);
  const previewMutation = useMutation({
    onMutate: () => ({
      epoch: editEpoch.current,
      navigation: navigationRef.current,
    }),
    mutationFn: (payload: string) => {
      const scopeError = requestedQuarterError(payload, conditions, kind);
      if (scopeError) throw new Error(scopeError);
      return api<components["schemas"]["EventPreviewOutput"]>("/events/preview", "POST", {
        text: payload,
        ...(revision
          ? { set_id: revision.set_id, expected_version: revision.version }
          : {}),
      } satisfies PreviewInput);
    },
    onSuccess: async (result, payload, context) => {
      if (!mounted.current || context?.navigation !== navigationRef.current)
        return;
      if (context?.epoch !== editEpoch.current) {
        setActionError(
          new Error("检查期间资料已修改，新输入已保留；请重新检查当前资料。"),
        );
        return;
      }
      const importedKind =
        facts(result.document).schema_version === "iirp.earnings-events.v1"
          ? "earnings"
          : "custom";
      const importedYears = importedScopeHistoricalYears(facts(result.document).scope);
      const importedCategory = importedScopeDateCategory(facts(result.document).scope, importedKind);
      setText(payload);
      setImportRepair("");
      // A fresh import supplies the analysis range; prior URL filters must not widen it.
      if (importedYears !== null) setHistoricalYears(importedYears);
      setResearchYears("");
      setExcludeYears("");
      setDateCategory(importedCategory);
      setPreview(result);
      setEdited(facts(result.document));
      setSecurity(
        rows(result.candidates).length === 1
          ? String(rows(result.candidates)[0].id)
          : "",
      );
      setReviews(
        Object.fromEntries(
          rows(result.events).map((event) => [
            event.client_event_id,
            {
              client_event_id: event.client_event_id,
              selected: true,
              date_verified: false,
              time_verified: false,
              period_verified: false,
              note: "",
            } satisfies ReviewInput,
          ]),
        ),
      );
      if (!title)
        setTitle(
          `${display(facts(facts(result.document).company).name)} · ${importedKind === "earnings" ? "财报日期" : "事件日期"}观察`,
        );
      setActionError(null);
      if (importedKind !== kind) {
        await persistDraft(
          `event-draft:${importedKind}:new:${result.preview_id}`,
          {
            ...snapshot(),
            text: payload,
            historicalYears: importedYears ?? historicalYears,
            researchYears: "",
            excludeYears: "",
            dateCategory: importedCategory,
            preview: result,
            edited: facts(result.document),
            reviews: Object.fromEntries(
              rows(result.events).map((event) => [
                event.client_event_id,
                {
                  client_event_id: event.client_event_id,
                  selected: true,
                  date_verified: false,
                  time_verified: false,
                  period_verified: false,
                  note: "",
                },
              ]),
            ),
            security:
              rows(result.candidates).length === 1
                ? String(rows(result.candidates)[0].id)
                : "",
          },
        );
        if (
          !mounted.current ||
          context?.navigation !== navigationRef.current ||
          context.epoch !== editEpoch.current
        )
          return;
        updateParams({
          kind: importedKind,
          new: "1",
          draft: result.preview_id,
          historical_years: String(importedYears ?? historicalYears),
          years: undefined,
          excluded_years: undefined,
          date_category: importedCategory ?? undefined,
          quarter: undefined,
        });
      }
    },
    onError: (error, _payload, context) => {
      if (
        mounted.current &&
        context?.navigation === navigationRef.current &&
        context.epoch === editEpoch.current
      ) {
        const guidance = importGuidance(error as ApiFailure, kind, conditions, prompt.data?.input_json_schema as Record<string, any> | undefined);
        setActionError(new Error(guidance.message));
        setImportRepair(guidance.repair);
      }
    },
  });
  const confirm = useMutation({
    onMutate: () => ({ navigation: navigationRef.current, epoch: editEpoch.current }),
    mutationFn: async ({ runAnalysis }: { runAnalysis: boolean }) => {
      const submittedEpoch = editEpoch.current;
      const submittedNavigation = navigationRef.current;
      const ownsConfirmation = () => mounted.current && submittedNavigation === navigationRef.current && submittedEpoch === editEpoch.current;
      if (runAnalysis && customBenchmark && !benchmark)
        throw new Error("请输入行业ETF代码，或选择仅查看股票。");
      if (!preview || !edited || json(edited) !== json(preview.document))
        throw new Error("资料已经修改，请先重新检查。");
      const values = {
        preview_id: preview.preview_id,
        ...(security ? { security_id: security } : {}),
        title,
        ...(revision ? { expected_version: revision.version } : {}),
        reviews: Object.values(reviews),
        revision_note: revisionNote,
        analyze: false,
      };
      const request = {
        ...values,
        request_id: commandId("confirm", values),
      } satisfies ConfirmInput;
      await persistDraft(storageKey, snapshot());
      const result = await api<components["schemas"]["EventConfirmOutput"]>("/events/confirm", "POST", request);
      const stillCurrent = ownsConfirmation();
      const target: PendingStart = {
        id: result.set_id,
        selectedVersion: result.version,
        origin: { analysisId: "", version: String(result.version), resultId: "" },
        draftSignature: researchDraftSignature(snapshot()),
        auto: runAnalysis && stillCurrent,
        ...(runAnalysis
          ? { request: analysisCommand(result.set_id, result.version) }
          : {}),
      };
      const destinationKey = `event-draft:${kind}:${result.set_id}`;
      try {
        await initializeDraft(destinationKey, {
          ...snapshot(), preview: null, edited: null, revision: null, pendingStart: null,
        });
        if (runAnalysis) {
          await patchDraft<ReturnType<typeof snapshot>>(destinationKey, stored => {
            const current = ownsConfirmation();
            const existing = stored.pendingStart?.request?.request_id;
            const mayReplace = current || !stored.pendingStart || existing === target.request?.request_id;
            return mayReplace ? { ...stored, pendingStart: { ...target, auto: current,
              preserveDraft: !current || target.preserveDraft } } : stored;
          });
          // Navigation can happen while IndexedDB commits the valid handoff.
          if (!ownsConfirmation()) await patchDraft<ReturnType<typeof snapshot>>(destinationKey, stored =>
            stored.pendingStart && stored.pendingStart.request?.request_id === target.request?.request_id
              ? { ...stored, pendingStart: { ...stored.pendingStart, auto: false, preserveDraft: true } } : stored);
        }
      } catch {
        if (mounted.current && submittedNavigation === navigationRef.current && submittedEpoch === editEpoch.current)
          setStorageWarning(new Error("资料已保存，但浏览器未能保存启动记录；请从已保存事件集继续。"));
      }
      return { result, runAnalysis, target };
    },
    onSuccess: ({ result, runAnalysis, target }, _input, context) => {
      void client.invalidateQueries({ queryKey: ["event-sets"] });
      void client.invalidateQueries({ queryKey: ["event-set", result.set_id] });
      if (!mounted.current) return;
      if (context?.navigation !== navigationRef.current || context.epoch !== editEpoch.current) {
        // The persisted destination draft retains recovery; a late receipt has
        // no authority to start or replace a command in the current workspace.
        notice({ text: "资料版本已保存，当前阅读位置已保留。" });
        return;
      }
      updateParams({
        set: result.set_id,
        version: String(result.version),
        a: undefined,
        result: undefined,
        new: undefined,
        draft: undefined,
        historical_years: String(historicalYears),
        years: researchYears.trim() || undefined,
        excluded_years: excludeYears.trim() || undefined,
        date_category: dateCategory ?? undefined,
        quarter: undefined,
      }, true);
      setImportOpen(false);
      setPreview(null);
      setEdited(null);
      setRevision(null);
      setActionError(null);
      notice({
        text: result.reused
          ? "已复用相同事件资料版本。"
          : "事件资料与人工核对记录已保存为独立版本。",
      });
      if (runAnalysis) {
        // The destination workspace owns the automatic start and every retry.
        autoStarted.current = false;
        if (result.set_id === setId) {
          // Explicitly submitted save-and-start owns this handoff, even though the
          // canonical target URL is changing within the same mounted workspace.
          startAnalysis(target);
        }
      }
    },
    onError: (error, _input, context) => {
      if (mounted.current && context?.navigation === navigationRef.current && context.epoch === editEpoch.current)
        setActionError(error);
    },
  });
  function startImport(revise = false) {
    const revisionTarget = revise && saved.id && saved.latest_version
      ? { set_id: saved.id, version: saved.latest_version } : null;
    if (revise && !revisionTarget) return;
    editEpoch.current++;
    setImportOpen(true);
    setImportRepair("");
    if (!revise) {
      setRevision(null);
      setPreview(null);
      setEdited(null);
      setReviews({});
      setPendingStart(null);
      setRevisionNote("");
      setActionError(null);
      updateParams({
        set: undefined,
        version: undefined,
        a: undefined,
        result: undefined,
        new: "1",
        draft: undefined,
      });
      return;
    }
    setPreview(null);
    setEdited(null);
    setReviews({});
    setActionError(null);
    setRevisionNote("");
    setRevision(revisionTarget);
    setText(revise ? saved.raw_text || json(saved.document) : "");
    setTitle(revise ? saved.title ?? "" : "");
    if (!revise) setConditions(eventPromptConditions(params, kind, promptLanguage));
    if (revise)
      setConditions(
        promptLanguage === "zh"
          ? `公司：${json(facts(saved.document).company)}。原查询范围：${json(facts(saved.document).scope)}。请补充或修订资料，保留完整查询覆盖。`
          : `Company: ${json(facts(saved.document).company)}. Original scope: ${json(facts(saved.document).scope)}. Complete or revise the facts while preserving the full requested coverage.`,
      );
  }
  function promptText() {
    return `${conditions.trim() ? `${promptLanguage === "zh" ? "用户已提供的条件" : "User supplied conditions"}：${conditions.trim()}\n\n` : ""}${prompt.data?.prompt ?? ""}`;
  }
  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(promptText());
      notice({
        text: "简洁提示词已复制。交给能联网的 AI；已有条件直接查询，仅补问缺失信息。",
      });
    } catch {
      notice({
        text: "浏览器没有允许复制，请在下方提示词中手动选择复制。",
        error: true,
      });
    }
  }
  function reviewEvent(
    id: string,
    key: keyof ReviewInput,
    value: boolean | string,
  ) {
    setReviews((previous) => ({
      ...previous,
      [id]: {
        ...previous[id],
        [key]: value,
        ...(key === "date_verified" && value === false
          ? { time_verified: false }
          : {}),
      },
    }));
  }
  const document = facts(edited ?? preview?.document);
  const isEarnings = document.schema_version === "iirp.earnings-events.v1";
  const altered = !!preview && json(edited) !== json(preview.document);
  const reviewReady = Object.values(reviews).some(
    (item) => item.selected && item.date_verified,
  );
  const excludedWithoutReason = Object.values(reviews).some(
    (item) => !item.selected && !item.note?.trim(),
  );
  const previousEvents = revision ? rows(facts(saved.document).events) : [];
  const removed = previousEvents.filter(
    (event) =>
      !rows(document.events).some(
        (item) =>
          item.client_event_id === event.client_event_id ||
          eventIdentity(item) === eventIdentity(event),
      ),
  );
  function difference(event: Fact) {
    const previous = previousEvents.find(
      (item) => item.client_event_id === event.client_event_id,
    );
    return !revision
      ? "新增候选"
      : previous
        ? JSON.stringify(previous) === JSON.stringify(event)
          ? "资料未变，核对标记需重新确认"
          : "资料有修改，需重新核对"
        : previousEvents.some(
              (item) => eventIdentity(item) === eventIdentity(event),
            )
          ? "疑似同一事件，导入标识有变化"
          : "新增候选";
  }
  const selectedResult = facts(analysis.data?.data);
  function createWithinImportedScope() {
    if (!scopeConflict || conditionsDirty || activePendingStart) return;
    const request = eventAnalysisForImportedScope(requestParams, importedScope);
    setRefreshError(null);
    startAnalysis({
      id: setId,
      selectedVersion: Number(requestParams.event_version),
      request: {
        ...request,
        request_id: commandId(`scope-migration:${setId}:${analysisId}`, request),
      } as EventAnalysisInput,
    });
  }
  function manageScopeSources() {
    const target = window.document.getElementById("event-source-management");
    if (target instanceof HTMLDetailsElement) target.open = true;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "start", behavior: "instant" });
  }
  function selectResultView(selection: { metric?: string; group?: string }) {
    if (!analysis.data || createAnalysis.isPending || scopeConflict) return;
    const values = facts(analysis.data.params);
    const selectedVersion = Number(values.event_version);
    const request = {
      ...Object.fromEntries(
        [
          "cutoff_date",
          "current_fiscal_year",
          "include_unverified",
          "historical_years",
          "common_years",
          "benchmark",
          "years",
          "excluded_years",
          "date_window",
          "date_category",
        ].map((key) => [key, values[key]]),
      ),
      version: selectedVersion,
      ...(selection.metric ? { date_window: selection.metric } : {}),
      ...(selection.group ? { date_category: selection.group } : {}),
    };
    startAnalysis({
      id: setId,
      selectedVersion,
      preserveDraft: conditionsDirty,
      request: {
        ...request,
        request_id: commandId(`analyze:${setId}`, request),
      } as EventAnalysisInput,
    });
  }
  const editCondition = (change: () => void) => {
    editEpoch.current++;
    setConditionsDirty(true);
    change();
  };
  const researchRangeControls = (quarters: unknown) => (
    <>
      <label>
        目标历史年数（完整导入范围仍保留）
        <input
          type="number"
          min="1"
          max="9997"
          value={historicalYears}
          onChange={(e) =>
            editCondition(() => setHistoricalYears(Number(e.target.value)))
          }
        />
      </label>
      {kind === "earnings" && (
        <label>
          <input
            type="checkbox"
            checked={commonYears}
            onChange={(e) =>
              editCondition(() => setCommonYears(e.target.checked))
            }
          />
          {eventCommonYearsLabel(quarters)}
        </label>
      )}
    </>
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <Link
            className="text-link"
            to={sourceHref(
              location.state,
              researchHref(kind === "earnings" ? "earnings" : "monthly"),
            )}
            state={location.state?.fromState}
          >
            ← 分析中心
          </Link>
          <h1>
            {kind === "earnings" ? "财报日期补录与分析" : "自定义事件分析"}
          </h1>
          <p>
            {kind === "earnings"
              ? "核对实际业绩发布日期，比较当天及前后各 5 个交易日。"
              : "比较发布会、开发者大会等真实事件前后的历年股价表现。"}
          </p>
        </div>
        <Button variant="primary" onClick={() => startImport()}>
          新建事件分析
        </Button>
      </div>
      <nav className="tabs" aria-label="事件资料类型">
        <Link
          className={kind === "custom" ? "active" : ""}
          to={modeHref("custom")}
        >
          自定义事件
        </Link>
        <Link
          className={kind === "earnings" ? "active" : ""}
          to={modeHref("earnings")}
        >
          财报日期
        </Link>
      </nav>
      <ErrorNotice error={storage.error} />
      <ErrorNotice error={storageWarning} />
      {activePendingStart && (
        <section className="panel panel-body">
          <p>
            资料 v{activePendingStart.selectedVersion}{" "}
            已保存。已有行情将复用，缺失部分自动获取。
          </p>
          <ErrorNotice
            error={
              activePendingStart.error
                ? new Error(
                    `${activePendingStart.error} 资料已安全保存在服务器，可继续启动。`,
                  )
                : actionError
            }
          />
          <Button
            variant="primary"
            busy={createAnalysis.isPending}
            onClick={() => startAnalysis(activePendingStart)}
          >
            继续启动分析
          </Button>
        </section>
      )}
      {pendingStart && !activePendingStart && (
        <details className="panel panel-body">
          <summary>之前条件的分析启动记录</summary>
          <p>资料 v{pendingStart.selectedVersion} 的启动记录已保留。当前研究与草稿不受影响；恢复将使用之前提交的条件。</p>
          <Button busy={createAnalysis.isPending} onClick={() => startAnalysis({ ...pendingStart, preserveDraft: true })}>
            恢复之前的分析启动
          </Button>
        </details>
      )}
      {conditionsDirty && analysisId && (
        <p className="notice notice-warning">
          研究条件有未应用改动，草稿已保留。下方结果仍使用该分析保存时的条件；应用改动请生成新分析。
        </p>
      )}
      {params.has("draft") && (
        <p className="notice">
          已为这份跨类型资料建立独立草稿。
          <Link to={`/analysis/events?kind=${kind}&new=1`}>
            返回本类型原有草稿
          </Link>
        </p>
      )}
      {analysisId && (
        <>
          <ResearchReadStatus error={analysis.error} retry={analysis.refetch}
            taskError={task.error} retryTask={task.refetch}
            pending={analysis.isPending} label="正在读取事件分析与逐项进度…" />
          {analysis.data && (
            <>
              {saved.id && (
                <section className="panel panel-body" aria-label="当前事件研究">
                  <h2>
                    {display(facts(saved.security).symbol)} · {saved.title}
                  </h2>
                  <p className="context-baseline">
                    资料范围 {saved.kind === "earnings" ? "FY " : ""}
                    {display(
                      facts(facts(saved.document).scope)[
                        saved.kind === "earnings"
                          ? "fiscal_year_start"
                          : "year_start"
                      ],
                    )}
                    —
                    {display(
                      facts(facts(saved.document).scope)[
                        saved.kind === "earnings"
                          ? "fiscal_year_end"
                          : "year_end"
                      ],
                    )}{" "}
                    · {saved.event_count} 次事件 · 当前结果截至{" "}
                    {display(
                      analysis.data.result_cutoff ??
                        facts(selectedResult.metadata).cutoff_date,
                    )}
                  </p>
                  <p>
                    {scopeConflict && <><strong>已有结果 · 更新需选择范围</strong> · </>}
                    研究目标截止{" "}
                    {display(facts(analysis.data.params).cutoff_date)}
                    {freshness.latest_completed_session && (
                      <>
                        {" "}
                        · 最近已完成交易日 {freshness.latest_completed_session}
                      </>
                    )}
                  </p>
                  <p className="muted">
                    {scopeConflict
                      ? "当前保存条件不能直接刷新；请先选择更新范围，原有结果不会改变。"
                      : "行情为 24 小时缓存：期间重复打开不再下载，过期后打开会自动重新获取；事件事实仍固定于所选资料版本。"}
                  </p>
                  {scopeConflict && (
                    <div className="notice notice-warning" role="status">
                      <p>旧研究指定年份超出事件资料 v{requestParams.event_version} 的范围 {scopeConflict.start}—{scopeConflict.end}：{scopeConflict.outsideYears.join("、")}。</p>
                      <p>旧记录无法区分默认展开与明确选年，不会自动缩小目标。自动更新已暂停；旧结果、固定链接与导出仍可读。按资料范围新建会使用最新已完成交易日，并保留事件版本、窗口、基准及排除条件。</p>
                      <div className="button-row">
                        <Button variant="primary" busy={createAnalysis.isPending} disabled={conditionsDirty || !!activePendingStart} onClick={createWithinImportedScope}>
                          以资料范围 {scopeConflict.start}—{scopeConflict.end} 新建并跟随最新
                        </Button>
                        <Button onClick={manageScopeSources}>保留目标并扩展事件资料</Button>
                      </div>
                    </div>
                  )}
                  <ResearchVersionActions busy={refreshing}
                    disabled={conditionsDirty || !!activePendingStart || !!scopeConflict}
                    refreshLabel="重新获取"
                    refresh={() => { void refresher.current?.ensure(true); }}>
                    <span role="status" aria-live="polite">
                      {scopeConflict
                        ? "需先选择更新范围，已有结果仍可读。"
                        : refreshing
                        ? "正在重新获取，已有结果仍可读…"
                        : freshness.refresh_checked_at
                          ? `最近检查 ${formatTime(freshness.refresh_checked_at, "America/New_York")}`
                          : "尚无本次更新检查记录"}
                    </span>
                  </ResearchVersionActions>
                  <p className="muted">
                    行情获取于{" "}
                    {freshness.price_fetched_at
                      ? formatTime(freshness.price_fetched_at, "America/New_York")
                      : "—"}
                    ，有效至{" "}
                    {freshness.price_expires_at
                      ? formatTime(freshness.price_expires_at, "America/New_York")
                      : "—"}{" "}
                    · 盘中报价时间见顶部，不能替代已完成日线。
                  </p>
                  <ErrorNotice
                    error={refreshError}
                    retry={() => {
                      void refresher.current?.ensure(true);
                    }}
                  />
                </section>
              )}
              <details className="panel" open={!analysis.data.result_id}>
                <summary className="section-heading">
                  <h2>准备任务 · {eventLabel(analysis.data.status)}</h2>
                  <span>{analysis.data.result_id ? "已有结果可读 · " : ""}查看覆盖、等待原因与任务控制</span>
                </summary>
                {rows(analysis.data.progress).map((item, index) => (
                  <div className="panel-body" key={index}>
                    <p>
                      {display(item.symbol)} · 任务 {eventLabel(item.status)} ·{" "}
                      {item.ready_events == null || item.event_count == null
                        ? "覆盖未知：旧版本未记录目标覆盖，不能据此判断存在缺口"
                        : `已就绪 ${item.ready_events} / ${item.event_count} 次事件`}
                    </p>
                    {item.wait_reason && (
                      <p className="reason">{item.wait_reason}</p>
                    )}
                    {rows(item.required_ranges).map((range, i) => (
                      <small key={i}>
                        所需实际日期范围：{range.start_date} → {range.end_date}
                      </small>
                    ))}
                  </div>
                ))}
                {analysis.data.batch_id && (
                  <details className="result-notes">
                    <summary>查看任务、暂停与恢复</summary>
                    <BatchPanel id={analysis.data.batch_id} />
                  </details>
                )}
              </details>
              {analysis.data.result_id && (
                <div className="analysis-form">
                  <label>
                    结果（24 小时内有效）
                    <select
                      value={resultId}
                      onChange={(e) =>
                        updateParams({ result: e.target.value || undefined }, true)
                      }
                    >
                      <option value="">跟随最新 · 当前可用结果</option>
                      {rows(analysis.data.results).map((item) => (
                        <option key={item.id} value={item.id}>
                          {formatTime(item.created_at, "America/New_York")} ·{" "}
                          {String(item.id).slice(0, 8)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <span>
                    结果 {String(analysis.data.result_id).slice(0, 8)} ·
                    美东时间
                  </span>
                </div>
              )}
              {analysis.data.data ? (
                <EventDatesResult
                  key={originId}
                  readingScope={originId}
                  analysisId={analysisId}
                  resultId={analysis.data.result_id ?? undefined}
                  data={selectedResult}
                  busy={createAnalysis.isPending}
                  onSelect={scopeConflict ? undefined : selectResultView}
                  csvUrl={`/api/v1/events/analyses/${encodeURIComponent(analysisId)}/export?${new URLSearchParams({ result_id: analysis.data.result_id!, format: "csv" })}`}
                  jsonUrl={`/api/v1/events/analyses/${encodeURIComponent(analysisId)}/export?${new URLSearchParams({ result_id: analysis.data.result_id!, format: "json" })}`}
                />
              ) : (
                <EmptyState
                  title="正在准备可核对的事件窗口"
                  description="后台会复用已有行情并补齐缺口；身份或日期需要核对时，具体原因显示在任务中。"
                />
              )}
            </>
          )}
        </>
      )}
      <details className="panel">
        <summary className="section-heading">最近研究 / 已保存事件集</summary>
        <div className="section-heading">
          <h2>已保存事件集</h2>
          <span>{rows(sets.data?.items).length} 个</span>
        </div>
        <ErrorNotice error={sets.error} retry={sets.refetch} />
        {sets.isPending ? (
          <Loading />
        ) : rows(sets.data?.items).length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>事件集 / 证券</th>
                  <th>资料查询截至</th>
                  <th>事件核对</th>
                  <th>版本</th>
                </tr>
              </thead>
              <tbody>
                {rows(sets.data?.items).map((item) => (
                  <tr key={item.id}>
                    <td>
                      <Link
                        className="text-link"
                        to={`/analysis/events?${new URLSearchParams({ kind: item.kind, set: item.id })}`}
                        onClick={() => setImportOpen(false)}
                      >
                        {item.title} · {display(facts(item.security).symbol)}
                      </Link>
                    </td>
                    <td>{display(item.research_as_of)}</td>
                    <td>
                      已核对 {display(item.verified_count)} / 已选择{" "}
                      {display(item.selected_count)} / 共{" "}
                      {display(item.event_count)} 次
                    </td>
                    <td>v{item.version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="先建立一份有来源的事件日期资料"
            description="复制提示词给 AI → 粘贴并核对 JSON → 保存并查看分析。"
          />
        )}
      </details>
      {importOpen && (
        <section className="panel event-import">
          <fieldset
            className="event-import-fields"
            disabled={confirm.isPending}
          >
            <div className="section-heading">
              <h2>{revision ? "更新事件资料" : "新建事件资料"}</h2>
              <Button variant="ghost" onClick={() => setImportOpen(false)}>
                收起导入
              </Button>
            </div>
            <details className="result-notes">
              <summary>1. 复制 AI 查询提示词</summary>
              <p>
                只需目标范围内的日期、支持来源和一句说明。公司与范围只写一次，
                无需财报全文或内部编号。仅缺条件时 AI 才补问；这里不需要配置 AI
                API。
              </p>
              <label>
                AI 沟通语言
                <select aria-label="AI 沟通语言" value={promptLanguage} onChange={(e) => {
                  const next = e.target.value as "zh" | "en";
                  setConditions((current) => eventConditionsForLanguage(current, params, kind, promptLanguage, next));
                  setPromptLanguage(next);
                }}>
                  <option value="zh">中文</option>
                  <option value="en">English</option>
                </select>
              </label>
              <label>
                已明确的条件（可留空）
                <textarea
                  rows={2}
                  value={conditions}
                  onChange={(e) => setConditions(e.target.value)}
                  placeholder={
                    kind === "earnings"
                      ? "例如：Apple，2018—2025 财年，所有季度"
                      : "例如：Apple 发布会，想比较 2018—2025 年"
                  }
                />
              </label>
              <ErrorNotice error={prompt.error} retry={prompt.refetch} />
              <div className="button-row">
                <Button
                  disabled={!prompt.data?.prompt}
                  onClick={() => void copyPrompt()}
                >
                  复制 AI 查询提示词
                </Button>
              </div>
              <textarea
                className="prompt-preview"
                aria-label="AI 查询提示词全文"
                readOnly
                rows={6}
                value={prompt.data?.prompt ? promptText() : "正在读取提示词…"}
              />
              <details>
                <summary>高级格式说明（原完整 JSON 仍兼容）</summary>
                <p>
                  默认使用简洁模板；来源的支持项须逐条有依据，所有事实仍待人工核对。
                </p>
                <details>
                  <summary>简洁输入完整字段</summary>
                  <pre>{json(prompt.data?.input_json_schema)}</pre>
                </details>
                <details>
                  <summary>原完整 JSON 格式</summary>
                  <pre>{json(prompt.data?.json_schema)}</pre>
                </details>
              </details>
            </details>
            <div className="panel-body">
              <label>
                2. 粘贴 AI 返回的 JSON
                <textarea
                  rows={preview ? 4 : 10}
                  aria-label="AI 返回的 JSON"
                  value={text}
                  disabled={!storage.ready}
                  onChange={(e) => {
                    editEpoch.current++;
                    setText(e.target.value);
                    setPreview(null);
                    setEdited(null);
                    setActionError(null);
                    setImportRepair("");
                  }}
                  placeholder="粘贴简洁 JSON 或原完整 JSON；未知日期和财期留空，不补 01-01 或 00:00。"
                />
              </label>
              <ErrorNotice error={actionError} />
              {importRepair && <Button variant="ghost" onClick={() => void navigator.clipboard.writeText(importRepair).then(() => notice({text: "修正说明已复制；原始 JSON 已保留。"})).catch(() => notice({text: "复制失败，请检查浏览器剪贴板权限。", error: true}))}>复制给 AI 的修正说明</Button>}
              <ErrorNotice error={limits.error} retry={limits.refetch} />
              {limits.data && (
                <details className="result-notes">
                  <summary>
                    已输入 {Array.from(text).length.toLocaleString()} 字符 ·
                    UTF-8{" "}
                    {new TextEncoder().encode(text).length.toLocaleString()}{" "}
                    字节 · 草稿自动保存
                  </summary>
                  <p>
                    最多 {limits.data.text_characters.toLocaleString()} 字符 /{" "}
                    {limits.data.text_utf8_bytes.toLocaleString()} UTF-8
                    字节；预览、修订和确认每次最多{" "}
                    {limits.data.request_bytes.toLocaleString()} 请求字节。
                    {limits.data.explanation}
                  </p>
                  <p>
                    支持八年以上完整资料，请保留来源与备注。超限时先检查是否重复粘贴整份文档。
                  </p>
                </details>
              )}
              <Button
                variant="primary"
                busy={previewMutation.isPending}
                disabled={!text.trim() || !storage.ready}
                onClick={() => previewMutation.mutate(text)}
              >
                检查资料并继续
              </Button>
              <p className="muted">
                检查资料 → 核对事实 → 获取缺失行情并分析。来源与备注完整保留。
              </p>
            </div>
            {preview && (
              <>
                <div className="section-heading">
                  <div>
                    <h3>3. 核对事实，再保存版本</h3>
                    <p>
                      {display(facts(document.company).name)} ·{" "}
                      {display(facts(document.company).ticker)} · 资料查询截至{" "}
                      {display(document.research_as_of)}
                    </p>
                    <p>
                      {display(
                        facts(document.scope).fiscal_year_start ??
                          facts(document.scope).year_start,
                      )}
                      —
                      {display(
                        facts(document.scope).fiscal_year_end ??
                          facts(document.scope).year_end,
                      )}{" "}
                      · {rows(document.events).length} 次事件 ·{" "}
                      {
                        Object.values(reviews).filter(
                          (item) => item.selected && !item.date_verified,
                        ).length
                      }{" "}
                      次日期待核对
                    </p>
                  </div>
                </div>
                <div className="panel-body">
                  <p className="reason">
                    AI 资料先作为候选。日期、实际时刻、财年财季、财期类型和发生状态分别核对；所有核对标记默认关闭，来源链接不等于已核验。
                  </p>
                  {kind === "custom" && (rows(facts(document.scope).include_keywords).length > 0 || rows(facts(document.scope).exclude_keywords).length > 0) && <p className="reason">
                    纳入关键词仅指导 AI 查找候选；排除关键词命中事件名称时，保存前必须取消选入并填写理由。其他候选仍须逐项核对，原始资料保留。
                  </p>}
                  {kind === "earnings" && <p className="reason">
                    请求财季：{formatRequestedQuarters(facts(document.scope).fiscal_quarters)}。常规财季历史汇总需要已核对公告日、财年财季及有来源支持的财期类型；不能从 Q2 字样推断“常规”。缺少 period_kind 时逐次日期价格仍可读，但不计入常规财季汇总 N。精确公告反应还需已核对的首次公开时刻，电话会或官方日程不代替实际时刻。
                  </p>}
                  <details className="result-notes">
                    <summary>查看资料检查说明与缺口</summary>
                    {(Array.isArray(preview.warnings)
                      ? preview.warnings
                      : []
                    ).map((warning: string, index: number) => (
                      <p className="muted" key={index}>
                        {warning}
                      </p>
                    ))}
                  </details>
                  {removed.length > 0 && (
                    <p className="reason">
                      本次资料未包含旧版本的 {removed.length}{" "}
                      次事件。旧版本继续保留，请在修订说明中记录原因。
                    </p>
                  )}
                  <div className="analysis-form">
                    <label>
                      事件集名称
                      <input
                        value={title}
                        maxLength={200}
                        onChange={(e) => setTitle(e.target.value)}
                      />
                    </label>
                    <label>
                      对应证券
                      <select
                        value={security}
                        onChange={(e) => setSecurity(e.target.value)}
                      >
                        <option value="">
                          {rows(preview.candidates).length
                            ? "请选择经核对的证券"
                            : "本地尚无候选，保存后核对身份"}
                        </option>
                        {rows(preview.candidates).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.symbol} · {display(item.name)} ·{" "}
                            {display(item.exchange)} · {display(item.currency)}{" "}
                            · {eventLabel(item.status)}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                </div>
                <div className="panel-body event-start-actions">
                  {altered && (
                    <div className="notice notice-warning">
                      事件资料已修改，重新检查通过后再逐项核对。
                      <Button
                        busy={previewMutation.isPending}
                        onClick={() => previewMutation.mutate(json(document))}
                      >
                        应用修改并重新检查
                      </Button>
                    </div>
                  )}
                  <label>
                    {revision ? "本次修订说明（必填）" : "保存说明（可选）"}
                    <textarea
                      rows={2}
                      value={revisionNote}
                      onChange={(e) => setRevisionNote(e.target.value)}
                    />
                  </label>
                  {isEarnings && (
                    <label>
                      当前财年（核对公司口径后填写，未知留空）
                      <input
                        type="number"
                        min="1"
                        max="9998"
                        value={fiscalYear}
                        onChange={(e) =>
                          editCondition(() => setFiscalYear(e.target.value))
                        }
                      />
                      <small>
                        未知当前财年时保留日期观察，不冒充历史财年汇总。
                      </small>
                    </label>
                  )}
                  <div className="analysis-form">
                    <BenchmarkPicker
                      value={benchmark}
                      custom={customBenchmark}
                      onChange={(value, custom) =>
                        editCondition(() => {
                          setBenchmark(value);
                          setCustomBenchmark(custom);
                          setActionError(null);
                        })
                      }
                    />
                    {researchRangeControls(facts(document.scope).fiscal_quarters)}
                  </div>
                  <ErrorNotice error={actionError} />
                  <p className="muted">
                    先复用已有行情，自动补齐缺口。核对标记仅来自你的逐项确认。
                  </p>
                  <div className="button-row">
                    <Button
                      variant="primary"
                      busy={confirm.isPending || createAnalysis.isPending}
                      disabled={
                        altered ||
                        !reviewReady ||
                        excludedWithoutReason ||
                        (!!revision && !revisionNote.trim())
                      }
                      onClick={() => confirm.mutate({ runAnalysis: true })}
                    >
                      开始获取行情并分析
                    </Button>
                    <Button
                      busy={confirm.isPending}
                      disabled={
                        altered ||
                        excludedWithoutReason ||
                        (!!revision && !revisionNote.trim())
                      }
                      onClick={() => confirm.mutate({ runAnalysis: false })}
                    >
                      仅保存资料版本
                    </Button>
                  </div>
                  {excludedWithoutReason && (
                    <p className="reason">
                      请为每条未选入分析的事件填写排除原因。
                    </p>
                  )}
                  {!reviewReady && (
                    <p className="muted">
                      至少核对一条选中事件的日期后可开始分析。待核对资料可以先保存。
                    </p>
                  )}
                </div>
                <details className="result-notes">
                  <summary>
                    核对日期、财期与来源（{rows(document.events).length}{" "}
                    次事件）
                  </summary>
                  {rows(document.events).map((event, index) => {
                    const review = reviews[event.client_event_id];
                    const supports = rows(event.sources).flatMap((source) =>
                      Array.isArray(source.supports) ? source.supports : [],
                    );
                    const dateAllowed =
                      !!event.event_date &&
                      event.date_status === "supported" &&
                      supports.includes("event_date");
                    const timeAllowed =
                      review?.date_verified &&
                      event.event_status === "occurred" &&
                      ((!!event.event_time &&
                        event.time_basis === "reported_actual") ||
                        (isEarnings &&
                          !event.event_time &&
                          event.release_session !== "unknown" &&
                          supports.includes("release_session")));
                    const periodAllowed =
                      isEarnings &&
                      !!event.fiscal_year &&
                      !!event.fiscal_quarter &&
                      supports.includes("fiscal_year") &&
                      supports.includes("fiscal_quarter");
                    return (
                      <article
                        className="event-review-row"
                        key={event.client_event_id}
                      >
                        <div className="panel-body">
                          <h4>
                            {index + 1}. {event.event_name}
                          </h4>
                          <p>
                            {eventLabel(event.event_type)} ·{" "}
                            {display(event.event_date)}
                            {event.event_time
                              ? ` ${event.event_time} ${event.timezone}`
                              : isEarnings &&
                                  event.release_session &&
                                  event.release_session !== "unknown"
                                ? ` · ${eventLabel(event.release_session)}（分钟未知）`
                                : " · 时间未知，按日期观察"}{" "}
                            · {eventLabel(event.event_status)}
                          </p>
                          {isEarnings && (
                            <p>
                              FY{display(event.fiscal_year)} Q
                              {display(event.fiscal_quarter)} · 期末{" "}
                              {display(event.period_end)} ·{" "}
                              {eventLabel(event.period_kind)}
                            </p>
                          )}
                          <p className="muted">
                            {difference(event)} · {eventLabel(event.time_basis)}
                          </p>
                          <EventDateSources sources={event.sources} />
                          {event.notes && (
                            <p className="notice">{event.notes}</p>
                          )}
                          <div className="ticker-checkboxes">
                            <label>
                              <input
                                type="checkbox"
                                checked={review?.selected ?? false}
                                onChange={(e) =>
                                  reviewEvent(
                                    event.client_event_id,
                                    "selected",
                                    e.target.checked,
                                  )
                                }
                              />
                              选入本版本分析
                            </label>
                            <label>
                              <input
                                type="checkbox"
                                disabled={!dateAllowed || altered}
                                checked={review?.date_verified ?? false}
                                onChange={(e) =>
                                  reviewEvent(
                                    event.client_event_id,
                                    "date_verified",
                                    e.target.checked,
                                  )
                                }
                              />
                              已核对事件日期
                            </label>
                            <label>
                              <input
                                type="checkbox"
                                disabled={!timeAllowed || altered}
                                checked={review?.time_verified ?? false}
                                onChange={(e) =>
                                  reviewEvent(
                                    event.client_event_id,
                                    "time_verified",
                                    e.target.checked,
                                  )
                                }
                              />
                              已核对实际时刻 / 公告时段
                            </label>
                            {isEarnings && (
                              <label>
                                <input
                                  type="checkbox"
                                  disabled={!periodAllowed || altered}
                                  checked={review?.period_verified ?? false}
                                  onChange={(e) =>
                                    reviewEvent(
                                      event.client_event_id,
                                      "period_verified",
                                      e.target.checked,
                                    )
                                  }
                                />
                                已核对财年与财季
                              </label>
                            )}
                          </div>
                          {event.time_basis === "official_schedule" && (
                            <p className="muted">
                              官方日程时刻可保留，但不能确认成实际开始分钟。
                            </p>
                          )}
                          <label>
                            核对备注 / 排除原因
                            <textarea
                              rows={2}
                              required={review?.selected === false}
                              value={review?.note ?? ""}
                              onChange={(e) =>
                                reviewEvent(
                                  event.client_event_id,
                                  "note",
                                  e.target.value,
                                )
                              }
                              placeholder={
                                review?.selected
                                  ? "可记录核对了哪份资料"
                                  : "请说明为何不纳入，保留排除记录"
                              }
                            />
                          </label>
                        </div>
                        <EventEditor
                          event={event}
                          earnings={isEarnings}
                          onChange={(updated) => {
                            editEpoch.current++;
                            setEdited({
                              ...document,
                              events: rows(document.events).map((item, i) =>
                                i === index ? updated : item,
                              ),
                            });
                          }}
                        />
                      </article>
                    );
                  })}
                </details>
                <CoverageTable document={document} />
              </>
            )}
          </fieldset>
        </section>
      )}
      {setId && (
        <>
          <ErrorNotice error={current.error} retry={current.refetch} />
          {current.isPending ? (
            <Loading />
          ) : (
            current.data && (
              <details
                className="panel"
                id="event-source-management"
                tabIndex={-1}
              >
                <summary className="section-heading">
                  管理日期与来源 · 资料 v{saved.version}
                </summary>
                <div className="section-heading">
                  <div>
                    <h2>
                      {saved.title} · {display(facts(saved.security).symbol)}
                    </h2>
                    <p>
                      事件资料 v{saved.version} · 查询截至{" "}
                      {saved.research_as_of} · 已核对 {saved.verified_count} /{" "}
                      {saved.event_count} 次
                    </p>
                  </div>
                  <Button
                    disabled={saved.version !== saved.latest_version}
                    onClick={() => startImport(true)}
                  >
                    更新事件资料 / 重新导入
                  </Button>
                </div>
                <div className="analysis-form">
                  <label>
                    事件版本
                    <select
                      value={saved.version}
                      onChange={(e) =>
                        updateParams({
                          version: e.target.value,
                          a: undefined,
                          result: undefined,
                        })
                      }
                    >
                      {rows(saved.versions).map((item) => (
                        <option key={item.version} value={item.version}>
                          v{item.version} ·{" "}
                          {formatTime(item.created_at, "America/New_York")}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    行情截止交易日（留空取最新已完成交易日）
                    <input
                      type="date"
                      value={cutoff}
                      onChange={(e) =>
                        editCondition(() => setCutoff(e.target.value))
                      }
                    />
                  </label>
                  <BenchmarkPicker
                    value={benchmark}
                    custom={customBenchmark}
                    onChange={(value, custom) =>
                      editCondition(() => {
                        setBenchmark(value);
                        setCustomBenchmark(custom);
                        setActionError(null);
                      })
                    }
                  />
                  {researchRangeControls(facts(facts(saved.document).scope).fiscal_quarters)}
                  <label>
                    指定历史年份（留空使用已导入范围；需要8年时先导入并核对8年资料）
                    <input
                      value={researchYears}
                      onChange={(e) =>
                        editCondition(() => setResearchYears(e.target.value))
                      }
                      placeholder="例如 2018,2019,2020"
                    />
                  </label>
                  <label>
                    排除历史年份
                    <input
                      value={excludeYears}
                      onChange={(e) =>
                        editCondition(() => setExcludeYears(e.target.value))
                      }
                      placeholder="留空不排除"
                    />
                  </label>
                  {saved.kind === "earnings" && (
                    <label>
                      当前财年（未知留空）
                      <input
                        type="number"
                        min="1"
                        max="9998"
                        value={fiscalYear}
                        onChange={(e) =>
                          editCondition(() => setFiscalYear(e.target.value))
                        }
                      />
                    </label>
                  )}
                  <label>
                    <input
                      type="checkbox"
                      checked={includeUnverified}
                      onChange={(e) =>
                        editCondition(() =>
                          setIncludeUnverified(e.target.checked),
                        )
                      }
                    />
                    包含未核对日期观察，仍不计入默认汇总
                  </label>
                  <Button
                    variant="primary"
                    busy={createAnalysis.isPending}
                    onClick={() => {
                      if (!saved.id || !saved.version) return;
                      startAnalysis({ id: saved.id, selectedVersion: saved.version });
                    }}
                  >
                    更新行情并生成新分析
                  </Button>
                </div>
                <ErrorNotice error={actionError} />
                {saved.version !== saved.latest_version && (
                  <p className="panel-body reason">
                    正在查看旧事件版本，旧结果保持原口径。需要修订资料时请先切到最新版本
                    v{saved.latest_version}。
                  </p>
                )}
                <CoverageTable document={facts(saved.document)} />
                <details className="result-notes">
                  <summary>查看已保存事件、来源与人工核对记录</summary>
                  {rows(saved.events).map((event) => (
                    <div
                      className="panel-body"
                      key={event.client_event_id}
                      id={`event-source-${event.client_event_id}`}
                      tabIndex={-1}
                    >
                      <h4>
                        {event.event_name} · {display(event.event_date)}{" "}
                        {display(event.event_time)}
                      </h4>
                      <p>
                        {event.date_verified ? "日期已核对" : "日期待核对"} ·{" "}
                        {event.time_verified
                          ? "实际时刻已核对"
                          : "实际时刻未核对"}
                        {event.excluded ? " · 已排除" : ""}
                      </p>
                      <p>{display(facts(event.review).note)}</p>
                      {reviewTarget === event.client_event_id && (
                        <Button
                          variant="ghost"
                          disabled={saved.version !== saved.latest_version}
                          onClick={() => {
                            startImport(true);
                            window.requestAnimationFrame(() => {
                              const input =
                                window.document.querySelector<HTMLTextAreaElement>(
                                  "textarea[aria-label='AI 返回的 JSON']",
                                );
                              input?.focus({ preventScroll: true });
                              input?.scrollIntoView({
                                block: "center",
                                behavior: "instant",
                              });
                            });
                          }}
                        >
                          修订资料与核对记录
                        </Button>
                      )}
                      <EventDateSources sources={event.sources} />
                      {event.notes && <p className="notice">{event.notes}</p>}
                    </div>
                  ))}
                </details>
                {rows(saved.analyses).length > 0 && (
                  <details className="result-notes">
                    <summary>已保存分析版本</summary>
                    {rows(saved.analyses).map((item) => (
                      <p key={item.id}>
                        <Button
                          variant="ghost"
                          onClick={() =>
                            updateParams({
                              a: item.id,
                              version: String(facts(item.params).event_version),
                              result: undefined,
                            })
                          }
                        >
                          事件 v{display(facts(item.params).event_version)} ·
                          行情截至 {display(facts(item.params).cutoff_date)} ·{" "}
                          {formatTime(item.created_at, "America/New_York")}
                        </Button>
                      </p>
                    ))}
                  </details>
                )}
              </details>
            )
          )}
        </>
      )}
    </>
  );
}
