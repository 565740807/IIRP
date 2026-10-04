import { systemRefreshInterval } from "../systemRefresh";
import { correctionSources } from "../earningsCorrectionSources";
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
  rows,
  sourceLabel,
  useCollection,
  useInvalidate,
  usePreferences,
  useProviders,
  type CollectionInput,
  type EventCorrection,
  type GenericOutput,
  type ImportInput,
  type ImportOutput,
  type Fact,
} from "../api";
import {
  CollectionButton,
  PolicyControl,
  ScopeNotice,
} from "../components/CollectionControls";
import { BatchList, BatchPanel } from "../components/Batches";
import { PriceRangePreview } from "../components/PriceRange";
import { TaskList } from "../components/Tasks";
import { FactTable } from "../components/ResearchTable";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  Modal,
  useNotice,
} from "../components/ui";
export function ProviderList() {
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
            <option value="earnings">财报事件与行情</option>
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
  const notice = useNotice();
  const invalidate = useInvalidate();
  const q = useQuery({
    queryKey: ["coverage", selected, scope],
    queryFn: () =>
      api<GenericOutput>(
        `/coverage?ticker=${encodeURIComponent(selected)}${scope}`,
      ),
  });
  const maintain = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api(`/securities/${id}/maintenance?enabled=${enabled}`, "PATCH"),
    onSuccess: invalidate,
    onError: (e) => notice({ text: e.message, error: true }),
  });
  return (
    <section className="panel" id="coverage">
      <div className="section-heading">
        <h2>覆盖与长期维护</h2>
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
            placeholder="留空查看已维护对象"
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
          title="尚无正式覆盖记录"
          description="完成获取与核验后，会按实际交易日和字段显示覆盖。"
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
                  {item.security_id && (
                    <Button
                      variant="ghost"
                      busy={maintain.isPending}
                      onClick={() =>
                        maintain.mutate({
                          id: item.security_id,
                          enabled: !(
                            item.maintain ??
                            item.maintained ??
                            item.active
                          ),
                        })
                      }
                    >
                      {(item.maintain ?? item.maintained ?? item.active)
                        ? "停止长期维护"
                        : "加入长期维护"}
                    </Button>
                  )}
                </div>
                <p>
                  {item.price_range && <>行情目标：{facts(item.price_range).target_start_date} → {facts(item.price_range).target_end_date}<br /></>}
                  {display(cov.start_date)} → {display(cov.end_date)} · 有效{" "}
                  {display(cov.valid_sessions)} / 应有{" "}
                  {display(cov.expected_sessions)} 个交易日
                </p>
                <p>已取得有效行情：{display(cov.first_valid_date)} → {display(cov.last_valid_date)}；数据集生成时间：<Timestamp value={cov.as_of} /></p>
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
function ImportPanel() {
  const [kind, setKind] = useState<ImportInput["kind"]>("market");
  const [ticker, setTicker] = useState("AAPL");
  const [source, setSource] = useState("");
  const [basis, setBasis] = useState<ImportInput["price_basis"]>("UNVERIFIED");
  const [csv, setCsv] = useState("");
  const [preview, setPreview] = useState<ImportOutput | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const invalidate = useInvalidate();
  const mutation = useMutation({
    mutationFn: (v: ImportInput) =>
      api<ImportOutput>("/imports/preview", "POST", v),
    onSuccess: (r) => {
      setPreview(r);
      setError(null);
    },
    onError: setError,
  });
  const commit = useMutation({
    mutationFn: () =>
      api<ImportOutput>(`/imports/${preview!.id}/commit`, "POST"),
    onSuccess: (r) => {
      setPreview(r);
      void invalidate();
    },
    onError: setError,
  });
  function change() {
    setPreview(null);
  }
  return (
    <section className="panel" id="imports">
      <div className="section-heading">
        <h2>CSV 导入</h2>
        <span>先预览差异，再确认入库</span>
      </div>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate({
            kind,
            ticker,
            csv,
            source_url: source,
            price_basis: basis,
          });
        }}
      >
        <label>
          导入类型
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as ImportInput["kind"]);
              change();
            }}
          >
            <option value="market">历史行情</option>
            <option value="earnings">财报事件</option>
          </select>
        </label>
        <label>
          证券代码
          <input
            required
            value={ticker}
            onChange={(e) => {
              setTicker(e.target.value.toUpperCase());
              change();
            }}
          />
        </label>
        <label className="ticker-field">
          来源链接
          <input
            type="url"
            required
            value={source}
            placeholder="https://…"
            onChange={(e) => {
              setSource(e.target.value);
              change();
            }}
          />
        </label>
        {kind === "market" && (
          <label>
            价格口径
            <select
              value={basis}
              onChange={(e) => {
                setBasis(e.target.value as ImportInput["price_basis"]);
                change();
              }}
            >
              <option value="UNVERIFIED">未核验（保留来源观察）</option>
              <option value="SPLIT_ONLY">仅拆股调整</option>
            </select>
          </label>
        )}
        <label className="csv-field">
          CSV 文件（最多 5 MB）
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              if (f.size > 5_000_000) {
                setError(new Error("文件超过 5 MB，请拆分后导入。"));
                return;
              }
              setCsv(await f.text());
              change();
            }}
          />
        </label>
        <label className="csv-field">
          预览输入内容
          <textarea
            required
            rows={5}
            value={csv}
            onChange={(e) => {
              setCsv(e.target.value);
              change();
            }}
            placeholder={
              kind === "market"
                ? "date,open,high,low,close,volume\n2026-08-03,100,102,99,101,100000"
                : "fiscal_year,fiscal_quarter,announced_date,announced_at,time_precision,source_url"
            }
          />
        </label>
        <Button type="submit" busy={mutation.isPending}>
          预览差异
        </Button>
      </form>
      <ErrorNotice error={error} />
      {preview && (
        <div className="import-preview">
          <div className="notice notice-info">
            {sourceLabel(preview.status)} · {preview.valid_rows} 行有效 ·
            此预览冻结了当前文件及来源信息
          </div>
          {preview.errors.map((e, i) => (
            <p className="form-error" key={i}>
              {e}
            </p>
          ))}
          {preview.differences.map((d, i) => (
            <p key={i}>{d}</p>
          ))}
          <FactTable
            data={preview.preview}
            columns={Object.keys(preview.preview[0] ?? {}).map((k) => ({
              key: k,
              label: k,
            }))}
          />
          <div className="button-row">
            <Button
              variant="primary"
              busy={commit.isPending}
              disabled={
                !preview.valid_rows ||
                preview.errors.length > 0 ||
                ["COMMITTED", "committed"].includes(preview.status)
              }
              onClick={() => commit.mutate()}
            >
              确认导入 {preview.valid_rows} 行
            </Button>
            {preview.batch_id && (
              <Link
                className="text-link"
                to={`/data?batch=${preview.batch_id}`}
              >
                查看导入批次 →
              </Link>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
function EventEditor({
  event,
  ticker,
  close,
}: {
  event: Fact;
  ticker: string;
  close: () => void;
}) {
  const [value, setValue] = useState<EventCorrection>({
    fiscal_year: event.fiscal_year ?? new Date().getFullYear(),
    fiscal_quarter: event.fiscal_quarter ?? event.quarter ?? 1,
    announced_date: event.announced_date ?? "",
    announced_at: event.announced_at ?? null,
    time_precision: event.time_precision ?? "date_only",
    is_primary: event.is_primary ?? true,
    ...correctionSources(event),
    note: "",
    revision: event.revision ?? 1,
  });
  const invalidate = useInvalidate();
  const mutation = useMutation({
    mutationFn: () =>
      api<GenericOutput>(`/earnings/${event.id}`, "PATCH", value),
    onSuccess: () => {
      void invalidate();
      close();
    },
  });
  return (
    <Modal
      open
      onOpenChange={close}
      title={`${ticker} · 核对财报事件`}
      description="保留原始观察与修订历史；精确反应统计只使用满足时间要求的事件。"
    >
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <label>
          财政年度
          <input
            type="number"
            required
            min="1"
            max="9998"
            value={value.fiscal_year}
            onChange={(e) =>
              setValue((v) => ({ ...v, fiscal_year: e.target.valueAsNumber }))
            }
          />
        </label>
        <label>
          财政季度
          <select
            value={value.fiscal_quarter}
            onChange={(e) =>
              setValue((v) => ({
                ...v,
                fiscal_quarter: Number(e.target.value),
              }))
            }
          >
            {[1, 2, 3, 4].map((q) => (
              <option key={q} value={q}>
                Q{q}
              </option>
            ))}
          </select>
        </label>
        <label>
          公告日期
          <input
            type="date"
            required
            value={value.announced_date}
            onChange={(e) =>
              setValue((v) => ({ ...v, announced_date: e.target.value }))
            }
          />
        </label>
        <label>
          时间精度
          <select
            value={value.time_precision}
            onChange={(e) =>
              setValue((v) => ({
                ...v,
                time_precision: e.target
                  .value as EventCorrection["time_precision"],
              }))
            }
          >
            {[
              "exact",
              "before_open",
              "after_close",
              "date_only",
              "intraday",
              "conflict",
            ].map((t) => (
              <option key={t} value={t}>
                {sourceLabel(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="csv-field">
          公告时刻（含时区，未知留空）
          <input
            value={value.announced_at ?? ""}
            placeholder="2026-07-30T16:30:00-04:00"
            onChange={(e) =>
              setValue((v) => ({ ...v, announced_at: e.target.value || null }))
            }
          />
        </label>
        {["exact", "before_open", "after_close"].includes(value.time_precision) && <label className="csv-field">
          实际首次公开时刻或盘前/盘后来源原文及位置
          <textarea
            required
            rows={2}
            value={value.time_evidence ?? ""}
            onChange={(e) => setValue((v) => ({ ...v, time_evidence: e.target.value }))}
            placeholder="引用发行人公告中明确的发布时间或盘前/盘后说明；电话会时间和官方排期不适用"
          />
        </label>}
        <label>
          财期类型
          <select
            value={value.period_kind ?? "unknown"}
            onChange={(e) => setValue((v) => ({
              ...v,
              period_kind: e.target.value as EventCorrection["period_kind"],
              period_kind_source_url: e.target.value === "unknown" ? null : v.period_kind_source_url,
              period_kind_evidence: e.target.value === "unknown" ? null : v.period_kind_evidence,
            }))}
          >
            <option value="unknown">未知 · 不进入常规财季汇总</option>
            <option value="regular">常规 · 须有明确来源</option>
            <option value="transition">过渡 · 不进入常规财季汇总</option>
          </select>
        </label>
        {value.period_kind !== "unknown" && <label className="csv-field">
          财期类型来源链接（可与公告日期来源不同）
          <input
            type="url"
            required
            value={value.period_kind_source_url ?? ""}
            onChange={(e) => setValue((v) => ({ ...v, period_kind_source_url: e.target.value }))}
          />
        </label>}
        {value.period_kind !== "unknown" && <label className="csv-field">
          财期类型的来源原文或明确位置
          <textarea
            required
            rows={2}
            value={value.period_kind_evidence ?? ""}
            onChange={(e) => setValue((v) => ({ ...v, period_kind_evidence: e.target.value }))}
          />
        </label>}
        <label className="csv-field">
          公告日期/时刻核对来源
          <input
            type="url"
            required
            value={value.source_url}
            onChange={(e) =>
              setValue((v) => ({ ...v, source_url: e.target.value }))
            }
          />
        </label>
        <label className="csv-field">
          修正依据
          <textarea
            required
            rows={3}
            value={value.note}
            onChange={(e) => setValue((v) => ({ ...v, note: e.target.value }))}
          />
        </label>
        <label className="check-label csv-field">
          <input
            type="checkbox"
            checked={value.is_primary}
            onChange={(e) =>
              setValue((v) => ({ ...v, is_primary: e.target.checked }))
            }
          />
          作为该财年财季的主公告
        </label>
        <p className="muted csv-field">
          同季重复或辅助公告请取消勾选，保留其真实财年、季度与核对依据。
        </p>
        <ErrorNotice error={mutation.error} />
        <Button type="submit" variant="primary" busy={mutation.isPending}>
          保存核对结果
        </Button>
      </form>
      <details className="result-notes">
        <summary>来源与核对记录（{rows(event.evidence).length}）</summary>
        {rows(event.evidence).map((record, index) => (
          <article className="scope-item" key={index}>
            <p>
              <strong>{sourceLabel(record.provider)}</strong>
              {record.reviewed_at && <> · <Timestamp value={record.reviewed_at} /></>}
            </p>
            {record.note && <p>{record.note}</p>}
            {record.reason && <p>{record.reason}</p>}
            {record.fiscal_year && (
              <p>
                FY{record.fiscal_year} Q{record.fiscal_quarter} ·{" "}
                {display(record.announced_date)} ·{" "}
                {sourceLabel(record.time_precision)}
              </p>
            )}
            {record.previous && (
              <p>
                修正前：FY{facts(record.previous).fiscal_year} Q
                {facts(record.previous).fiscal_quarter} ·{" "}
                {display(facts(record.previous).announced_date)} ·{" "}
                {sourceLabel(facts(record.previous).time_precision)}
              </p>
            )}
            {record.source_url && (
              <a
                className="text-link"
                href={record.source_url}
                target="_blank"
                rel="noreferrer"
              >
                查看这条记录的原始来源 ↗
              </a>
            )}
          </article>
        ))}
      </details>
    </Modal>
  );
}
function EarningsPanel() {
  const [params] = useSearchParams();
  const [ticker, setTicker] = useState(params.get("events") ?? "AAPL");
  const [selected, setSelected] = useState(ticker);
  const [editing, setEditing] = useState<Fact | null>(null);
  const [page, setPage] = useState(0);
  const q = useQuery({
    queryKey: ["earnings", selected],
    queryFn: () =>
      api<GenericOutput>(`/earnings?ticker=${encodeURIComponent(selected)}`),
  });
  return (
    <section className="panel" id="events">
      <div className="section-heading">
        <h2>财报事件核对</h2>
        <span>财政年度与自然年度分别记录</span>
      </div>
      <form
        className="analysis-form"
        onSubmit={(e) => {
          e.preventDefault();
          setSelected(ticker.trim().toUpperCase());
          setPage(0);
        }}
      >
        <label>
          股票代码
          <input value={ticker} onChange={(e) => setTicker(e.target.value)} />
        </label>
        <Button type="submit">查看事件</Button>
        <CollectionButton kind="earnings" tickers={[selected]}>
          获取与核对事件
        </CollectionButton>
      </form>
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.isPending ? (
        <Loading />
      ) : q.data?.items.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>财年 / 季度</th>
                <th>公告时间（美东）</th>
                <th>精度</th>
                <th>核对状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.slice(page * 50, (page + 1) * 50).map((e, i) => (
                <tr key={e.id ?? i} data-event-id={e.id}>
                  <td>
                    {display(e.fiscal_year)} / Q
                    {display(e.fiscal_quarter ?? e.quarter)}
                  </td>
                  <td>
                    {e.announced_at
                      ? <Timestamp value={e.announced_at} />
                      : display(e.announced_date)}
                  </td>
                  <td>{sourceLabel(e.time_precision)}</td>
                  <td>
                    {sourceLabel(e.status ?? e.verification_status)}
                    {e.is_primary === false && <small> · 辅助公告</small>}
                    {e.legacy_combined_source && <small> · 旧版合并来源待分项复核</small>}
                  </td>
                  <td>
                    <Button variant="ghost" onClick={() => setEditing(e)}>
                      核对 / 修正
                    </Button>
                    {e.source_url && (
                      <a
                        className="text-link"
                        href={e.source_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        来源 ↗
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          title="还没有本地财报事件"
          description="获取候选并核对，或在 CSV 导入中提供有来源的事件。"
        />
      )}
      {(q.data?.items.length ?? 0) > 50 && (
        <div className="pagination">
          <Button disabled={!page} onClick={() => setPage((p) => p - 1)}>
            上一页事件
          </Button>
          <span>
            第 {page + 1} 页 · 共 {q.data!.items.length} 个事件
          </span>
          <Button
            disabled={(page + 1) * 50 >= q.data!.items.length}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页事件
          </Button>
        </div>
      )}
      {q.data?.data && (
        <p className="panel-footer">
          {display(
            q.data.data.coverage ?? q.data.data.message ?? q.data.data.notice,
          )}
        </p>
      )}
      {editing && (
        <EventEditor
          event={editing}
          ticker={selected}
          close={() => setEditing(null)}
        />
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
  const storage = facts(q.data?.storage);
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>容量与备份</h2>
        <div className="button-row">
          <Button busy={cleanup.isPending} onClick={() => cleanup.mutate()}>
            清理未引用缓存
          </Button>
        </div>
      </div>
      <ErrorNotice error={q.error} />
      <ErrorNotice error={cleanup.error} />
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
          <strong>{storage.inventory_status === "fresh" && typeof storage.paths?.evidence?.allocated_bytes === "number" ? `${formatNumber(storage.paths.evidence.allocated_bytes / 1024 ** 2)} MiB` : "未知"}</strong>
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
  const tab = params.get("events") ? "events" : (params.get("tab") ?? "tasks");
  const invalidate = useInvalidate();
  const targetBatch = params.get("batch"),
    targetEvents = params.get("events");
  useEffect(() => {
    if (targetBatch) window.scrollTo({ top: 0, behavior: "instant" });
    else if (targetEvents)
      document.getElementById("events")?.scrollIntoView({ block: "start" });
  }, [targetBatch, targetEvents]);
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
          ["events", "财报核对"],
          ["imports", "导入"],
          ["sources", "数据来源"],
          ["maintenance", "维护与偏好"],
        ].map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("tab", id);
              next.delete("events");
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
      {tab === "events" && <EarningsPanel />}
      {tab === "imports" && <ImportPanel />}
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
