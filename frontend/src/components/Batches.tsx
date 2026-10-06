import { useResearchBatch } from "../researchQueries";
import { Timestamp } from "./Timestamp";
import { useEffect, useState } from "react";
import { acquiredSessionsText, taskSource, taskStage } from "../taskPresentation";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  api,
  activeStatuses,
  display,
  facts,
  formatNumber,
  formatTime,
  marketName,
  sourceLabel,
  useBatches,
  useBatchAction,
  type Batch,
  type GenericOutput,
} from "../api";
import { Button, EmptyState, ErrorNotice, Loading, useNotice } from "./ui";
import { PriceRangeSummary } from "./PriceRange";
function AnalysisLink({
  batch,
  onNavigate,
}: {
  batch: Batch;
  onNavigate?: () => void;
}) {
  if (!batch.analysis_id) return null;
  const purpose =
    batch.kind === "event_dates"
      ? batch.params.event_kind === "earnings" ? "earnings" : "events"
      : String(batch.params.purpose ?? "monthly");
  const kind = ["monthly", "interval", "earnings", "events"].includes(purpose)
    ? purpose
    : "monthly";
  return (
    <Link
      className="text-link"
      to={"/analysis/" + kind + "?a=" + batch.analysis_id}
      onClick={onNavigate}
    >
      查看研究结果 →
    </Link>
  );
}
function BatchExecution({ batch }: { batch: Batch }) {
  const [opened, setOpened] = useState(batch.kind === "maintenance");
  const [cursors, setCursors] = useState([""]);
  const [page, setPage] = useState(0);
  const query = useQuery({
    queryKey: ["batch-jobs", batch.id, cursors[page]],
    queryFn: () =>
      api<GenericOutput>(
        `/batches/${batch.id}/jobs?${new URLSearchParams({ cursor: cursors[page], limit: "50" })}`,
      ),
    enabled: opened,
    refetchInterval:
      opened && activeStatuses.includes(batch.status) ? 2000 : false,
  });
  return (
    <details
      className="result-notes"
      open={opened}
      onToggle={(e) => setOpened(e.currentTarget.open)}
    >
      <summary>执行进度与结果</summary>
      <ErrorNotice error={query.error} retry={query.refetch} />
      {query.isPending && opened && <Loading />}
      {query.data?.items.map((job) => {
        const result = facts(job.result);
        return (
          <article className="scope-item" key={job.id}>
            <div className="scope-title">
              <strong>
                {job.kind === "maintenance_clean"
                  ? "缓存与日志维护"
                  : job.kind === "maintenance_backup"
                    ? "一致备份"
                    : job.title}
              </strong>
              <span>{sourceLabel(job.status)}</span>
              <span>
                {job.progress_total
                  ? `${job.progress_done}/${job.progress_total}`
                  : `已完成 ${job.progress_done}，总量待确定`}
              </span>
            </div>
            {job.error && <p className="reason">{job.error}</p>}
            {job.kind === "maintenance_clean" && job.result ? (
              <div className="panel-body">
                <p>
                  已清理 {display(result.analysis_results_removed)}{" "}
                  份未引用结果，释放{" "}
                  {formatNumber(
                    Number(result.analysis_bytes_reclaimed ?? 0) / 1024 ** 2,
                  )}{" "}
                  MiB；受保护结果 {display(result.analysis_protected)} 份。
                </p>
                <p>
                  过期阅读版本 {display(result.reading_sessions_removed)} 份 ·
                  历史数据
                  {result.research_history_preserved ? "已保留" : "状态待核对"}
                </p>
                {result.cache_target_exceeded_by_protected_results && (
                  <p>已发布研究版本超过缓存目标，仍全部保留；需评估后续容量与归档。</p>
                )}
              </div>
            ) : (
              job.result && <p>{display(job.result)}</p>
            )}
          </article>
        );
      })}
      {(page > 0 || query.data?.data.next_cursor) && (
        <div className="pagination">
          <Button disabled={!page} onClick={() => setPage((p) => p - 1)}>
            上一页执行记录
          </Button>
          <span>第 {page + 1} 页</span>
          <Button
            disabled={!query.data?.data.next_cursor}
            onClick={() => {
              setCursors((c) => [
                ...c.slice(0, page + 1),
                query.data!.data.next_cursor,
              ]);
              setPage((p) => p + 1);
            }}
          >
            下一页执行记录
          </Button>
        </div>
      )}
    </details>
  );
}
export function BatchActions({ batch }: { batch: Batch }) {
  const mutation = useBatchAction();
  const notice = useNotice();
  const [continued, setContinued] = useState<string | null>(null);
  const controls = [
    ...(["QUEUED", "RUNNING", "RETRY_WAIT", "WAITING"].includes(batch.status)
      ? [{ action: "pause" as const, label: "暂停" }]
      : []),
    ...(batch.status === "PAUSED"
      ? [{ action: "resume" as const, label: "继续" }]
      : []),
    ...(["FAILED", "PARTIAL"].includes(batch.status)
      ? [{ action: "retry_failed" as const, label: "重试失败" }]
      : []),
    ...(batch.status === "CANCELLED"
      ? [{ action: "continue_remaining" as const, label: "继续剩余范围" }]
      : []),
    ...([...activeStatuses, "PAUSED", "PARTIAL"].includes(batch.status) &&
    batch.status !== "CANCEL_REQUESTED"
      ? [{ action: "cancel" as const, label: "取消" }]
      : []),
  ];
  return (
    <div className="job-actions">
      {controls.map((x) => (
        <Button
          key={x.action}
          variant="ghost"
          busy={mutation.isPending}
          onClick={() =>
            mutation.mutate(
              { id: batch.id, action: x.action },
              {
                onSuccess: (r) => {
                  if (x.action === "continue_remaining")
                    setContinued(r.batch_id);
                  notice({
                    text:
                      x.action === "continue_remaining"
                        ? `已建立新批次 ${batchTitle(r.batch)}，原取消记录保留。`
                        : `${batchTitle(r.batch)}：${sourceLabel(r.batch.status)}`,
                  });
                },
                onError: (e) => notice({ text: e.message, error: true }),
              },
            )
          }
        >
          {x.label}
        </Button>
      ))}
      <ErrorNotice error={mutation.error} />
      {continued && (
        <a className="text-link" href={`/data?batch=${continued}`}>
          查看新批次 →
        </a>
      )}
    </div>
  );
}
function taskReason(value: unknown) {
  return String(value ?? "")
    .split(/;\s*/)
    .map((part) => {
      const label = sourceLabel(part);
      return label === part && /^[a-z][a-z0-9_]+$/.test(part)
        ? "资料尚未满足分析条件，请查看诊断与来源"
        : label;
    })
    .join("；");
}
function taskTarget(item: Batch["items"][number]) {
  const symbol = facts(item.progress?.current_target).symbol;
  return typeof symbol === "string" ? symbol : item.symbol;
}
function taskTargetText(value: unknown) {
  const target = facts(value);
  return [
    target.symbol ? marketName(String(target.symbol)) : null,
    target.start_date || target.end_date
      ? `${display(target.start_date)} → ${display(target.end_date)}`
      : null,
    target.form,
    target.accepted_at
      ? formatTime(String(target.accepted_at))
      : target.filing_date,
    target.accession,
  ]
    .filter(Boolean)
    .join(" · ");
}
function batchTitle(batch: Batch) {
  const labels: Record<string, string> = {
    monthly: "月度分析",
    interval: "区间分析",
    event_dates: "事件分析",
    research: "历史行情",
    market_history: "历史行情",
  };
  return batch.title.replace(
    /\b(monthly|interval|event_dates|research|market_history)\b/g,
    (value) => labels[value],
  );
}
export function BatchScopes({ batch }: { batch: Batch }) {
  return (
    <div className="scope-list">
      {batch.price_range && <PriceRangeSummary range={batch.price_range} />}
      {batch.items.map((item) => (
        <div key={item.id} className="scope-item">
          <div className="scope-title">
            <strong>{marketName(item.symbol)}</strong>
            <span
              className={`status status-${String(item.progress?.activity_status ?? item.status).toLowerCase()}`}
            >
              {sourceLabel(item.progress?.activity_status ?? item.status)}
            </span>
            {!!item.jobs_total && <span>已完成 {item.jobs_done}/{item.jobs_total} 项</span>}
          </div>
          {batch.kind !== "maintenance" &&
            (item.coverage.start_date ||
              item.coverage.end_date ||
              item.security_id) && (
              <p>
                {batch.price_range ? "计算/获取范围：" : "已保存范围："}{item.coverage.start_date ?? "起点待确定"} →{" "}
                {item.coverage.end_date ?? "最新"}
                {item.security_id && item.progress?.acquired_sessions == null &&
                  batch.kind === "market_history" && (
                    <>
                      {" "}
                      · 已获取 {item.coverage.valid_sessions} 个有效交易日
                      {item.coverage.expected_sessions ? ` / 所需 ${item.coverage.expected_sessions} 日` : ""}
                      {" · "}
                      {sourceLabel(item.coverage.status)}
                    </>
                  )}
              </p>
            )}
          {batch.kind === "market_history" && <p>已取得有效行情：{item.coverage.first_valid_date ?? "尚无"} → {item.coverage.last_valid_date ?? "尚无"}</p>}
          {item.progress && (
            <div className="batch-progress" role="status">
              <p>
                {taskReason(taskStage(item.progress.stage, batch.kind, item.status))} ·{" "}
                {taskTargetText(item.progress.current_target)}
              </p>
              {acquiredSessionsText(item.progress.acquired_sessions, item.progress.expected_sessions) && (
                <p>{acquiredSessionsText(item.progress.acquired_sessions, item.progress.expected_sessions)}</p>
              )}
              {item.progress.event_count != null && (
                <p>
                  事件已就绪 {display(item.progress.ready_events)} /{" "}
                  {display(item.progress.event_count)}；仅采集各事件附近窗口
                </p>
              )}
              {item.progress.discovered != null && (
                <p>
                  发现 {display(item.progress.discovered)} 份 · 解析{" "}
                  {display(item.progress.parsed)} 份
                </p>
              )}
              {typeof item.progress.last_progress_at === "string" && <small>
                最近进展{" "}
                <Timestamp value={typeof item.progress.last_progress_at === "string" ? item.progress.last_progress_at : null} />
                {typeof item.progress.retry_at === "string" && (
                  <> · 下次重试 <Timestamp value={item.progress.retry_at} /></>
                )}
              </small>}
              {item.progress.history_boundary_reached === true && (
                <p>已到设置的历史边界；范围内不足最近 N 条时保留实际数量。</p>
              )}
            </div>
          )}
          {item.wait_reason && item.wait_reason !== item.progress?.stage && (
            <p className="reason">{taskReason(item.wait_reason)}</p>
          )}
          {!!item.coverage.reasons?.filter(
            (reason) =>
              reason !== item.wait_reason && reason !== item.progress?.stage,
          ).length && (
            <p className="reason">
              {item.coverage.reasons
                .filter(
                  (reason) =>
                    reason !== item.wait_reason &&
                    reason !== item.progress?.stage,
                )
                .map(taskReason)
                .join("；")}
            </p>
          )}
          {item.wait_reason &&
            taskReason(item.wait_reason) !== item.wait_reason && (
              <details>
                <summary>来源诊断</summary>
                <p>{item.wait_reason}</p>
              </details>
            )}
          {!!item.coverage.missing_dates?.length && (
            <details>
              <summary>
                缺少 {item.coverage.missing_dates.length} 个交易日
              </summary>
              <p>{item.coverage.missing_dates.join("、")}</p>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
export function BatchPanel({ id }: { id: string }) {
  const q = useResearchBatch(id);
  return (
    <>
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.isPending && <Loading label="读取任务范围…" />}
      {q.data && (
        <div className="panel">
          <div className="section-heading">
            <h2>{batchTitle(q.data.batch)}</h2>
            <span>
              {sourceLabel(q.data.batch.activity_status ?? q.data.batch.status)}
            </span>
            <div className="button-row">
              <BatchActions batch={q.data.batch} />
            </div>
          </div>
          {q.data.batch.status === "PAUSED" && (
            <p className="panel-body muted">
              本任务已暂停，下方保留范围与进度；点击“继续”后恢复执行。
            </p>
          )}
          {q.data.batch.status === "CANCELLED" && (
            <p className="panel-body muted">
              本任务已取消，下方保留范围与进度；已完成结果仍可读取。
            </p>
          )}
          <BatchScopes batch={q.data.batch} />
          {q.data.batch.analysis_id && (
            <div className="panel-body">
              <AnalysisLink batch={q.data.batch} />
            </div>
          )}
          <BatchExecution key={q.data.batch.id} batch={q.data.batch} />
        </div>
      )}
    </>
  );
}
export function BatchList({ compact = false, initialCategory, onCategoryChange }: {
  compact?: boolean;
  initialCategory?: string;
  onCategoryChange?: (category: string) => void;
}) {
  const [category, setCategory] = useState(initialCategory ?? (compact ? "active" : "running"));
  const [cursors, setCursors] = useState([""]);
  const [page, setPage] = useState(0);
  const [policy, setPolicy] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const q = useBatches(category, cursors[page], policy, compact ? "personal" : "all");
  useEffect(() => {
    if (initialCategory) {
      setCategory(initialCategory);
      setPage(0);
      setCursors([""]);
      setPolicy("");
      setSelected(null);
    }
  }, [initialCategory]);
  const tabs = compact ? [["active", "进行中"], ["attention", "需要处理"]] : [
    ["running", "进行中"],
    ["waiting", "等待中"],
    ["attention", "需要处理"],
    ["history", "历史记录"],
  ];
  function choose(value: string) {
    setCategory(value);
    onCategoryChange?.(value);
    setPage(0);
    setCursors([""]);
    setPolicy("");
    setSelected(null);
  }
  return (
    <div className={compact ? "compact-tasks" : "task-center"}>
      <nav className="tabs" aria-label="任务分类">
        {tabs.map(([value, label]) => (
          <button
            key={value}
            className={category === value ? "active" : ""}
            onClick={() => choose(value)}
          >
            {label} {value === "active" ? (q.data ? (q.data.counts?.running ?? 0) + (q.data.counts?.waiting ?? 0) : "") : (q.data?.counts?.[value] ?? "")}
          </button>
        ))}
      </nav>
      {policy && (
        <p>
          正在查看此自动计划在本分类的各轮任务{" "}
          <Button
            variant="ghost"
            onClick={() => {
              setPolicy("");
              setPage(0);
              setCursors([""]);
            }}
          >
            返回归组列表
          </Button>
        </p>
      )}
      <ErrorNotice error={q.error} retry={q.refetch} />
      {q.data?.items.some((batch) => batch.history_count > 1) && (
        <p className="panel-body muted">
          自动任务按目标与来源归组；数量包含保留的各轮记录。卡片操作仅作用于所示轮次，展开可分别查看。
        </p>
      )}
      {q.isPending ? (
        <Loading />
      ) : !q.data?.items.length ? (
        <EmptyState
          title={`暂无${tabs.find(([value]) => value === category)?.[1]}任务`}
          description={compact ? "已完成的结果与自动采集记录可在“数据与任务”查看。" : "已保存结果可继续阅读；其他状态在对应页签查看。"}
        />
      ) : (
        q.data.items.map((batch) => {
          const active =
            batch.items.find(
              (item) => item.progress?.activity_status === "RUNNING",
            ) ??
            batch.items.find((item) =>
              ["RUNNING", "WAITING", "QUEUED", "RETRY_WAIT"].includes(
                String(item.progress?.activity_status ?? item.status),
              ),
            ) ??
            batch.items[0];
          const ready = batch.items.filter(
            (item) => item.status === "READY",
          ).length;
          return (
            <article className="task-card panel-body" key={batch.id}>
              <div className="section-heading">
                <button
                  className="task-title"
                  onClick={() =>
                    setSelected(selected === batch.id ? null : batch.id)
                  }
                >
                  {batchTitle(batch)}
                </button>
                <span>
                  {sourceLabel(batch.activity_status ?? batch.status)}
                </span>
              </div>
              {batch.trigger === "automatic" && <p className="muted">自动更新 · {taskSource(batch.kind)} · {batch.items.map((item) => marketName(item.symbol)).join("、")}</p>}
              <p>
                {active
                  ? ["PAUSED", "CANCELLED"].includes(batch.status)
                    ? `${marketName(active.symbol)} · 保留的范围与进度`
                    : `${marketName(taskTarget(active))} · ${taskReason(taskStage(active.progress?.stage, batch.kind, active.status))}${taskTarget(active) !== active.symbol ? `（${marketName(active.symbol)} 研究）` : ""}`
                  : [
                        "SUCCEEDED",
                        "CANCELLED",
                        "FAILED",
                        "PARTIAL",
                        "PAUSED",
                      ].includes(batch.status)
                    ? "此任务未提供逐项范围记录"
                    : "等待规划所需范围"}
                {batch.items.length
                  ? ` · 已完成 ${ready}/${batch.items.length} ${batch.items.some((item) => item.security_id) ? "只股票" : "个目标"}`
                  : ""}
              </p>
              {active?.wait_reason &&
                active.wait_reason !== active.progress?.stage && (
                  <p className="reason">{taskReason(active.wait_reason)}</p>
                )}
              {!!active?.progress?.retry_at && (
                <p>
                  自动重试时间：<Timestamp value={String(active.progress.retry_at)} />
                  ；已有结果仍可读取。
                </p>
              )}
              <div className="button-row">
                <BatchActions batch={batch} />
                <AnalysisLink batch={batch} />
                <Button
                  variant="ghost"
                  onClick={() =>
                    setSelected(selected === batch.id ? null : batch.id)
                  }
                >
                  {selected === batch.id ? "收起任务" : "查看详情"}
                </Button>
              </div>
              {batch.history_count > 1 && (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setPolicy(batch.policy_key ?? "");
                    setPage(0);
                    setCursors([""]);
                  }}
                >
                  此目标在本分类共 {batch.history_count} 轮 ·{" "}
                  {category === "history" ? "查看该来源全部历史" : "查看该来源各轮任务"}
                </Button>
              )}
              {selected === batch.id && <BatchPanel id={batch.id} />}
              <small>最近活动 <Timestamp value={batch.updated_at} /></small>
            </article>
          );
        })
      )}
      {(page > 0 || q.data?.next_cursor) && (
        <div className="pagination">
          <Button disabled={!page} onClick={() => setPage(page - 1)}>
            上一页任务
          </Button>
          <span>第 {page + 1} 页</span>
          <Button
            disabled={!q.data?.next_cursor}
            onClick={() => {
              setCursors([...cursors.slice(0, page + 1), q.data!.next_cursor!]);
              setPage(page + 1);
            }}
          >
            下一页任务
          </Button>
        </div>
      )}
    </div>
  );
}
