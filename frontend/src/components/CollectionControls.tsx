import { Timestamp } from "./Timestamp";
import { useMutation } from "@tanstack/react-query";
import * as Switch from "@radix-ui/react-switch";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import {
  api,
  requestId,
  useCollection,
  useInvalidate,
  usePolicy,
  type CollectionInput,
  type StrategyInput,
} from "../api";
import { Button, ErrorNotice, Loading, useNotice } from "./ui";
export function CollectionButton({
  kind,
  tickers,
  issuer_id,
  owner_id,
  historical_years,
  history_months,
  primary = false,
  children,
}: {
  kind: CollectionInput["kind"];
  tickers?: string[];
  issuer_id?: string;
  owner_id?: string;
  historical_years?: number;
  history_months?: number;
  primary?: boolean;
  children: ReactNode;
}) {
  const mutation = useCollection();
  const notice = useNotice();
  return (
    <Button
      variant={primary ? "primary" : "secondary"}
      busy={mutation.isPending}
      onClick={() =>
        mutation.mutate(
          {
            request_id: requestId(),
            kind,
            tickers,
            issuer_id,
            owner_id,
            historical_years,
            history_months,
          },
          {
            onSuccess: (r) =>
              notice({
                text: `${r.reused ? "已复用" : "已创建"}批次：${r.batch.title}。可在数据与任务查看进度。`,
              }),
            onError: (e) => notice({ text: e.message, error: true }),
          },
        )
      }
    >
      {children}
    </Button>
  );
}
const names: Record<string, string> = {
  sec: "SEC 申报",
  market: "活跃证券行情",
  earnings: "财报事件",
  backup: "每日备份",
  maintenance: "存储维护",
};
export function PolicyControl({ full = false }: { full?: boolean }) {
  const query = usePolicy();
  const invalidate = useInvalidate();
  const notice = useNotice();
  const mutation = useMutation({
    mutationFn: (input: StrategyInput) =>
      api("/collection-policy", "PATCH", input),
    onSuccess: invalidate,
    onError: (e) => notice({ text: e.message, error: true }),
  });
  if (query.isPending) return <Loading label="读取自动更新策略…" />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={query.refetch} />;
  const policies =
    query.data?.items.filter((x) => full || x.key === "sec") ?? [];
  return (
    <div className={full ? "panel policy-list" : "policy-inline"}>
      {full && (
        <div className="section-heading">
          <h2>自动更新</h2>
          <span>分别控制来源；手动获取不改变开关</span>
        </div>
      )}
      {policies.map((p) => (
        <div className={full ? "policy-panel" : "policy-switch"} key={p.key}>
          <div>
            <label htmlFor={`policy-${p.key}`}>{names[p.key] ?? p.key}</label>
            {full && (
              <p>
                {p.enabled ? "自动更新已开启" : "自动更新已关闭"}
                {p.next_run_at && <> · 下次 <Timestamp value={p.next_run_at} /></>}
              </p>
            )}
          </div>
          <div className="policy-switch">
            <span>{p.enabled ? "已开启" : "已关闭"}</span>
            <Switch.Root
              id={`policy-${p.key}`}
              className="switch"
              checked={p.enabled}
              disabled={mutation.isPending}
              onCheckedChange={(enabled) =>
                mutation.mutate({ key: p.key as StrategyInput["key"], enabled })
              }
            >
              <Switch.Thumb className="switch-thumb" />
            </Switch.Root>
          </div>
        </div>
      ))}
    </div>
  );
}
export function ScopeNotice() {
  return (
    <p className="notice notice-info">
      按所选范围持久获取并复用已验证数据；缩小研究范围不会删除历史。来源不足会保留具体缺口。
    </p>
  );
}
export function CollectionStrip() {
  return (
    <div className="collection-strip">
      <strong>Insider 更新</strong>
      <div className="button-row">
        <CollectionButton kind="sec_latest" primary>
          获取最新申报
        </CollectionButton>
        <PolicyControl />
      </div>
      <Link to="/data" className="strip-help">
        查看覆盖与任务
      </Link>
    </div>
  );
}
