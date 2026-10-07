import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { components } from "../generated/api";
import { ErrorNotice } from "./common";

type Range = components["schemas"]["PriceRangeView"];
export function PriceRangeSummary({ range }: { range: Range }) {
  return <div className="price-range" aria-label="行情目标范围">
    <p>{range.basis === "legacy_scope" ? "旧任务保存范围（目标与缓冲未分开）：" : "行情目标："}<time>{range.target_start_date}</time> → <time>{range.target_end_date}</time></p>
    <details>
      <summary>目标口径、收盘与计算范围</summary>
      <p>{range.explanation}</p>
      <p>本次计算/获取范围：{range.collection_start_date} → {range.collection_end_date}；已形成收盘截至 {range.completed_through}，不代表这些价格均已获取。</p>
      {range.buffer_explanation && <p>{range.buffer_explanation}</p>}
      <p>实际有效行情日期和缺口按每只证券另列；周末、休市、成立前和供应商缺失不伪造价格。</p>
    </details>
  </div>;
}

export function PriceRangePreview({ request }: { request: components["schemas"]["PriceRangeInput"] }) {
  const parameters = JSON.stringify(request);
  const query = useQuery({ queryKey: ["price-range", parameters],
    queryFn: () => api<Range>(`/price-range?parameters=${encodeURIComponent(parameters)}`),
    staleTime: 30_000, refetchOnWindowFocus: false, retry: false });
  return <div className="context-baseline">
    <ErrorNotice error={query.error} />
    {query.data ? <PriceRangeSummary range={query.data} /> : query.isPending ? <p>正在核对行情目标范围…</p> : null}
  </div>;
}
