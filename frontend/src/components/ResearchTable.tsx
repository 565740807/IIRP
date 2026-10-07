import { useState } from "react";
import {
  display,
  percent,
  sourceLabel,
  type Fact,
} from "../api";
import { Button } from "./common";

export function ResearchTable({ data }: { data: Fact[] }) {
  const [page, setPage] = useState(0);
  const lastPage = Math.max(0, Math.ceil(data.length / 50) - 1);
  const currentPage = Math.min(page, lastPage);
  const shown = data.slice(currentPage * 50, (currentPage + 1) * 50);
  return (
    <>
      <p className="table-hint">明细表可横向滚动；聚焦表格后可用方向键阅读。</p>
      <div className="table-scroll" role="region" aria-label="研究明细表" tabIndex={0}>
        <table>
          <thead>
            <tr>
              <th>年份 / 分组</th>
              <th>实际交易范围</th>
              <th>基准日期</th>
              <th>累计变化</th>
              <th>收盘路径最高 / 最低</th>
              <th>最大收盘回撤幅度</th>
              <th>汇总样本资格</th>
              <th>当前窗口状态与缺口</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={`${r.year}-${i}`}>
                <td>
                  {display(r.year)}{" "}
                  <small>
                    {r.group === "current"
                      ? "当前对照"
                      : r.group === "observation"
                        ? "日期观察"
                        : "历史"}
                  </small>
                </td>
                <td>{`${display(r.actual_start)} → ${display(r.actual_end)}`}</td>
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
                </td>
                <td>
                  {percent(r.relative_high)} / {percent(r.relative_low)}
                </td>
                <td>{percent(r.max_drawdown).replace(/^\+/, "")}</td>
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
