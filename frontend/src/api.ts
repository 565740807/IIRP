import { isFrozenEntityQuery } from "./taskPresentation";
import { isFrozenResearchQuery, mutableResearchForBatch } from "./queryIdentity";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import type { components } from "./generated/api";
export type Job = components["schemas"]["JobSummary"];
export type JobDetail = components["schemas"]["JobDetailView"];
export type JobsResponse = components["schemas"]["JobsResponse"];
export type Worker = components["schemas"]["WorkerView"];
export type Batch = components["schemas"]["BatchView"];
export type CollectionInput = components["schemas"]["CollectionInput"];
export type CollectionOutput = components["schemas"]["CollectionOutput"];
export type AnalysisInput = components["schemas"]["AnalysisInput"];
export type AnalysisRefreshOutput = components["schemas"]["AnalysisRefreshOutput"];
export type AnalysisOutput = components["schemas"]["AnalysisOutput"];
export type AnalysisItem = components["schemas"]["AnalysisItem"];
// Flexible fact payloads are defined by the generated OpenAPI dictionary, never a second finance model.
export type Fact = {
  [K in keyof NonNullable<components["schemas"]["GenericOutput"]["data"]>]: any;
};
export type GenericOutput = Omit<
  components["schemas"]["GenericOutput"],
  "data" | "items"
> & { data: Fact; items: Fact[] };
export type StrategyInput = components["schemas"]["StrategyInput"];
export type PreferenceInput = components["schemas"]["PreferenceInput"];
export type ImportInput = components["schemas"]["ImportInput"];
export type ImportOutput = components["schemas"]["ImportOutput"];
export type EventCorrection = components["schemas"]["EventCorrection"];
export type TransactionSecurityInput =
  components["schemas"]["TransactionSecurityInput"];
export type RequestIdentity = components["schemas"]["RequestIdentity"];
export type System = Fact;
export type ApiFailure = Error & { details?: unknown; status?: number };
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const serializedBody = body === undefined ? undefined : JSON.stringify(body);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(method !== "GET" ? { "X-IIRP-Client": "web" } : {}),
      },
      body: serializedBody,
      // Aborting a read has no relationship to durable business task controls.
      signal: method === "GET" ? signal : undefined,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new Error("未能收到本地服务响应，请确认服务和连接正常后重试。");
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务暂未返回可读取的数据，请检查本地服务后重试。");
  }
  if (!response.ok) {
    const detail = (data as { detail?: unknown })?.detail;
    const error = new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((x) => x.msg ?? "输入无效").join("；")
          : `请求失败（${response.status}），请检查输入后重试。`,
    ) as ApiFailure;
    error.details = detail;
    error.status = response.status;
    throw error;
  }
  return data as T;
}
export const requestId = () => crypto.randomUUID();
export const activeStatuses = [
  "QUEUED",
  "RUNNING",
  "PAUSE_REQUESTED",
  "CANCEL_REQUESTED",
  "RETRY_WAIT",
  "WAITING",
];
export const statusLabels: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "正在运行",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  PAUSE_REQUESTED: "正在暂停",
  PAUSED: "已暂停",
  CANCEL_REQUESTED: "正在取消",
  CANCELLED: "已取消",
  RETRY_WAIT: "等待重试",
  WAITING: "等待依赖",
  COMPLETE: "完整",
  READY: "可用",
  NOT_FETCHED: "尚未获取",
};
export const sourceLabels: Record<string, string> = {
  unassigned_earnings: "财期归属待核对",
  wwdc_keynote: "WWDC 主题演讲",
  developer_keynote: "开发者大会主题演讲",
  earnings_release: "业绩发布",
  iphone_launch: "iPhone 发布会",
  mac_launch: "Mac 发布会",
  product_launch: "产品发布会",
  sec_issuer_identity_unconfirmed: "SEC 公司身份尚待核对",
  fiscal_year_end_unconfirmed: "公司财年结束月份尚未核对，不能确定完整历史财年",
  conflicting_release_observations: "公告日期或财期存在冲突，需核对来源后采用",
  earnings_evidence_incomplete: "财报来源覆盖尚不完整，已确认的样本仍可读取",
  earnings_source_checkpoint_missing: "财报来源进度缺失，可重试获取",
  candidate_requested_range_not_closed: "目标范围的公告候选尚未收齐",
  security_identity_or_calendar_unconfirmed: "证券身份或交易日历尚待核对",
  ...statusLabels,
  NOT_CONFIGURED: "未配置",
  CONFIGURED: "已配置",
  AVAILABLE: "可用",
  OK: "已验证",
  VERIFIED: "已验证",
  DATE_VERIFIED: "财期与日期已核对，时间待核对",
  CANDIDATE: "候选日期待核对",
  CONFLICT: "存在冲突，待核对",
  AUXILIARY: "辅助公告",
  manual_review: "人工核对",
  LOCAL: "本地可用",
  UNSUPPORTED: "不支持",
  PENDING: "待验证",
  MISSING: "缺失",
  PROBE_ONLY: "仅少量探测",
  UNVERIFIED: "待验证",
  NEEDS_CONFIG: "未配置",
  AVAILABLE_FOR_PROBE: "可做小样验证",
  SAMPLE_ONLY: "仅测试采样",
  SPLIT_ONLY: "仅拆股调整，不含股息再投资",
  UNVERIFIED_PROVIDER_RECORDS: "来源口径待核验",
  NOT_MATURE: "窗口尚未成熟",
  NOT_OCCURRED: "尚未发生",
  EXCLUDED: "已排除",
  date_only: "只有日期",
  exact: "精确时间",
  before_open: "盘前",
  after_close: "盘后",
  intraday: "盘中",
  conflict: "时间冲突",
  VALIDATED: "已验证",
  CONFIRMED: "已核对",
  DAILY: "最近日线",
  CLOSED: "已收盘",
  CURRENT: "现行事实",
  IDENTITY_PENDING: "证券身份待核对",
  LOCAL_OBSERVATIONS: "本地已解析观察",
  UNCONFIRMED: "关系待核对",
  UNCONFIRMED_DUPLICATE: "疑似重复待核对",
  intraday_observation: "盘中披露观察",
  available: "可用",
  no_session_yet: "尚无交易日",
  unconfirmed_event_time: "公告时间待核对",
  unverified_event: "事件待核对",
  current_fiscal_year: "当前财年单独对照",
  current_fiscal_year_unconfirmed: "当前财年尚待核对",
  same_fiscal_quarter: "同一财季",
  split_only: "仅拆股调整，不含股息再投资",
  same_progress: "截至同期",
  complete: "完整历史范围",
  in_progress: "进行中",
  incomplete_path: "路径不完整",
  missing_acceptance_time: "缺少接受时间",
  missing_baseline: "缺少基准价格",
  missing_event: "缺少事件",
  missing_event_date: "缺少公告日期",
  missing_price: "缺少价格",
  non_session_carry: "休市日，沿用前收",
  missing_window: "缺少可配对的实际日期窗口",
  before_listing: "早于上市或成立日期",
  benchmark_missing: "尚未取得基准资料",
  benchmark_missing_prices: "基准在相同日期窗口内缺少价格",
  benchmark_prices_pending: "基准行情尚待获取",
  benchmark_identity_pending: "基准身份正在核对",
  benchmark_identity_unconfirmed: "基准身份尚未确认",
  benchmark_not_etf_or_index: "所选基准尚未确认为指数或 ETF",
  benchmark_calendar_or_currency_mismatch: "基准与股票的交易日历或币种不一致",
  not_started: "尚未开始",
  not_yet_formed: "窗口尚未成熟",
  user_excluded: "用户排除",
  not_historical_year: "不在历史组",
  verified: "已核对",
  pending: "待核对",
};
export const sourceLabel = (status: unknown) =>
  sourceLabels[String(status ?? "")] ?? String(status ?? "未知");
const operationalRoots = [
  "home", "coverage", "earnings", "system", "jobs", "batches", "batch",
  "batch-jobs", "workers", "policy", "providers", "preferences",
];
/** Operational mutations refresh their summaries, never every cached resource. */
export function useInvalidate(roots: readonly string[] = operationalRoots) {
  const c = useQueryClient();
  return () =>
    c.invalidateQueries({
      predicate: (query) =>
        roots.includes(String(query.queryKey[0])) &&
        !isFrozenResearchQuery(query.queryKey) &&
        !isFrozenEntityQuery(query.queryKey),
    });
}
export const JOB_PAGE_SIZE = 20;
/** Twenty compact rows per page; checkpoint/result stay in the detail read. */
export function useJobs(cursor = "") {
  return useQuery({
    queryKey: ["jobs", cursor],
    queryFn: () =>
      api<JobsResponse>(
        `/jobs?${new URLSearchParams({ limit: String(JOB_PAGE_SIZE), ...(cursor ? { cursor } : {}) })}`,
      ),
    refetchInterval: (q) =>
      q.state.data?.items.some((x) => activeStatuses.includes(x.status))
        ? document.hidden
          ? 10000
          : 2000
        : false,
  });
}
export function useJob(id: string) {
  return useQuery({
    queryKey: ["jobs", "detail", id],
    queryFn: () => api<JobDetail>(`/jobs/${encodeURIComponent(id)}`),
    refetchInterval: (q) =>
      q.state.data && activeStatuses.includes(q.state.data.status)
        ? document.hidden
          ? 10000
          : 2000
        : false,
  });
}
export function useBatches(category = "all", cursor = "", policyKey = "", view = "all") {
  const c = useQueryClient();
  const previous = useRef<Map<string, Batch> | null>(null);
  const query = useQuery({
    queryKey: ["batches", category, cursor, policyKey, view],
    queryFn: () =>
      api<components["schemas"]["BatchesOutput"]>(
        `/batches?${new URLSearchParams({ category, cursor, policy_key: policyKey, view })}`,
      ),
    refetchInterval: (q) =>
      q.state.data?.items.some((x) => activeStatuses.includes(x.status))
        ? document.hidden
          ? 10000
          : 2000
        : 15000,
  });
  useEffect(() => {
    if (!query.data) return;
    const items = query.data.items;
    const currentIds = new Set(items.map(x => x.id));
    const changed = [
      ...items.filter(x => {
        const old = previous.current?.get(x.id);
        return previous.current && (!old || old.status !== x.status || old.updated_at !== x.updated_at);
      }),
      // Completion can remove a batch from the active list altogether.
      ...[...(previous.current?.values() ?? [])].filter(x => !currentIds.has(x.id)),
    ];
    if (changed.length) void c.invalidateQueries({ predicate: query => {
      const key = query.queryKey;
      if (isFrozenEntityQuery(key) || isFrozenResearchQuery(key)) return false;
      if (changed.some(x => mutableResearchForBatch(key, x.analysis_id))) return true;
      if (["batch", "batch-jobs"].includes(String(key[0]))) return changed.some(x => x.id === key[1]);
      // These non-research summaries expose progress or newly available facts.
      return ["home", "coverage", "earnings", "entity", "system"].includes(String(key[0]));
    } }, { cancelRefetch: false });
    previous.current = new Map(items.map(x => [x.id, x]));
  }, [query.data, c]);
  return query;
}
export function usePolicy() {
  return useQuery({
    queryKey: ["policy"],
    queryFn: () =>
      api<components["schemas"]["StrategyOutput"]>("/collection-policy"),
  });
}
export function usePreferences() {
  return useQuery({
    queryKey: ["preferences"],
    queryFn: () =>
      api<components["schemas"]["PreferenceOutput"]>("/preferences"),
  });
}
export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: () => api<GenericOutput>("/providers"),
  });
}
export function useCollection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (
      input: Pick<CollectionInput, "request_id" | "kind"> &
        Partial<CollectionInput>,
    ) => api<CollectionOutput>("/collections", "POST", input),
    onSuccess: invalidate,
  });
}
export function batchActionOptions(client: QueryClient) {
  return {
    mutationFn: ({
      id,
      action,
    }: {
      id: string;
      action: components["schemas"]["BatchAction"]["action"];
    }) =>
      api<CollectionOutput>(
        `/batches/${encodeURIComponent(id)}/actions`,
        "POST",
        { action },
      ),
    onSuccess: async (value: CollectionOutput, command: { id: string }) => {
      await client.cancelQueries({ predicate: query => query.queryKey[0] === "batch" &&
        [command.id, value.batch_id].includes(String(query.queryKey[1])) });
      // A command can commit before another client's action yet arrive later.
      // Its receipt confirms the command, not the latest batch state. Re-read
      // both identities after cancelling older GETs; equal/missing timestamps
      // cannot safely establish server ordering either.
      void client.invalidateQueries({ predicate: query => {
        const key = query.queryKey;
        return ["batches", "jobs", "system"].includes(String(key[0])) ||
          (key[0] === "batch" && [command.id, value.batch_id].includes(String(key[1]))) ||
          (key[0] === "batch-jobs" && [command.id, value.batch_id].includes(String(key[1]))) ||
          mutableResearchForBatch(key, value.batch.analysis_id);
      } });
    },
  };
}
export function useBatchAction() {
  return useMutation(batchActionOptions(useQueryClient()));
}
export function useJobAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({
      id,
      action,
    }: {
      id: string;
      action: "pause" | "resume" | "cancel" | "retry";
    }) =>
      api<Job>(`/jobs/${encodeURIComponent(id)}/actions`, "POST", { action }),
    onSuccess: invalidate,
  });
}
export function formatTime(value?: string | null, timeZone = "America/New_York") {
  if (!value) return "时间未知";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value}（仅日期）`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "short",
    timeZone,
  }).format(date);
  return `${formatted}（${timeZone === "America/New_York" ? "美东" : timeZone}）`;
}
export function formatNumber(value: number | string) {
  return Number.isFinite(Number(value))
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(
        Number(value),
      )
    : "—";
}
export function percent(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n)
    ? `${n > 0 ? "+" : ""}${(n * 100).toFixed(2)}%`
    : "—";
}
// Market quotes already contain a percentage, unlike research return ratios.
export function quotePercent(value: unknown) {
  if (value === null || value === undefined || value === "") return "暂未取得";
  const n = Number(value);
  return Number.isFinite(n)
    ? `${n > 0 ? "+" : ""}${n.toFixed(2)}%`
    : "暂未取得";
}
export function marketName(symbol: string) {
  return (
    (
      {
        "^GSPC": "标普500",
        "^IXIC": "纳斯达克综合",
        "^DJI": "道琼斯工业平均",
        "GC=F": "黄金期货",
        "^VIX": "VIX波动率指数",
        SEC: "Insider申报",
      } as Record<string, string>
    )[symbol] ?? symbol
  );
}
export function facts(value: unknown): Fact {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Fact)
    : {};
}
export function rows(value: unknown): Fact[] {
  return Array.isArray(value) ? value.map(facts) : [];
}
export function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(display).join("、");
  if (typeof value === "object")
    return Object.entries(value)
      .map(([k, v]) => `${sourceLabel(k)}：${display(v)}`)
      .join("；");
  return String(value);
}
