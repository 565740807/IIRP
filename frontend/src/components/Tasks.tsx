import { Timestamp } from "./Timestamp";
import { useState } from "react";
import {
  CheckCircle2,
  ChevronRight,
  Circle,
  Pause,
  Play,
  RotateCcw,
  Square,
  Timer,
} from "lucide-react";
import {
  activeStatuses,
  JOB_PAGE_SIZE,
  statusLabels,
  useJob,
  useJobAction,
  useJobs,
  type Job,
  type JobDetail as JobDetailData,
} from "../api";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  Modal,
  useNotice,
} from "./ui";

export function JobStatusLabel({ job }: { job: Job }) {
  const Icon =
    job.status === "SUCCEEDED"
      ? CheckCircle2
      : job.status === "PAUSED" || job.status === "PAUSE_REQUESTED"
        ? Pause
        : activeStatuses.includes(job.status)
          ? Timer
          : Circle;
  return (
    <span className={`status status-${job.status.toLowerCase()}`}>
      <Icon size={14} />
      {statusLabels[job.status]}
    </span>
  );
}
export function JobActions({ job }: { job: Job }) {
  const action = useJobAction();
  const notice = useNotice();
  const execute = (value: "pause" | "resume" | "cancel" | "retry") =>
    action.mutate(
      { id: job.id, action: value },
      {
        onSuccess: (updated) =>
          notice({
            text:
              updated.control_notice ||
              `${updated.title}：${statusLabels[updated.status]}`,
          }),
        onError: (error) => notice({ text: error.message, error: true }),
      },
    );
  const canPause = ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(job.status);
  const canCancel =
    [...activeStatuses, "PAUSED"].includes(job.status) &&
    job.status !== "CANCEL_REQUESTED";
  const canRetry = ["FAILED", "PARTIAL"].includes(job.status);
  return (
    <div className="job-actions">
      {canPause && (
        <Button
          variant="ghost"
          busy={action.isPending}
          onClick={() => execute("pause")}
        >
          <Pause size={13} />
          暂停
        </Button>
      )}
      {job.status === "PAUSED" && (
        <Button
          variant="ghost"
          busy={action.isPending}
          onClick={() => execute("resume")}
        >
          <Play size={13} />
          恢复
        </Button>
      )}
      {canRetry && (
        <Button
          variant="ghost"
          busy={action.isPending}
          onClick={() => execute("retry")}
        >
          <RotateCcw size={13} />
          重试
        </Button>
      )}
      {canCancel && (
        <Button
          variant="ghost"
          busy={action.isPending}
          onClick={() => execute("cancel")}
        >
          <Square size={12} />
          取消
        </Button>
      )}
    </div>
  );
}
export function JobDetail({
  job,
  open,
  onClose,
}: {
  job: Job;
  open: boolean;
  onClose: () => void;
}) {
  const detail = useJob(job.id);
  const current: Job & Partial<JobDetailData> = detail.data ?? job;
  return (
    <Modal
      open={open}
      onOpenChange={onClose}
      title={current.title}
      description="状态与进度来自持久任务；刷新页面不会丢失任务。"
    >
      <div className="detail-summary">
        <JobStatusLabel job={current} />
        <JobActions job={current} />
      </div>
      <dl className="definition-list">
        <dt>已完成</dt>
        <dd>
          {current.progress_total ? `${current.progress_done} / ${current.progress_total} 项` : `已完成 ${current.progress_done} 项`}
        </dd>
        <dt>触发方式</dt>
        <dd>
          {current.trigger === "manual"
            ? "手动"
            : current.trigger === "automatic" || current.trigger === "scheduled"
              ? "自动"
              : current.trigger}
        </dd>
        <dt>开始时间</dt>
        <dd>{current.started_at ? <Timestamp value={current.started_at} /> : "任务已保存，尚未开始"}</dd>
        <dt>更新时间</dt>
        <dd><Timestamp value={current.updated_at} /></dd>
        <dt>尝试次数</dt>
        <dd>{current.attempts}</dd>
      </dl>
      {current.requested_action && (
        <div className="notice">
          已保存控制请求：
          {(
            {
              pause: "暂停",
              policy_pause: "暂停自动需求",
              resume: "恢复",
              cancel: "取消",
              retry: "重试",
            } as Record<string, string>
          )[current.requested_action] ?? current.requested_action}
          {current.status === "PAUSE_REQUESTED" ||
          current.status === "CANCEL_REQUESTED"
            ? "。后台将在安全检查点处理。"
            : "。该控制意图已持久保存。"}
        </div>
      )}
      {current.error && <ErrorNotice error={new Error(current.error)} />}
      {current.status === "CANCELLED" && (
        <p className="muted">
          已取消未完成工作；已完成的 {current.progress_done} 项仍然保留。
        </p>
      )}
      {current.control_notice && (
        <div className="notice notice-info">{current.control_notice}</div>
      )}
      {current.result && (
        <div className="result-box">
          <h3>执行结果</h3>
          <p>
            {typeof current.result.message === "string"
              ? current.result.message
              : "任务已完成，详细记录可展开诊断查看。"}
          </p>
          {typeof current.result.source_hash === "string" && (
            <a
              className="text-link"
              href={`/api/v1/sources/${encodeURIComponent(current.result.source_hash)}`}
              download
            >
              下载本次来源记录
            </a>
          )}
        </div>
      )}
      <ErrorNotice error={detail.error} retry={detail.refetch} />
      <details className="diagnostic">
        <summary>任务诊断</summary>
        <p>任务 ID：{current.id}</p>
        {detail.data ? (
          <pre>
            {JSON.stringify(
              { checkpoint: current.checkpoint, result: current.result },
              null,
              2,
            )}
          </pre>
        ) : (
          <Loading label="读取任务详情…" />
        )}
      </details>
    </Modal>
  );
}
export function TaskList({ compact = false }: { compact?: boolean }) {
  // Cursor stack: index 0 is the newest page; older pages follow next_cursor.
  const [cursors, setCursors] = useState<string[]>([""]);
  const page = cursors.length - 1;
  const query = useJobs(cursors[page]);
  const [selected, setSelected] = useState<Job | null>(null);
  return (
    <>
      <ErrorNotice error={query.error} retry={query.refetch} />
      {query.isPending ? (
        <Loading />
      ) : (
        query.data && (
          <>
            {!query.data.worker.online && (
              <div className="notice notice-warning">
                后台目前离线。任务会保留在队列，服务恢复后再继续。
              </div>
            )}
            {query.data.items.length === 0 ? (
              <EmptyState
                title="还没有后台任务"
                description="运行一次本地样例，即可验证任务进度、暂停与恢复。"
              />
            ) : compact ? (
              <div className="compact-tasks">
                {query.data.items.map((job) => (
                  <article key={job.id}>
                    <button
                      className="task-title"
                      onClick={() => setSelected(job)}
                    >
                      <span>{job.title}</span>
                      <ChevronRight size={16} />
                    </button>
                    <JobStatusLabel job={job} />
                    <span className="muted">
                      {job.progress_total ? `${job.progress_done} / ${job.progress_total} 项` : `已完成 ${job.progress_done} 项`}
                    </span>
                    <JobActions job={job} />
                  </article>
                ))}
              </div>
            ) : (
              <div className="table-scroll">
                <table className="task-table">
                  <thead>
                    <tr>
                      <th>任务</th>
                      <th>状态</th>
                      <th>进度</th>
                      <th>更新时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((job) => (
                      <tr key={job.id}>
                        <td>
                          <button
                            className="task-title"
                            onClick={() => setSelected(job)}
                          >
                            {job.title}
                            <ChevronRight size={14} />
                          </button>
                          <small>
                            {job.kind === "fixture_check"
                              ? "合成测试 · 不写入真实信息流"
                              : job.kind === "market_probe"
                                ? "单只股票 · 有界来源验证"
                                : "SEC · 有界来源验证"}
                          </small>
                          {job.control_notice && (
                            <small className="control-notice">
                              {job.control_notice}
                            </small>
                          )}
                        </td>
                        <td>
                          <JobStatusLabel job={job} />
                        </td>
                        <td className="numeric">
                          {job.progress_total ? `${job.progress_done} / ${job.progress_total}` : `已完成 ${job.progress_done}`}
                        </td>
                        <td className="time-cell">
                          <Timestamp value={job.updated_at} />
                        </td>
                        <td>
                          <JobActions job={job} />
                          {!activeStatuses.includes(job.status) &&
                            ["SUCCEEDED", "CANCELLED"].includes(job.status) && (
                              <Button
                                variant="ghost"
                                onClick={() => setSelected(job)}
                              >
                                {job.status === "CANCELLED"
                                  ? "查看记录"
                                  : "查看结果"}
                              </Button>
                            )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {(page > 0 || query.data.next_cursor) && (
              <div className="pagination">
                <span>第 {page + 1} 页 · 每页 {JOB_PAGE_SIZE} 个任务</span>
                <div className="button-row">
                  <Button
                    variant="ghost"
                    disabled={page === 0}
                    onClick={() => setCursors(cursors.slice(0, -1))}
                  >
                    较新的任务
                  </Button>
                  <Button
                    variant="ghost"
                    disabled={!query.data.next_cursor}
                    onClick={() =>
                      query.data?.next_cursor &&
                      setCursors([...cursors, query.data.next_cursor])
                    }
                  >
                    更早的任务
                  </Button>
                </div>
              </div>
            )}
          </>
        )
      )}
      {selected && (
        <JobDetail
          job={selected}
          open={!!selected}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}
