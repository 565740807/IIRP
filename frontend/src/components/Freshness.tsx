import { Timestamp } from "./Timestamp";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { api, formatTime, type Fact } from "../api";
import {
  createFreshnessRefresher,
  marketPageVisible,
  newestFreshness,
  type RefreshReason,
} from "../freshnessRefresh";
import { Button, ErrorNotice } from "./ui";

function describeTarget(target: Fact) {
  return [
    target.issuer_name || target.symbol,
    target.accession ? "申报 " + target.accession : "",
    target.form ? "Form " + target.form : "",
    target.start_date && target.end_date
      ? target.start_date + " — " + target.end_date
      : "",
    target.accepted_at ? "SEC 接受 " + formatTime(target.accepted_at) : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

export function FreshnessStrip() {
  const client = useQueryClient();
  const location = useLocation();
  const marketVisible = useRef(marketPageVisible(location.pathname));
  marketVisible.current = marketPageVisible(location.pathname);
  const [checking, setChecking] = useState<RefreshReason | null>(null);
  const [refreshError, setRefreshError] = useState<Error | null>(null);
  const controller = useRef<ReturnType<
    typeof createFreshnessRefresher<Fact>
  > | null>(null);
  const query = useQuery({
    queryKey: ["freshness"],
    queryFn: async () => {
      const value = await api<Fact>("/freshness");
      return newestFreshness(client.getQueryData<Fact>(["freshness"]), value);
    },
    refetchInterval: () => (document.hidden ? false : 2000),
  });
  useEffect(() => {
    const current = createFreshnessRefresher<Fact>({
      visible: () => !document.hidden,
      marketVisible: () => marketVisible.current,
      request: (payload) => api<Fact>("/freshness/ensure", "POST", payload),
      started: (reason) => {
        setChecking(reason);
        setRefreshError(null);
      },
      received: (value) => {
        client.setQueryData<Fact>(["freshness"], (previous) =>
          newestFreshness(previous, value),
        );
        void client.invalidateQueries({ queryKey: ["home"] });
        void client.invalidateQueries({ queryKey: ["market"] });
        void client.invalidateQueries({ queryKey: ["feed-updates"] });
        void client.invalidateQueries({ queryKey: ["batches"] });
      },
      failed: (error) =>
        setRefreshError(
          error instanceof Error
            ? error
            : new Error("暂时无法检查最新数据，请重试。"),
        ),
      settled: () => setChecking(null),
    });
    controller.current = current;
    const resume = () => void current.ensure("resume");
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    const timer = window.setInterval(
      () => void current.ensure("visible"),
      30000,
    );
    return () => {
      current.dispose();
      controller.current = null;
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
      window.clearInterval(timer);
    };
  }, [client]);
  useEffect(() => {
    void controller.current?.ensure("open");
  }, [location.pathname]);
  const market = query.data?.sources?.market;
  return (
    <details className="freshness-strip" aria-label="数据更新状态">
      <summary>
        {checking ? "正在检查最新数据 · " : ""}
        最近报价
        {market?.source_time
          ? `截至 ${formatTime(market.source_time)}`
          : `日期 ${market?.data_as_of ?? "待获取"}`}{" "}
        · {market?.stage ?? "正在读取"} · Insider{" "}
        {query.data?.sources?.sec?.stage ?? "正在读取"} · 查看更新详情
      </summary>
      <div className="freshness-sources">
        {(["sec", "market"] as const).map((key) => {
          const source = query.data?.sources?.[key];
          return (
            <div key={key}>
              <strong>{key === "sec" ? "SEC Insider" : "市场报价"}</strong>
              <span role="status">
                {checking
                  ? "正在检查最新数据，已有内容可继续阅读…"
                  : (source?.stage ?? "正在检查更新状态…")}
              </span>
              {source && (
                <small>
                  最近完成来源检查 <Timestamp value={source.last_checked_at} />
                  {key === "sec" && (
                    <>
                      {" "}
                      · 美东今日已发现 {source.discovered} 份 · 已发布{" "}
                      {source.published} 份
                    </>
                  )}
                  {(source.source_time || source.data_as_of) && (
                    <>
                      {" "}
                      · {key === "market"
                        ? "最新报价时间"
                        : "已发布数据截至"}{" "}
                      <Timestamp
                        value={source.source_time || source.data_as_of}
                      />
                    </>
                  )}
                  {key === "market" && <> · 各品种时间与延迟见行情卡</>}
                  {source.retry_at && (
                    <>
                      {" "}
                      · 下次重试 <Timestamp value={source.retry_at} />
                    </>
                  )}
                </small>
              )}
              {!!source?.progress?.length && (
                <small>
                  {source.progress[0].stage} ·{" "}
                  {describeTarget(source.progress[0].target ?? {})}
                </small>
              )}
            </div>
          );
        })}
      </div>
      <div className="button-row">
        <Button
          variant="ghost"
          busy={checking === "manual"}
          onClick={() => void controller.current?.ensure("manual")}
        >
          检查最新
        </Button>
        <Link to="/data" className="text-link">
          进度与设置
        </Link>
      </div>
      <ErrorNotice
        error={refreshError ?? query.error}
        retry={() => void controller.current?.ensure("resume")}
      />
    </details>
  );
}
