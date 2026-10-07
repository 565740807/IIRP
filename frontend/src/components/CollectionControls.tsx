import { Timestamp } from "./Timestamp";
import { useMutation } from "@tanstack/react-query";
import * as Switch from "@radix-ui/react-switch";
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
import i18n from "../i18n";
import { Button, ErrorNotice, Loading, useNotice } from "./common";
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
                text: i18n.t(r.reused ? "ui.collection.reused" : "ui.collection.created", { title: r.batch.title }),
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
  market: "首页行情报价",
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
  const policies = query.data?.items.filter((x) => full || x.key === "sec") ?? [];
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
                {p.next_run_at && !p.blocked_reason && (
                  <> · 下次 <Timestamp value={p.next_run_at} /></>
                )}
              </p>
            )}
            {p.enabled && p.blocked_reason && (
              <p className="notice notice-warning" role="status">
                {p.blocked_reason}
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
      SEC 申报与交易永久保存；行情为 24 小时缓存，期间重复查看不再下载，过期后按需重新获取。来源不足会保留具体缺口。
    </p>
  );
}
