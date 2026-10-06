import { systemRefreshInterval } from "../systemRefresh";
import { Timestamp } from "../components/Timestamp";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  display,
  facts,
  formatNumber,
  marketName,
  requestId,
  sourceLabel,
  useCollection,
  useInvalidate,
  usePreferences,
  useProviders,
  type CollectionInput,
  type GenericOutput,
  type Fact,
} from "../api";
import {
  PolicyControl,
  ScopeNotice,
} from "../components/CollectionControls";
import { BatchList, BatchPanel } from "../components/Batches";
import { PriceRangePreview } from "../components/PriceRange";
import { TaskList } from "../components/Tasks";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  useNotice,
} from "../components/ui";
function ProviderList() {
  const q = useProviders();
  return (
    <section id="sources" className="panel">
      <div className="section-heading">
        <h2>数据来源</h2>
        <span>来源可用性与范围覆盖分别记录</span>
      </div>
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.isPending ? (
        <Loading />
      ) : (
        <div className="provider-list">
          {q.data?.items.map((p) => (
            <details className="provider" key={p.id}>
              <summary className="provider-trigger">
                <strong>{p.name}</strong>
                <span>{sourceLabel(p.status)}</span>
                <span className="provider-budget">{p.budget}</span>
                <span>⌄</span>
              </summary>
              <div className="provider-description">
                <p>{p.message}</p>
                <p>
                  {p.configured ? "已配置" : "尚未配置"} ·
                  配置方法见本地运行手册
                </p>
              </div>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
function CollectionForm() {
  const [params] = useSearchParams();
  const [kind, setKind] = useState<CollectionInput["kind"]>("market_history");
  const [tickers, setTickers] = useState(params.get("ticker") ?? "AAPL");
  const preferences = usePreferences();
  const [years, setYears] = useState<number | null>(null);
  const [months, setMonths] = useState<number | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [filingUrl, setFilingUrl] = useState("");
  const [batch, setBatch] = useState("");
  const c = useCollection();
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>获取与回补</h2>
        <span>支持完整历史目标；只补缺失范围</span>
      </div>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          c.mutate(
            {
              request_id: requestId(),
              kind,
              tickers: kind.startsWith("sec_")
                ? []
                : tickers
                    .toUpperCase()
                    .split(/[\s,，]+/)
                    .filter(Boolean),
              historical_years: years,
              history_months: months,
              ...(kind === "sec_filing"
                ? { filing_url: filingUrl.trim() }
                : start || end
                  ? { start_date: start, end_date: end }
                  : {}),
              intent: "fill_missing",
            },
            { onSuccess: (r) => setBatch(r.batch_id) },
          );
        }}
      >
        <label>
          数据类型
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as CollectionInput["kind"])}
          >
            <option value="market_history">历史行情</option>
            <option value="sec_latest">最新 SEC 申报</option>
            <option value="sec_history">SEC 历史申报</option>
            <option value="sec_filing">指定 SEC 原文</option>
          </select>
        </label>
        {kind === "sec_filing" ? (
          <label className="ticker-field">
            SEC 完整申报原文地址
            <input
              type="url"
              required
              value={filingUrl}
              onChange={(e) => setFilingUrl(e.target.value)}
              placeholder="https://www.sec.gov/Archives/edgar/data/…/完整申报.txt"
            />
          </label>
        ) : !kind.startsWith("sec_") ? (
          <label className="ticker-field">
            证券代码
            <input
              value={tickers}
              onChange={(e) => setTickers(e.target.value)}
              placeholder="AAPL, MSFT"
            />
          </label>
        ) : (
          <p>全市场 SEC 申报；按公司或人员回补可从对应详情页发起。</p>
        )}
        {kind === "sec_history" ? (
          <label>
            默认回补月数
            <input
              type="number"
              min="1"
              required
              value={months ?? preferences.data?.values.history_months ?? 3}
              onChange={(e) => setMonths(e.target.valueAsNumber)}
            />
          </label>
        ) : kind !== "sec_filing" && kind !== "sec_latest" ? (
          <label>
            历史年数
            <input
              type="number"
              min="1"
              required
              value={years ?? preferences.data?.values.historical_years ?? 8}
              onChange={(e) => setYears(e.target.valueAsNumber)}
            />
          </label>
        ) : null}
        <Button type="submit" variant="primary" busy={c.isPending}>
          {kind === "sec_filing" ? "获取这一份原文" : "获取所选范围"}
        </Button>
        {kind === "sec_filing" ? (
          <p className="muted csv-field">
            仅获取并解析指定 accession
            的完整申报文件，保留来源与修订记录；单份原文不代表历史覆盖完整。
          </p>
        ) : (
          <details className="advanced-options">
            <summary>使用明确起止日期</summary>
            <div className="analysis-form nested-form">
              <label>
                开始日期
                <input
                  type="date"
                  value={start}
                  onChange={(e) => setStart(e.target.value)}
                />
              </label>
              <label>
                结束日期
                <input
                  type="date"
                  value={end}
                  onChange={(e) => setEnd(e.target.value)}
                />
              </label>
            </div>
          </details>
        )}
      </form>
      {kind === "market_history" && <PriceRangePreview request={{ historical_years: years,
        start_date: start || null, end_date: end || null }} />}
      <ErrorNotice error={c.error} />
      {batch && (
        <div className="panel-body">
          <BatchPanel id={batch} />
        </div>
      )}
    </section>
  );
}
function CoveragePanel() {
  const [ticker, setTicker] = useState("");
  const [selected, setSelected] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [scope, setScope] = useState("");
  const q = useQuery({
    queryKey: ["coverage", selected, scope],
    queryFn: () =>
      api<GenericOutput>(
        `/coverage?ticker=${encodeURIComponent(selected)}${scope}`,
      ),
  });
  return (
    <section className="panel" id="coverage">
      <div className="section-heading">
        <h2>行情缓存覆盖</h2>
      </div>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          setSelected(ticker.trim().toUpperCase());
          setScope(`&start_date=${start}&end_date=${end}`);
        }}
      >
        <label>
          证券代码
          <input
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="留空查看全部证券"
          />
        </label>
        <label>
          开始日期
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <Button type="submit">查看覆盖</Button>
      </form>
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.isPending ? (
        <Loading />
      ) : !q.data?.items.length ? (
        <EmptyState
          title="尚无行情缓存"
          description="行情为 24 小时缓存，分析或交易详情需要时自动获取。"
        />
      ) : (
        <div className="scope-list">
          {q.data.items.map((item, i) => {
            const cov = facts(item.coverage ?? item);
            return (
              <div className="scope-item" key={item.id ?? i}>
                <div className="scope-title">
                  <strong>
                    {marketName(
                      display(item.symbol ?? item.ticker ?? item.target),
                    )}
                  </strong>
                  <span>{sourceLabel(cov.status)}</span>
                </div>
                <p>
                  {item.price_range && <>行情目标：{facts(item.price_range).target_start_date} → {facts(item.price_range).target_end_date}<br /></>}
                  {display(cov.start_date)} → {display(cov.end_date)} · 有效{" "}
                  {display(cov.valid_sessions)} / 应有{" "}
                  {display(cov.expected_sessions)} 个交易日
                </p>
                <p>已取得有效行情：{display(cov.first_valid_date)} → {display(cov.last_valid_date)}；获取于 <Timestamp value={cov.as_of} />，有效至 <Timestamp value={cov.expires_at} /></p>
                <p>{display(cov.reasons ?? cov.message)}</p>
                {cov.missing_dates?.length > 0 && (
                  <details>
                    <summary>
                      查看 {cov.missing_dates.length} 个缺失日期
                    </summary>
                    <p>{display(cov.missing_dates)}</p>
                  </details>
                )}
              </div>
            );
          })}
        </div>
      )}
      {q.data?.data && (
        <p className="panel-footer">
          {display(q.data.data.summary ?? q.data.data.notice)}
        </p>
      )}
    </section>
  );
}
function StoragePanel() {
  const q = useQuery({
    queryKey: ["system"],
    queryFn: () => api<Fact>("/system"),
    refetchInterval: (state) => systemRefreshInterval(state.state.data?.storage?.inventory_status),
  });
  const invalidate = useInvalidate();
  const [report, setReport] = useState<Fact | null>(null);
  const notice = useNotice();
  const cleanup = useMutation({
    mutationFn: () => api<GenericOutput>("/cache/cleanup", "POST"),
    onSuccess: (r) => {
      setReport(r.data);
      notice({ text: "清理任务已创建，可在后台任务中查看进度与结果。" });
      void invalidate();
    },
  });
  const exact = useMutation({
    mutationFn: () => api<GenericOutput>("/system/storage/exact", "POST"),
    onSuccess: () => {
      notice({ text: "已开始精确核对目录占用，机械硬盘上约需 10 分钟。" });
      void q.refetch();
    },
  });
  const storage = facts(q.data?.storage);
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>容量与备份</h2>
        <div className="button-row">
          <Button busy={cleanup.isPending} onClick={() => cleanup.mutate()}>
            清理未引用缓存
          </Button>
          <Button
            busy={exact.isPending || storage.exact_status === "refreshing"}
            onClick={() => exact.mutate()}
          >
            精确核对目录占用
          </Button>
        </div>
      </div>
      <ErrorNotice error={q.error} />
      <ErrorNotice error={cleanup.error} />
      <ErrorNotice error={exact.error} />
      <div className="storage-grid">
        <div>
          <span>可用磁盘</span>
          <strong>
            {typeof storage.free_bytes === "number"
              ? `${formatNumber(storage.free_bytes / 1024 ** 3)} GiB`
              : "未知（磁盘测量失败）"}
          </strong>
        </div>
        <div>
          <span>原始来源文件</span>
          <strong>{storage.inventory_status === "fresh" ? `${display(storage.objects)} 个` : "盘点待更新"}</strong>
        </div>
        <div>
          <span>登记来源内容长度</span>
          <strong>
            {storage.inventory_status === "fresh"
              ? `${formatNumber(Number(storage.bytes) / 1024 ** 2)} MiB`
              : "未知"}
          </strong>
        </div>
        <div>
          <span>来源目录实际分配</span>
          <strong>{typeof storage.paths?.evidence?.allocated_bytes === "number" ? `${formatNumber(storage.paths.evidence.allocated_bytes / 1024 ** 2)} MiB` : "未核对"}</strong>
        </div>
      </div>
      <div className="panel-body">
        <p>
          容量盘点：{storage.inventory_status === "fresh" ? `已测量 ${display(storage.inventory_measured_at)}` : storage.inventory_status === "failed" ? "失败" : storage.inventory_status === "stale" ? "已过期" : "进行中／未测量"}。
          {storage.inventory_status === "fresh"
            ? ` 独立恢复预估 ${formatNumber(Number(storage.restore_estimated_temporary_bytes) / 1024 ** 3)} GiB；安全预留 ${formatNumber(Number(storage.min_free_bytes) / 1024 ** 3)} GiB；${storage.restore_capacity_sufficient === null ? "估算空间未知" : storage.restore_capacity_sufficient ? "估算空间足够" : "估算空间不足"}。`
            : " 恢复空间结论未知。"}
          {storage.inventory_error && ` 错误：${display(storage.inventory_error)}`}
        </p>
        {storage.inventory_status === "fresh" && <p className="muted">{display(storage.estimate_scope)}；执行前会重新测量，不使用此盘点作安全放行。</p>}
        <p className="muted">
          目录精确核对：{storage.exact_status === "fresh" ? `${display(storage.exact_measured_at)} 完成` : storage.exact_status === "refreshing" ? "进行中" : storage.exact_status === "failed" ? `失败${storage.exact_error ? `：${display(storage.exact_error)}` : ""}` : "尚未运行"}（手动触发；后台最多每天一次，在美东 0–5 点）。
        </p>
        <p>{display(storage.retention ?? q.data?.retention)}</p>
        {q.data?.backup && <p>备份：{display(q.data.backup)}</p>}
        {report && (
          <div className="notice notice-info">
            <span>{display(report.message ?? "清理任务已创建")}</span>
            {report.batch_id && (
              <Link className="text-link" to={`/data?batch=${report.batch_id}`}>
                查看清理任务 →
              </Link>
            )}
          </div>
        )}
        <p className="muted">
          清理只处理无有效引用的派生缓存；历史行情、申报事实与原文按保留策略维护。备份计划在自动更新区独立控制。
        </p>
      </div>
    </section>
  );
}
function PreferencesPanel() {
  const q = usePreferences();
  const [years, setYears] = useState<number | null>(null);
  const [months, setMonths] = useState<number | null>(null);
  const [automaticHistory, setAutomaticHistory] = useState<boolean | null>(
    null,
  );
  const c = useQueryClient();
  const notice = useNotice();
  const mutation = useMutation({
    mutationFn: () =>
      api("/preferences", "PATCH", {
        historical_years: years ?? q.data?.values.historical_years ?? 8,
        history_months: months ?? q.data?.values.history_months ?? 3,
        comparison: q.data?.values.comparison ?? "same_progress",
        automatic_history:
          automaticHistory ?? q.data?.values.automatic_history ?? true,
      }),
    onSuccess: () => {
      void c.invalidateQueries({ queryKey: ["preferences"] });
      notice({
        text: "默认范围已保存。手动批次保留原范围，自动回补遵循新边界。",
      });
    },
  });
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>默认研究范围</h2>
      </div>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <label>
          历史年数
          <input
            type="number"
            min="1"
            required
            value={years ?? q.data?.values.historical_years ?? 8}
            onChange={(e) => setYears(e.target.valueAsNumber)}
          />
        </label>
        <label>
          SEC 回补月数
          <input
            type="number"
            min="1"
            required
            value={months ?? q.data?.values.history_months ?? 3}
            onChange={(e) => setMonths(e.target.valueAsNumber)}
          />
        </label>
        <label>
          自动历史回补
          <input
            type="checkbox"
            checked={
              automaticHistory ?? q.data?.values.automatic_history ?? true
            }
            onChange={(e) => setAutomaticHistory(e.target.checked)}
          />
          <small>关闭只停止自动历史回补，最新数据检查继续。</small>
        </label>
        <Button type="submit" busy={mutation.isPending}>
          保存默认范围
        </Button>
      </form>
      <ErrorNotice error={q.error ?? mutation.error} />
    </section>
  );
}
export function DiagnosticsPage() {
  const [ticker, setTicker] = useState("AAPL");
  const invalidate = useInvalidate();
  const mutation = useMutation({
    mutationFn: (kind: string) =>
      api("/diagnostics/collections", "POST", {
        kind,
        ...(kind === "market_probe" ? { ticker } : {}),
      }),
    onSuccess: invalidate,
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>开发诊断</h1>
          <p>SAMPLE_ONLY · 来源探测不会成为正式覆盖或研究结果。</p>
        </div>
        <Link className="text-link" to="/data">
          返回数据与任务
        </Link>
      </div>
      <section className="panel">
        <div className="analysis-form">
          <label>
            诊断股票
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
            />
          </label>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("market_probe")}
          >
            行情小样验证
          </Button>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("sec_probe")}
          >
            SEC 来源验证
          </Button>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("fixture_check")}
          >
            本地合成任务
          </Button>
        </div>
        <ErrorNotice error={mutation.error} />
        <TaskList />
      </section>
    </>
  );
}
export function DataPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "tasks";
  const invalidate = useInvalidate();
  const targetBatch = params.get("batch");
  useEffect(() => {
    if (targetBatch) window.scrollTo({ top: 0, behavior: "instant" });
  }, [targetBatch]);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>数据与任务</h1>
          <p>获取范围、覆盖缺口与更新策略都在这里管理。</p>
        </div>
        <Button variant="ghost" onClick={() => void invalidate()}>
          刷新状态
        </Button>
      </div>
      <nav className="tabs" aria-label="数据管理分区">
        {[
          ["tasks", "任务中心"],
          ["collect", "获取数据"],
          ["coverage", "数据覆盖"],
          ["automatic", "自动更新"],
          ["sources", "数据来源"],
          ["maintenance", "维护与偏好"],
        ].map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("tab", id);
              next.delete("batch");
              setParams(next);
            }}
          >
            {label}
          </button>
        ))}
      </nav>
      {params.get("batch") && <BatchPanel id={params.get("batch")!} />}
      {tab === "automatic" && <PolicyControl full />}
      {tab === "collect" && (
        <>
          <ScopeNotice />
          <CollectionForm />
        </>
      )}
      {tab === "tasks" && (
        <section className="panel">
          <div className="section-heading">
            <h2>任务中心</h2>
            <span>暂停、取消意图持久保存</span>
          </div>
          <BatchList
            initialCategory={["running", "waiting", "attention", "history"].includes(params.get("category") ?? "") ? params.get("category")! : "running"}
            onCategoryChange={(category) => {
              const next = new URLSearchParams(params);
              next.set("category", category);
              setParams(next, { replace: true });
            }}
          />
        </section>
      )}
      {tab === "coverage" && <CoveragePanel />}
      {tab === "sources" && <ProviderList />}
      {tab === "maintenance" && (
        <>
          <PreferencesPanel />
          <StoragePanel />
          <details className="diagnostic">
            <summary>开发与诊断入口</summary>
            <Link className="text-link" to="/data/diagnostics">
              打开来源探测与底层任务
            </Link>
          </details>
        </>
      )}
    </>
  );
}
