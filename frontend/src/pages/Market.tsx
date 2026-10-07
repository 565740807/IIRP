import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { Timestamp } from "../components/Timestamp";
import { quoteStatusLabel, quoteTime } from "../freshnessRefresh";
import { api, display, facts, formatNumber, quotePercent, sourceLabel, type GenericOutput } from "../api";
import { ResearchChart } from "../components/ResearchChart";
import { sourceContext, sourceHref } from "../researchStorage";
import { ErrorNotice, Loading } from "../components/common";

// Index detail page opened from the market strip; restyled with the analysis pages (S6).
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
              <p className="panel-body muted">{quote.chart_note || "供应商日线序列，当日数据可能尚未收盘；不是分时走势。"}</p>
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
