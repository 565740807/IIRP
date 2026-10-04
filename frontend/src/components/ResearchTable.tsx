import { Timestamp } from "./Timestamp";
import { useState } from "react";
import {
  display,
  facts,
  percent,
  sourceLabel,
  type Fact,
} from "../api";
import { Button } from "./ui";

function eventReview(row: Fact) {
  if (!row.event_id) return sourceLabel(row.status ?? "missing_event");
  if (row.precise === true) return "事件与时点已核对";
  if (row.verified !== true) return "事件待核对";
  if (row.time_precision === "intraday") return "盘中事件，仅供日线观察";
  return "日期已核对，时点待核对";
}

export function ResearchTable({ data, kind }: { data: Fact[]; kind: string }) {
  const [page, setPage] = useState(0);
  const lastPage = Math.max(0, Math.ceil(data.length / 50) - 1);
  const currentPage = Math.min(page, lastPage);
  const shown = data.slice(currentPage * 50, (currentPage + 1) * 50);
  const earnings = kind === "earnings";
  return (
    <>
      {earnings && (
        <p className="context-baseline">
          行情完整与事件核对分别显示。仅有日期的价格观察保留，精确统计只纳入后端确认合格的历史事件。
        </p>
      )}
      <p className="table-hint">明细表可横向滚动；聚焦表格后可用方向键阅读。</p>
      <div className="table-scroll" role="region" aria-label="研究明细表" tabIndex={0}>
        <table>
          <thead>
            <tr>
              <th>{earnings ? "财年 / 财季" : "年份 / 分组"}</th>
              <th>{earnings ? "公告日期与时点（美东）" : "实际交易范围"}</th>
              <th>基准日期</th>
              <th>累计变化</th>
              {earnings ? (
                <>
                  <th>开盘跳空</th>
                  <th>各窗口价格与覆盖</th>
                  <th>事件核对</th>
                </>
              ) : (
                <>
                  <th>收盘路径最高 / 最低</th>
                  <th>最大收盘回撤幅度</th>
                </>
              )}
              <th>汇总样本资格</th>
              <th>当前窗口状态与缺口</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr
                key={`${r.event_id ?? r.year ?? r.fiscal_year}-${r.quarter ?? ""}-${i}`}
              >
                <td>
                  {display(r.year ?? r.fiscal_year)}{" "}
                  {earnings ? `Q${r.quarter}` : ""}
                  <small>
                    {r.group === "current"
                      ? "当前对照"
                      : r.group === "observation"
                        ? "日期观察"
                        : "历史"}
                  </small>
                </td>
                <td>
                  {earnings
                    ? r.announced_at
                      ? <Timestamp value={r.announced_at} />
                      : display(r.announced_date)
                    : `${display(r.actual_start)} → ${display(r.actual_end)}`}
                  {earnings && r.time_precision && (
                    <small>{sourceLabel(r.time_precision)}</small>
                  )}
                </td>
                <td>{display(r.baseline_date)}</td>
                <td
                  className={
                    r.endpoint == null
                      ? "muted"
                      : Number(r.endpoint) >= 0
                        ? "trade-buy"
                        : "trade-sell"
                  }
                >
                  {percent(r.endpoint)}
                  {earnings && r.endpoint != null && r.precise !== true && (
                    <small>日期观察</small>
                  )}
                </td>
                {earnings ? (
                  <>
                    <td>{percent(r.opening_gap ?? r.open_gap ?? r.gap)}</td>
                    <td>
                      <details>
                        <summary>查看各窗口与实际结束日期</summary>
                        {Object.entries(facts(r.windows)).map(([k, v]) => (
                          <p key={k}>
                            {k} 个交易日：{percent(facts(v).cumulative)}
                            <small>
                              截至 {display(facts(v).end_date)} ·{" "}
                              {facts(v).complete === true
                                ? "价格完整"
                                : sourceLabel(facts(v).status)}
                            </small>
                            {facts(v).missing_dates?.length > 0 && (
                              <small>
                                缺少 {facts(v).missing_dates.length}{" "}
                                个交易日价格
                              </small>
                            )}
                          </p>
                        ))}
                      </details>
                    </td>
                    <td>{eventReview(r)}</td>
                  </>
                ) : (
                  <>
                    <td>
                      {percent(r.relative_high)} / {percent(r.relative_low)}
                    </td>
                    <td>{percent(r.max_drawdown).replace(/^\+/, "")}</td>
                  </>
                )}
                <td>
                  {r.group === "current"
                    ? "当前对照，不计入历史 N"
                    : r.eligible === true
                      ? "计入有效历史样本"
                      : r.eligible === false
                        ? "未计入历史汇总"
                        : "样本资格尚未提供"}
                </td>
                <td>
                  {r.group !== "current" && (r.window_status ?? r.status) === "in_progress" && r.eligible === true
                    ? "已完成历史同期截取"
                    : sourceLabel(r.window_status ?? r.status)}
                  {r.missing_dates?.length > 0 && (
                    <small>缺少 {r.missing_dates.length} 个交易日</small>
                  )}
                  {r.reason && <small>{sourceLabel(r.reason)}</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.length > 50 && (
        <div className="pagination">
          <Button
            disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)}
          >
            上一页
          </Button>
          <span>
            {currentPage + 1} / {lastPage + 1}
          </span>
          <Button
            disabled={currentPage === lastPage}
            onClick={() => setPage(currentPage + 1)}
          >
            下一页
          </Button>
        </div>
      )}
    </>
  );
}
export function FactTable({
  data,
  columns,
}: {
  data: Fact[];
  columns: { key: string; label: string }[];
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.slice(0, 200).map((r, i) => (
            <tr key={r.id ?? i}>
              {columns.map((c) => (
                <td key={c.key}>{display(r[c.key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
