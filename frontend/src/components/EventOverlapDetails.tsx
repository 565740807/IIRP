import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { overlapOptions } from "../researchQueries";
import type { components } from "../generated/api";
import { Button } from "./ui";

type Summary = components["schemas"]["EventOverlapSummary"];

// Parent keys this component by analysis/result/event, so a late response can
// populate only its old query cache, never replace the newly selected page.
export function EventOverlapDetails({
  analysisId,
  resultId,
  eventKey,
  summary,
  legacyIds,
}: {
  analysisId?: string;
  resultId?: string;
  eventKey: string;
  summary?: Summary;
  legacyIds?: string[];
}) {
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const query = useQuery({
    ...overlapOptions(analysisId ?? "", resultId ?? "", eventKey, cursor),
    enabled: open && !!analysisId && !!resultId,
  });
  const total = summary?.total ?? legacyIds?.length ?? 0;
  const preview = summary?.preview_event_ids ?? legacyIds?.slice(0, 10) ?? [];
  if (!summary && !legacyIds) return null;
  if (!total) return <p>价格窗口重叠事件：0 次。</p>;
  return (
    <div className="event-overlap-details">
      <p className="reason">
        价格窗口与另外 {total} 次事件重叠，不能视为独立样本。
      </p>
      <p>
        重叠事件预览（{preview.length}/{total}）：{preview.join("、")}
      </p>
      {analysisId && resultId && (
        <Button onClick={() => setOpen(!open)}>
          {open ? "收起重叠详情" : "查看重叠详情"}
        </Button>
      )}
      {open && (
        <div aria-live="polite">
          {query.isFetching && <p role="status">正在读取重叠详情…</p>}
          {query.isError && (
            <p role="alert">
              重叠详情读取失败：{query.error.message}{" "}
              <Button onClick={() => query.refetch()}>重试重叠详情</Button>
            </p>
          )}
          {query.data && !query.isError && (
            <>
              <p>
                重叠详情 {query.data.offset + 1}—
                {query.data.offset + query.data.items.length} /{" "}
                {query.data.total} · 结果 {query.data.result_id.slice(0, 8)}
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>事件</th>
                      <th>事件标识</th>
                      <th>重叠判定窗口</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((item) => (
                      <tr key={item.event_key}>
                        <td>{item.label}</td>
                        <td>{item.event_key}</td>
                        <td>
                          {item.start_date ?? "未记录"} →{" "}
                          {item.end_date ?? "未记录"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pagination">
                <Button
                  disabled={!cursor || query.isFetching}
                  onClick={() => setCursor(null)}
                >
                  重叠详情首页
                </Button>
                <Button
                  disabled={!query.data.next_cursor || query.isFetching}
                  onClick={() => setCursor(query.data!.next_cursor)}
                >
                  重叠详情下一页
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
