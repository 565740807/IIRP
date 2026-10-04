import { Timestamp } from "../components/Timestamp";
import { quoteStatusLabel, quoteTime } from "../freshnessRefresh";
import { entityRequestParams, entitySnapshotParams } from "../taskPresentation";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import {
  api,
  display,
  facts,
  formatNumber,
  percent,
  quotePercent,
  requestId,
  rows,
  sourceLabel,
  useCollection,
  type CollectionOutput,
  type TransactionSecurityInput,
  type RequestIdentity,
  type GenericOutput,
  type Fact,
} from "../api";
import { DateAnomaly, TradeSummary, TransactionTable } from "../components/Feed";
import { ResearchChart } from "../components/ResearchChart";
import { BatchPanel } from "../components/Batches";
import {
  sourceContext,
  sourceHref,
  useContextSearchParams,
} from "../researchStorage";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  useNotice,
} from "../components/ui";

function ReturnLink() {
  const location = useLocation();
  const from = location.state?.from;
  return (
    <Link
      className="text-link"
      to={sourceHref(location.state, "/insiders")}
      state={location.state?.fromState}
    >
      ← 返回{from ? "来源页面" : "Insider 信息流"}
    </Link>
  );
}

function SecurityMatch({ transaction }: { transaction: Fact }) {
  const [securityId, setSecurityId] = useState("");
  const [evidence, setEvidence] = useState("");
  const [opened, setOpened] = useState(!transaction.security_id);
  const client = useQueryClient();
  const candidates = rows(transaction.securities);
  const mutation = useMutation({
    mutationFn: () =>
      api<GenericOutput>(`/transactions/${transaction.id}/security`, "POST", {
        security_id: securityId,
        evidence,
      } satisfies TransactionSecurityInput),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ["entity", "transaction", transaction.id],
      }),
  });
  return (
    <section className="panel">
      <details
        className="result-notes"
        open={opened}
        onToggle={(event) => setOpened(event.currentTarget.open)}
      >
        <summary>
          {transaction.security_id
            ? `证券已确认：${display(candidates.find((c) => c.id === transaction.security_id)?.symbol ?? transaction.ticker)} · 修改对应关系`
            : "确认交易证券与股类，查看对应价格"}
        </summary>
        <div className="panel-body">
          <p>
            申报证券：{display(transaction.security_title)}
            {transaction.table === "II"
              ? ` · 标的证券：${display(transaction.underlying_security_title)}`
              : ""}
          </p>
          <p className="muted">
            同公司可能有不同股类。选择申报中的具体股票或衍生品标的，并记录核对依据。
          </p>
        </div>
        {candidates.length ? (
          <form
            className="analysis-form"
            onSubmit={(e) => {
              e.preventDefault();
              mutation.mutate();
            }}
          >
            <label className="ticker-field">
              交易对应的证券
              <select
                required
                value={securityId}
                onChange={(e) => setSecurityId(e.target.value)}
              >
                <option value="">请选择经核对的股类</option>
                {candidates.map((c) => (
                  <option key={c.id} value={c.id}>
                    {display(c.symbol)} ·{" "}
                    {display(c.name ?? c.share_class ?? "股类请核对原文")} ·{" "}
                    {sourceLabel(c.status)}
                  </option>
                ))}
              </select>
            </label>
            <label className="csv-field">
              证券核对依据
              <textarea
                required
                rows={3}
                value={evidence}
                minLength={5}
                maxLength={2000}
                onChange={(e) => setEvidence(e.target.value)}
                placeholder="填写公告或 SEC 申报链接、证券名称/股类及对应 ticker 的依据。"
              />
            </label>
            <ErrorNotice error={mutation.error} />
            <Button type="submit" busy={mutation.isPending}>
              保存证券对应关系
            </Button>
          </form>
        ) : (
          <div className="panel-body">
            <p>申报事实可读；尚无可核对的同发行人证券候选，价格分析未准备。</p>
            {transaction.ticker && <Link className="text-link" to={`/analysis/monthly?ticker=${encodeURIComponent(transaction.ticker)}`}>
              获取该公司证券资料与行情，再核对股类 →
            </Link>}
          </div>
        )}
        {transaction.security_id && (
          <div className="panel-footer">
            <p>核对依据：{display(facts(transaction.security_mapping).evidence)}</p>
            <p>对应关系版本 {display(transaction.mapping_version)}；证券标识符有效期自 {display(facts(transaction.security_mapping).identifier_valid_from)} 起。原始申报仍按原版本保存。</p>
            {rows(transaction.security_mapping_history).map((mapping) => <Link key={mapping.version ?? 1} className="text-link" to={`/transactions/${transaction.id}?mapping_version=${mapping.version ?? 1}`}>
              查看对应关系版本 {mapping.version ?? 1} →
            </Link>)}
          </div>
        )}
      </details>
    </section>
  );
}

function AmendmentReview({
  relation,
  transaction,
}: {
  relation: Fact;
  transaction: Fact;
}) {
  const [action, setAction] = useState("ADD");
  const [original, setOriginal] = useState(relation.original_event_id ?? "");
  const [evidence, setEvidence] = useState("");
  const [opened, setOpened] = useState(false);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["amendment-candidates", transaction.issuer_id],
    queryFn: () =>
      api<GenericOutput>(`/companies/${transaction.issuer_id}?limit=100`),
    enabled: opened,
  });
  const mutation = useMutation({
    mutationFn: () =>
      api(
        `/amendments/${relation.id}/resolve?${new URLSearchParams({ action, original_event_id: action === "ADD" ? "" : original, evidence })}`,
        "POST",
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["entity"] });
    },
  });
  return (
    <details
      className="result-notes"
      onToggle={(e) => setOpened(e.currentTarget.open)}
    >
      <summary>核对这条修订：{sourceLabel(relation.action)}</summary>
      <p>核对原始申报后选择逐行关系，原文与旧观察继续保留。</p>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <label>
          这条修订的作用
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="ADD">独立新增交易</option>
            <option value="REPLACE">替换一条原交易</option>
            <option value="UNCHANGED">重复，原交易未变化</option>
            <option value="REMOVE">撤回原交易</option>
          </select>
        </label>
        {action !== "ADD" && (
          <label className="ticker-field">
            选择被修订的原交易
            <select
              required
              value={original}
              onChange={(e) => setOriginal(e.target.value)}
            >
              <option value="">选择同公司、共同申报主体的交易</option>
              {query.data?.items
                .filter((row) => row.id !== transaction.id)
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {display(row.transaction_date)} · {display(row.owner)} ·{" "}
                    {display(row.kind)} · {display(row.shares)} 股
                  </option>
                ))}
            </select>
          </label>
        )}
        <label className="csv-field">
          逐行核对依据
          <textarea
            required
            value={evidence}
            rows={3}
            onChange={(e) => setEvidence(e.target.value)}
            placeholder="记录原文链接、页码或脚注，以及如何确认两行关系。"
          />
        </label>
        <ErrorNotice error={query.error ?? mutation.error} />
        <Button type="submit" busy={mutation.isPending}>
          保存修订关系
        </Button>
      </form>
    </details>
  );
}
export function EntityPage({ kind }: { kind: "company" | "person" }) {
  const { id } = useParams();
  return <EntityWorkspace key={`${kind}:${id}`} kind={kind} />;
}
function EntityWorkspace({ kind }: { kind: "company" | "person" }) {
  const { id } = useParams();
  const location = useLocation();
  const [params, setParams] = useContextSearchParams();
  const [start, setStart] = useState(params.get("start_date") ?? "");
  const [end, setEnd] = useState(params.get("end_date") ?? "");
  const [basis, setBasis] = useState(params.get("date_basis") ?? "transaction");
  const [recent, setRecent] = useState(params.get("recent_count") ?? "");
  const [company, setCompany] = useState(params.get("issuer_id") ?? "");
  const [action, setAction] = useState(params.get("action") ?? "all");
  const [rangeMode, setRangeMode] = useState(
    params.has("recent_count")
      ? "count"
      : params.has("start_date")
        ? "dates"
        : "three_months",
  );
  const page = Math.max(0, Number(params.get("entity_page") ?? 0));
  const [cursors, setCursors] = useState<string[]>(() => {
    try {
      return JSON.parse(
        sessionStorage.getItem(`iirp.entity.${kind}.${id}`) ?? '[""]',
      );
    } catch {
      return [""];
    }
  });
  useEffect(() => {
    try {
      sessionStorage.setItem(
        `iirp.entity.${kind}.${id}`,
        JSON.stringify(cursors),
      );
    } catch {
      /* URL retains current page. */
    }
  }, [cursors, kind, id]);
  // Reading-session cursors arrive asynchronously. Only an actual filter change
  // (including back/forward navigation) replaces the user's unsubmitted draft.
  const appliedFilters = new URLSearchParams(
    ["start_date", "end_date", "recent_count", "action", "date_basis", "issuer_id"]
      .filter((key) => params.has(key))
      .map((key) => [key, params.get(key)!]),
  ).toString();
  useEffect(() => {
    const filters = new URLSearchParams(appliedFilters);
    setStart(filters.get("start_date") ?? "");
    setEnd(filters.get("end_date") ?? "");
    setRecent(filters.get("recent_count") ?? "");
    setAction(filters.get("action") ?? "all");
    setBasis(filters.get("date_basis") ?? "transaction");
    setCompany(filters.get("issuer_id") ?? "");
    setRangeMode(
      filters.has("recent_count")
        ? "count"
        : filters.has("start_date")
          ? "dates"
          : "three_months",
    );
  }, [appliedFilters]);
  const collection = useCollection();
  const notice = useNotice();
  const queryClient = useQueryClient();
  const requestParams = entityRequestParams(params);
  const query = useQuery({
    queryKey: ["entity", kind, id, requestParams],
    queryFn: () =>
      api<GenericOutput>(
        `/${kind === "company" ? "companies" : "people"}/${id}?${requestParams}`,
      ),
    // A saved reading page is immutable. Returning to it can use the local page.
    staleTime: params.has("cursor") ? 60_000 : 5_000,
  });
  const entity = facts(query.data?.data.entity);
  const info = facts(query.data?.data);
  useEffect(() => {
    const next = entitySnapshotParams(params, info.session_id, query);
    if (next) {
      setCursors([next.get("cursor")!]);
      // The first response already IS this immutable page. Seed its canonical
      // cursor key before changing the URL, so the alias never downloads twice.
      queryClient.setQueryData(
        ["entity", kind, id, entityRequestParams(next)],
        query.data,
        { updatedAt: query.dataUpdatedAt },
      );
      setParams(next, { replace: true, state: location.state });
    }
  }, [info.session_id, params, setParams, location.state, queryClient, query.data, query.dataUpdatedAt, query.status, query.fetchStatus, kind, id]);
  function go(nextPage: number, nextCursor: string) {
    const next = new URLSearchParams(params);
    next.set("cursor", nextCursor);
    next.set("entity_page", String(nextPage));
    setParams(next, { state: location.state });
  }
  function fill() {
    collection.mutate(
      {
        request_id: requestId(),
        kind: "sec_history",
        ...(kind === "company" ? { issuer_id: id } : { owner_id: id }),
        ...(kind === "person" && company ? { issuer_id: company } : {}),
        ...(start && end ? { start_date: start, end_date: end } : {}),
        date_basis: basis as "transaction" | "accepted",
        ...(recent ? { recent_count: Number(recent) } : {}),
      },
      {
        onSuccess: () =>
          notice({ text: "历史需求已加入后台，已存记录可继续阅读。" }),
        onError: (e) => notice({ text: e.message, error: true }),
      },
    );
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <ReturnLink />
          <h1>{display(entity.name ?? entity.label ?? id)}</h1>
          <p>
            {kind === "company" ? "公司申报与交易历史" : "申报主体跨公司历史"}{" "}
            {display(entity.ticker)}
          </p>
        </div>
        <Button variant="primary" busy={collection.isPending} onClick={fill}>
          获取所选历史
        </Button>
      </div>
      <section className="panel">
        <form
          className="analysis-form"
          onSubmit={(e) => {
            e.preventDefault();
            setParams(
              {
                ...(rangeMode === "dates" && start
                  ? { start_date: start }
                  : {}),
                ...(rangeMode === "dates" && end ? { end_date: end } : {}),
                ...(kind === "person" && company ? { issuer_id: company } : {}),
                date_basis: basis,
                ...(rangeMode === "count"
                  ? { recent_count: recent || "50" }
                  : {}),
                action,
              },
              { state: location.state },
            );
            setCursors([""]);
          }}
        >
          <label>
            查看范围
            <select
              value={rangeMode}
              onChange={(e) => {
                setRangeMode(e.target.value);
                if (e.target.value === "count") {
                  setRecent("50");
                  setStart("");
                  setEnd("");
                } else if (e.target.value === "three_months") {
                  setRecent("");
                  setStart("");
                  setEnd("");
                } else setRecent("");
              }}
            >
              <option value="three_months">最近 3 个月</option>
              <option value="count">最近 50 条 / 自定条数</option>
              <option value="dates">自定日期</option>
            </select>
          </label>
          {rangeMode === "dates" && (
            <>
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
            </>
          )}
          <label>
            日期基准
            <select value={basis} onChange={(e) => setBasis(e.target.value)}>
              <option value="transaction">实际交易日期</option>
              <option value="accepted">SEC 接受日期</option>
            </select>
          </label>
          {rangeMode === "count" && (
            <label>
              最近交易明细条数
              <input
                type="number"
                min="1"
                max="500"
                value={recent}
                onChange={(e) => setRecent(e.target.value)}
              />
            </label>
          )}
          <label>
            交易行为
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="all">全部行为</option>
              <option value="buy">买入</option>
              <option value="sell">卖出</option>
              <option value="derivative">衍生品</option>
              <option value="other">授予、代扣与其他</option>
            </select>
          </label>
          {kind === "person" && (
            <label>
              公司
              <select
                value={company}
                onChange={(event) => setCompany(event.target.value)}
              >
                <option value="">全部公司</option>
                {rows(info.companies).map((item) => (
                  <option key={item.issuer_id} value={item.issuer_id}>
                    {display(item.name ?? item.ticker ?? item.issuer_id)}
                    {item.name && item.ticker ? ` · ${item.ticker}` : ""}
                  </option>
                ))}
                {company &&
                  !rows(info.companies).some(
                    (item) => item.issuer_id === company,
                  ) && <option value={company}>当前公司 · {company}</option>}
              </select>
            </label>
          )}
          <Button type="submit">应用本地筛选</Button>
        </form>
        <ErrorNotice error={query.error} retry={query.refetch} />
        {query.data && (
          <div className="panel-body">
            <p>
              {params.has("recent_count")
                ? `最近 ${params.get("recent_count")} 条`
                : `当前范围 ${display(facts(info.coverage).start_date)}—${display(facts(info.coverage).end_date)}`}{" "}
              · 已找到 {info.total ?? 0} 条 · {info.filings ?? 0} 份申报 ·{" "}
              {facts(info.coverage).complete
                ? "已核对完整"
                : "覆盖仍有待核对部分"}
            </p>
            {info.transaction_start && (
              <p>
                筛选内实际交易日：{info.transaction_start}—
                {info.transaction_end}
              </p>
            )}
            {info.summary_note && (
              <p className="muted">{String(info.summary_note)}</p>
            )}
            <TradeSummary summary={rows(info.summary)} />
          </div>
        )}
        {query.isPending ? (
          <Loading />
        ) : query.data?.items.length ? (
          <TransactionTable
            items={query.data.items}
            showCompany={kind === "person"}
            focusOwnerId={
              kind === "person"
                ? String(entity.id ?? id).padStart(10, "0")
                : undefined
            }
          />
        ) : (
          <EmptyState
            title="这个范围尚无本地交易记录"
            description="可获取所选历史。无本地命中不代表该主体没有申报。"
          />
        )}
        {info.coverage && (
          <div className="panel-footer">
            {(facts(info.coverage).start_date ||
              facts(info.coverage).end_date) && (
              <p>
                当前查看范围：{display(facts(info.coverage).start_date)} →{" "}
                {display(facts(info.coverage).end_date)}
                {` · ${info.date_basis === "accepted_at" || info.date_basis === "accepted_date" ? "SEC 接受日期（美东）" : "实际交易日期"}`}
              </p>
            )}
            {sourceLabel(facts(info.coverage).status)} · 已找到{" "}
            {display(facts(info.coverage).observed_count)} 条交易明细
            {facts(info.coverage).requested_count != null &&
              ` / 目标 ${facts(info.coverage).requested_count} 条`}
            <p>
              {facts(info.coverage).complete === true
                ? "所选范围已核对完整。"
                : "所选范围尚未核对完整。"}
              {display(facts(info.coverage).message)}
            </p>
            {info.as_of && (
              <small>
                本次阅读版本：<Timestamp value={info.as_of} />
              </small>
            )}
          </div>
        )}
        <div className="pagination">
          <Button
            disabled={!page || cursors[page - 1] == null}
            onClick={() => go(page - 1, cursors[page - 1])}
          >
            上一页
          </Button>
          <span>第 {page + 1} 页</span>
          <Button
            disabled={!info.next_cursor}
            onClick={() => {
              setCursors((c) => [...c.slice(0, page + 1), info.next_cursor]);
              go(page + 1, info.next_cursor);
            }}
          >
            下一页
          </Button>
        </div>
      </section>
      {entity.ticker && (
        <Link
          className="text-link"
          to={`/analysis/monthly?ticker=${entity.ticker}`}
        >
          分析 {entity.ticker} 历史行情 →
        </Link>
      )}
    </>
  );
}
function ContextChart({
  context,
  title,
  ticker,
  metadata,
}: {
  context: Fact;
  title: string;
  ticker: string;
  metadata: Fact;
}) {
  const chart = {
    metadata,
    series: [
      {
        key: title,
        label: title,
        group: "current",
        points: rows(context.points),
      },
    ],
  };
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>{title}</h2>
      </div>
      <p className="context-baseline">
        基准收盘 {display(context.baseline_date)} 归零 ·{" "}
        {sourceLabel(context.price_status ?? context.status)}
        {context.reaction_date ? ` · 第 1 日 ${context.reaction_date}` : ""}
      </p>
      <div className="context-summary">
        <span>
          前 5 日变化 <strong>{percent(context.pre_5)}</strong>
        </span>
        <span>
          第 1 日 <strong>{percent(context.day_1)}</strong>
          <small>
            {display(context.day_1_date)} · {sourceLabel(context.day_1_status)}
          </small>
        </span>
        <span>
          第 5 日 <strong>{percent(context.day_5)}</strong>
          <small>
            {display(context.day_5_date)} · {sourceLabel(context.day_5_status)}
          </small>
        </span>
      </div>
      <ResearchChart
        data={chart}
        title={`${ticker} · ${title}`}
        provenance={`${sourceLabel(metadata.price_basis)} · 截至 ${display(metadata.cutoff_date)}\n基准 ${display(context.baseline_date)} · 计算 ${display(metadata.calculation_version)}${metadata.source ? ` · 来源 ${sourceLabel(metadata.source)}` : ""}\n数据 ${display(metadata.dataset_id)}${metadata.data_version ? ` · 版本 ${String(metadata.data_version).slice(0, 12)}` : ""}`}
      />
      <details className="result-notes">
        <summary>实际日期与价格口径</summary>
        <p>
          {context.non_session_transaction
            ? "申报交易日期为非交易日，以后续交易日观察，未更改原申报日期。"
            : ""}
        </p>
        <p>成熟窗口：{display(context.mature_sessions)} 个交易日</p>
        {rows(context.points).map((p, i) => (
          <p key={i}>
            {p.x} · {p.date} · {percent(p.value)} · {sourceLabel(p.status)}
          </p>
        ))}
      </details>
    </section>
  );
}
export function TransactionPage() {
  const location = useLocation();
  const { id } = useParams();
  const selected = new URLSearchParams(location.search);
  const mappingVersion = selected.get("mapping_version");
  const datasetId = selected.get("dataset_id");
  const cutoffDate = selected.get("cutoff_date");
  const [windowBatch, setWindowBatch] = useState("");
  const query = useQuery({
    queryKey: ["entity", "transaction", id, mappingVersion, datasetId, cutoffDate],
    queryFn: () => api<GenericOutput>(`/transactions/${id}${mappingVersion || datasetId || cutoffDate ? `?${new URLSearchParams({...(mappingVersion ? {mapping_version: mappingVersion} : {}), ...(datasetId ? {dataset_id: datasetId} : {}), ...(cutoffDate ? {cutoff_date: cutoffDate} : {})})}` : ""}`),
  });
  const notice = useNotice();
  const fetchWindow = useMutation({
    mutationFn: () =>
      api<CollectionOutput>(`/transactions/${id}/price-window`, "POST", {
        request_id: requestId(),
      } satisfies RequestIdentity),
    onSuccess: (result) => {
      setWindowBatch(result.batch_id);
      notice({
        text: "双时间价格窗口已加入后台，范围由交易日和接受时间确定。",
      });
    },
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={query.refetch} />;
  const data = facts(query.data?.data);
  const t = facts(data.transaction);
  const context = facts(data.price_context);
  const ticker = t.ticker ?? data.ticker ?? "证券未知";
  return (
    <>
      <div className="page-heading">
        <div>
          <ReturnLink />
          <h1>{ticker} · 交易与价格</h1>
          <p>原始申报数值与仅拆股调整后的股价分别展示。{!t.ticker && t.issuer_ticker_raw ? ` SEC 原文证券字段：${t.issuer_ticker_raw}；尚未核对为可用 ticker。` : ""}</p>
        </div>
        {t.security_id && (
          <Button
            busy={fetchWindow.isPending}
            onClick={() => fetchWindow.mutate()}
          >
            获取双时间价格窗口
          </Button>
        )}
      </div>
      <ErrorNotice error={fetchWindow.error} />
      {windowBatch && <BatchPanel id={windowBatch} />}
      <section className="panel">
        <div className="section-heading">
          <h2>申报事实</h2>
        </div>
        <TransactionTable items={[t]} hideDetailLink />
        <div className="timeline">
          <div>
            <span>实际交易日期</span>
            <strong>{display(t.transaction_date)}</strong>
            <DateAnomaly row={t} />
          </div>
          <span>
            →{" "}
            {(context.disclosure_lag_calendar_days ??
              context.calendar_days_to_disclosure ??
              data.calendar_days_to_disclosure) == null
              ? "披露间隔待确定"
              : `${context.disclosure_lag_calendar_days ?? context.calendar_days_to_disclosure ?? data.calendar_days_to_disclosure} 个自然日`}{" "}
            →
          </span>
          <div>
            <span>SEC 接受时间（美东）</span>
            <strong>
              <Timestamp value={t.accepted_at ?? context.accepted_at} />
            </strong>
          </div>
          <div>
            <span>系统首次发现（美东）</span>
            <strong><Timestamp value={t.first_seen_at} /></strong>
          </div>
        </div>
        <div className="panel-body button-row">
          {(t.company_id ?? t.issuer_id) && (
            <Link
              className="text-link"
              to={`/companies/${t.company_id ?? t.issuer_id}`}
              state={sourceContext(location)}
            >
              公司历史
            </Link>
          )}
          {rows(t.owners).map((o) => (
            <Link
              key={o.id}
              className="text-link"
              to={`/people/${o.id}`}
              state={sourceContext(location)}
            >
              {display(o.name)}
            </Link>
          ))}
          {(t.source_url ?? data.source_url) && (
            <a
              className="text-link"
              href={t.source_url ?? data.source_url}
              target="_blank"
              rel="noreferrer"
            >
              SEC 原文 ↗
            </a>
          )}
        </div>
        {(t.footnotes ?? data.footnotes) && (
          <details className="result-notes">
            <summary>脚注与修订记录</summary>
            <p>{display(t.footnotes ?? data.footnotes)}</p>
            <p>{display(t.amendments ?? data.amendments)}</p>
          </details>
        )}
        {rows(t.amendments)
          .filter((a) =>
            ["UNCONFIRMED", "UNCONFIRMED_DUPLICATE"].includes(a.action),
          )
          .map((relation) => (
            <AmendmentReview
              key={relation.id}
              relation={relation}
              transaction={t}
            />
          ))}
      </section>
      {!t.security_id && <div className="notice notice-warning">申报事实可读；证券与股类尚待核对，交易和披露两个价格窗口未准备。</div>}
      {!t.security_id && (
        <SecurityMatch key={`${t.id}-unconfirmed`} transaction={t} />
      )}
      {t.security_id && context.dataset_id && <section className="panel"><p>价格版本 {display(context.dataset_id)} · 对应关系版本 {display(t.mapping_version)} · 截止 {display(context.cutoff_date)}。保存下列版本链接可复现当前读数。</p><div className="button-row"><a className="button button-secondary" href={`/api/v1/transactions/${id}/export?${new URLSearchParams({format:"csv",mapping_version:String(t.mapping_version),dataset_id:String(context.dataset_id),cutoff_date:String(context.cutoff_date)})}`}>导出双时间价格 CSV</a><a className="button button-secondary" href={`/api/v1/transactions/${id}/export?${new URLSearchParams({format:"json",mapping_version:String(t.mapping_version),dataset_id:String(context.dataset_id),cutoff_date:String(context.cutoff_date)})}`}>导出事实与价格 JSON</a></div></section>}
      {t.security_id && context.reason && (
        <div className="notice notice-warning">{display(context.reason)}</div>
      )}
      {(context.transaction || context.disclosure) && (
        <div className="dual-charts">
          <ContextChart
            context={facts(context.transaction)}
            title={
              t.table === "II" ? "交易日收盘归零 · 标的股价" : "交易日收盘归零"
            }
            ticker={ticker}
            metadata={context}
          />
          <ContextChart
            context={facts(context.disclosure)}
            title={facts(context.disclosure).title ?? "披露前最后收盘归零"}
            ticker={ticker}
            metadata={context}
          />
        </div>
      )}
      {t.security_id && (
        <SecurityMatch key={`${t.id}-${t.security_id}`} transaction={t} />
      )}
      {context.next_open && (
        <section className="panel">
          <details className="result-notes">
            <summary>从公开后下一次常规开盘观察</summary>
            <p>
              起点：{display(facts(context.next_open).start_date)} · 第 1 日：
              {percent(facts(context.next_open).day_1)} · 第 5 日：
              {percent(facts(context.next_open).day_5)}
            </p>
            <p>这是股价观察，不含交易成本与实际成交条件，不代表可跟单收益。</p>
          </details>
        </section>
      )}
    </>
  );
}
export function MarketDetailPage() {
  const { symbol } = useParams();
  const location = useLocation();
  const query = useQuery({
    queryKey: ["market", symbol],
    queryFn: () => api<GenericOutput>(`/market/${encodeURIComponent(symbol!)}`),
    refetchInterval: () => (document.hidden ? false : 3000),
  });
  const data = facts(query.data?.data);
  const quote = { ...facts(data.quote), ...data };
  const price = (value: unknown) =>
    value == null || value === "" || !Number.isFinite(Number(value))
      ? "暂未取得"
      : formatNumber(Number(value));
  const unit = quote.unit ? ` ${quote.unit}` : "";
  const chartData = useMemo(() => ({
    value_kind: "price",
    metadata: { alignment: "trading" },
    series: [{
      key: symbol,
      label: `${data.name ?? symbol} 日线`,
      group: "current",
      points: (query.data?.items ?? []).map((row, i) => ({
        x: i, date: row.date, value: row.close, status: "available",
      })),
    }],
  }), [query.data?.items, symbol, data.name]);
  return (
    <>
      <div className="page-heading">
        <div>
          <Link
            className="text-link"
            to={sourceHref(location.state, "/")}
            state={location.state?.fromState}
          >
            ← 市场概览
          </Link>
          <h1>{display(data.name ?? symbol)}</h1>
          <p>当前报价、历史来源与覆盖状态</p>
        </div>
        <Link
          className="button button-primary"
          state={sourceContext(location)}
          to={`/analysis/monthly?ticker=${encodeURIComponent(symbol!)}`}
        >
          研究历史路径
        </Link>
      </div>
      <ErrorNotice error={query.error} retry={query.refetch} />
      {query.isPending ? (
        <Loading />
      ) : (
        <section className="panel">
          <div className="section-heading">
            <h2>{display(data.name ?? symbol)}</h2>
            <span>{quoteStatusLabel(quote.status) ?? sourceLabel(quote.status)}</span>
          </div>
          <div className="summary-grid">
            <div>
              <span>{quote.status === "DAILY" ? "最近收盘" : "最近报价"}</span>
              <strong>
                {price(quote.value)}
                {unit}
              </strong>
            </div>
            <div>
              <span>涨跌幅</span>
              <strong>{quotePercent(quote.change_percent)}</strong>
            </div>
            <div>
              <span>{quote.status === "DAILY" ? "日线日期" : "报价对应时间"}</span>
              <strong><Timestamp value={quoteTime(quote)} /></strong>
              <small>{quote.delay || quoteStatusLabel(quote.status) || sourceLabel(quote.status)}</small>
            </div>
          </div>

          {quote.refresh_notice && <p className="panel-body" role="status">{quote.refresh_notice}</p>}
          {query.data?.items.length ? (
            <>
              <h3 className="panel-body">
                日线走势 · {display(quote.chart_start)} →{" "}
                {display(quote.chart_end)}
              </h3>
              <p className="panel-body muted">{quote.chart_note || (quote.chart_dataset_id ? "已核验日线；不是分时走势。" : "供应商日线序列，当日数据可能尚未收盘；不是分时走势。")}</p>
              <ResearchChart
                data={chartData}
                title={`${data.name ?? symbol} · 近期日线${data.unit ? `（${data.unit}）` : ""}`}
                provenance={`图表数据截至 ${display(quote.chart_end)} · ${quote.chart_source ? sourceLabel(quote.chart_source) : "图表来源尚未记录"}\n${display(quote.chart_basis)}`}
              />
            </>
          ) : null}
          <details className="result-notes">
            <summary>来源、采集时间与覆盖说明</summary>
            <dl className="definition-list panel-body">
              <dt>{quote.status === "DAILY" ? "最近日线收盘" : "最近报价"}</dt>
              <dd>
                {price(quote.value)}
                {unit}
              </dd>
              <dt>数据对应日期</dt>
              <dd>{quote.as_of ? display(quote.as_of) : "尚未取得有效日期"}</dd>
              <dt>来源记录时点（美东）</dt>
              <dd>
                {quote.status !== "DAILY" && quote.source_time
                  ? <Timestamp value={quote.source_time} />
                  : "仅有日线日期，来源未提供与该值配对的精确时刻"}
              </dd>
              <dt>最近完成来源检查</dt>
              <dd>
                {(quote.last_checked_at || quote.fetched_at)
                  ? <Timestamp value={quote.last_checked_at || quote.fetched_at} />
                  : "尚无采集时间记录"}
              </dd>
              <dt>来源</dt>
              <dd>
                {quote.source ? sourceLabel(quote.source) : "来源尚未记录"}
              </dd>
              <dt>行情口径</dt>
              <dd>
                {[quote.instrument, quote.delay, quote.baseline]
                  .filter(Boolean)
                  .join(" · ") || "行情尚未取得"}
              </dd>
              {quote.session && (
                <>
                  <dt>供应商交易时段</dt>
                  <dd><Timestamp value={quote.session.start} /> → <Timestamp value={quote.session.end} /></dd>
                </>
              )}
              <dt>涨跌基准日期</dt>
              <dd>{quote.baseline_date || "来源未提供可确认的前收日期"}</dd>
              <dt>前一收盘</dt>
              <dd>
                {price(quote.previous_close)}
                {unit}
              </dd>
              {quote.coverage && (
                <>
                  <dt>日线覆盖</dt>
                  <dd>{display(quote.coverage)}</dd>
                </>
              )}
              {(quote.reason || quote.message) && (
                <>
                  <dt>缺口说明</dt>
                  <dd>{display(quote.reason ?? quote.message)}</dd>
                </>
              )}
            </dl>
          </details>
        </section>
      )}
    </>
  );
}
