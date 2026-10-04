import { Timestamp } from "../components/Timestamp";
import { quoteStatusLabel, quoteTime } from "../freshnessRefresh";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { sourceContext } from "../researchStorage";
import {
  api,
  formatNumber,
  quotePercent,
  rows,
  sourceLabel,
  type Fact,
} from "../api";
import {
  CollectionButton,
  CollectionStrip,
} from "../components/CollectionControls";
import { Feed } from "../components/Feed";
import { ErrorNotice, Loading } from "../components/ui";
export function HomePage() {
  const location = useLocation();
  const q = useQuery({
    queryKey: ["home"],
    queryFn: () => api<Fact>("/home"),
    refetchInterval: () => (document.hidden ? false : 3000),
  });
  return (
    <div className="home-page">
      <div className="page-heading">
        <div>
          <h1>市场概览</h1>
        </div>
        <CollectionButton kind="market_quotes">更新市场行情</CollectionButton>
      </div>
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.isPending ? (
        <Loading />
      ) : (
        q.data && (
          <div className="market-grid">
            {rows(q.data.market).map((m) => (
              <article className="market-card" key={m.symbol}>
                <Link
                  state={sourceContext(location)}
                  to={`/market/${encodeURIComponent(m.symbol)}`}
                >
                  <h2>{m.name}</h2>
                  <div className="market-value numeric">
                    {m.value == null ? "—" : formatNumber(m.value)}
                    {m.change_percent != null && (
                      <span
                        className={
                          Number(m.change_percent) >= 0
                            ? "trade-buy"
                            : "trade-sell"
                        }
                      >
                        {quotePercent(m.change_percent)}
                      </span>
                    )}
                  </div>
                </Link>
                <p>
                  {quoteStatusLabel(m.status) ?? sourceLabel(m.status)} ·{" "}
                  <Timestamp value={quoteTime(m)} />
                </p>
                <p>{m.delay || "来源尚未提供延迟说明"}</p>
                {m.refresh_notice && <p role="status">{m.refresh_notice}</p>}
                <p title={m.baseline}>
                  {m.baseline || "涨跌基准待确认"}
                  {m.baseline_date ? ` · ${m.baseline_date}` : ""}
                  {m.last_completed_session ? ` · 最近已完成交易日 ${m.last_completed_session}` : ""}
                </p>
              </article>
            ))}
          </div>
        )
      )}
      <CollectionStrip />
      <Feed />
    </div>
  );
}
export function InsiderPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Insider</h1>
          <p>把申报时间、实际交易时间与交易行为分清楚。</p>
        </div>
        <div className="button-row">
          <CollectionButton kind="sec_history">回补近 3 个月</CollectionButton>
          <CollectionButton kind="sec_latest" primary>
            获取最新申报
          </CollectionButton>
        </div>
      </div>
      <Feed />
    </>
  );
}
