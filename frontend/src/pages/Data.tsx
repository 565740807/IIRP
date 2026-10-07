import { useMemo, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { AlertTriangle, CheckCircle2, Info, LoaderCircle, RotateCw, Terminal } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatAgo, formatEt, formatLocal } from "@/lib/format";
import { usePageVisible } from "@/lib/refresh";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { actions, resultHref } from "@/components/shell/Tasks";

type Batch = Schemas["BatchView"];
type Group = Schemas["HealthGroup"];
const ACTIVE = ["QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "RETRY_WAIT", "WAITING"];
const CATEGORIES = ["active", "attention", "history"] as const;
const KINDS = ["all", "sec", "prices", "events", "maintenance"] as const;

/** First line answers "is everything working?"; problems are grouped by cause, each with what to do. */
function Health() {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const queryClient = useQueryClient();
  const [, setParams] = useSearchParams();
  const health = useQuery({
    queryKey: ["system-health"],
    queryFn: () => unwrap(client.GET("/api/v1/system/health")),
    refetchInterval: visible ? 30_000 : false,
  });
  const retry = useMutation({
    mutationFn: (key: string) => unwrap(client.POST("/api/v1/system/health/retry", { body: { key } })),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["system-health"] });
      void queryClient.invalidateQueries({ queryKey: ["batches"] });
    },
  });
  if (health.isPending) return <Skeleton className="h-12 w-full" />;
  if (health.error)
    return (
      <p className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-card px-4 py-3 text-sm text-destructive">
        <AlertTriangle className="size-4" />
        {health.error.message}
      </p>
    );
  const data = health.data;
  if (data.status === "ok")
    return (
      <p className="flex items-center gap-2 rounded-lg border bg-card px-4 py-3 text-sm">
        <CheckCircle2 className="size-4 text-up" />
        <span className="font-medium">{t("ui.health.ok")}</span>
        <span className="ml-auto text-xs text-muted-foreground" title={t("ui.time.local", { time: formatLocal(data.checked_at) })}>
          {t("ui.health.checked", { time: formatEt(data.checked_at) })}
        </span>
      </p>
    );
  const action = (group: Group) => {
    if (!group.action) return null;
    if (group.action.kind === "retry")
      return (
        <Button size="xs" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate(group.action!.value!)}>
          {retry.isPending && retry.variables === group.action.value ? <LoaderCircle className="motion-safe:animate-spin" /> : <RotateCw />}
          {t("ui.health.retry")}
        </Button>
      );
    if (group.action.kind === "tasks")
      return (
        <Button size="xs" variant="outline" onClick={() => setParams({ kind: group.action!.value ?? "all", category: "attention" })}>
          {t("ui.health.view_tasks")}
        </Button>
      );
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <code className="inline-flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-xs">
            <Terminal className="size-3" />
            {group.action.value}
          </code>
        </TooltipTrigger>
        <TooltipContent>{t(`ui.health.command.${group.key.split("|")[0]}`, { defaultValue: t("ui.health.command.default") })}</TooltipContent>
      </Tooltip>
    );
  };
  return (
    <section className="rounded-lg border bg-card" aria-labelledby="health-heading">
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <AlertTriangle className="size-4 text-warn" />
        <h2 id="health-heading" className="text-sm font-semibold">{t("ui.health.issues", { count: data.groups.length })}</h2>
        <span className="ml-auto text-xs text-muted-foreground">{t("ui.health.checked", { time: formatEt(data.checked_at) })}</span>
      </header>
      <ul className="divide-y">
        {data.groups.map((group) => (
          <li key={group.key} className="flex items-center gap-3 px-4 py-2 text-sm">
            {group.severity === "info" ? <Info className="size-4 shrink-0 text-muted-foreground" /> : <span className={cn("size-2 shrink-0 rounded-full", group.severity === "critical" ? "bg-destructive" : "bg-warn")} />}
            <span className="min-w-0 flex-1">{tm(group.message)}</span>
            {group.since && <span className="shrink-0 text-xs text-muted-foreground">{t("ui.health.since", { time: formatAgo(group.since) })}</span>}
            {action(group)}
          </li>
        ))}
      </ul>
      {retry.data && <p className="border-t px-4 py-1.5 text-xs text-muted-foreground">{t("ui.health.retried", { count: retry.data.retried })}</p>}
    </section>
  );
}

/** Automatic collection on/off: SEC filings, home quotes, daily backup. */
function Automatic() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const policies = useQuery({ queryKey: ["collection-policy"], queryFn: () => unwrap(client.GET("/api/v1/collection-policy")) });
  const update = useMutation({
    mutationFn: (body: { key: "sec" | "market" | "backup" | "maintenance"; enabled: boolean }) => unwrap(client.PATCH("/api/v1/collection-policy", { body })),
    onSuccess: (data) => queryClient.setQueryData(["collection-policy"], data),
  });
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
      <span className="text-xs text-muted-foreground">{t("ui.data.automatic")}</span>
      {(["sec", "market", "backup", "maintenance"] as const).map((key) => {
        const policy = policies.data?.items.find((item) => item.key === key);
        if (!policy) return null;
        return (
          <Tooltip key={key}>
            <TooltipTrigger asChild>
              <button
                type="button"
                role="switch"
                aria-checked={policy.enabled}
                disabled={update.isPending}
                onClick={() => update.mutate({ key, enabled: !policy.enabled })}
                className="inline-flex items-center gap-2"
              >
                <span className={cn("relative h-4 w-7 rounded-full transition-colors", policy.enabled ? "bg-primary" : "bg-input")}>
                  <span className={cn("absolute top-0.5 left-0 size-3 rounded-full bg-card shadow transition-transform", policy.enabled ? "translate-x-3.5" : "translate-x-0.5")} />
                </span>
                {t(`ui.data.policy.${key}`)}
                {policy.blocked_reason && <AlertTriangle className="size-3.5 text-warn" />}
              </button>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              {policy.blocked_reason ? tm(policy.blocked_reason) : t(`ui.data.policy_tip.${key}`)}
              {policy.last_run_at && <> · {t("ui.data.last_run", { time: formatEt(policy.last_run_at) })}</>}
            </TooltipContent>
          </Tooltip>
        );
      })}
      {update.error && <span className="text-xs text-destructive">{update.error.message}</span>}
    </div>
  );
}

function progress(batch: Batch) {
  const done = batch.items.reduce((sum, item) => sum + (item.jobs_done ?? 0), 0);
  const total = batch.items.reduce((sum, item) => sum + (item.jobs_total ?? 0), 0);
  return { done, total };
}

const column = createColumnHelper<Batch>();

/** Tasks: filter by state and type; pause, resume, cancel or retry in place. */
function Tasks() {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const category = (CATEGORIES as readonly string[]).includes(params.get("category") ?? "") ? params.get("category")! : "active";
  const kind = (KINDS as readonly string[]).includes(params.get("kind") ?? "") ? params.get("kind")! : "all";
  const mine = params.get("by") === "me";
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors.at(-1) ?? "";
  const list = useQuery({
    queryKey: ["batches", category, kind, mine, cursor],
    queryFn: () => unwrap(client.GET("/api/v1/batches", { params: { query: { category, kind, cursor, view: mine ? "personal" : "all" } } })),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => (!visible ? false : query.state.data?.items.some((batch) => ACTIVE.includes(batch.status)) ? 3000 : 20_000),
  });
  const control = useMutation({
    mutationFn: ({ id, action }: { id: string; action: Schemas["BatchAction"]["action"] }) =>
      unwrap(client.POST("/api/v1/batches/{batch_id}/actions", { params: { path: { batch_id: id } }, body: { action } })),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["batches"] });
      void queryClient.invalidateQueries({ queryKey: ["my-tasks"] });
    },
  });
  const choose = (name: string, value: string | null) => {
    setCursors([""]);
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value === null) next.delete(name);
      else next.set(name, value);
      return next;
    }, { replace: true });
  };
  const columns = useMemo(
    () => [
      column.accessor("title", {
        header: t("ui.data.col.task"),
        cell: ({ row }) => {
          const batch = row.original;
          const reason = batch.items.find((item) => item.wait_reason)?.wait_reason;
          const href = resultHref(batch);
          return (
            <div className="min-w-0">
              <p className="truncate font-medium">{tm(batch.title)}</p>
              {reason && <p className="truncate text-xs text-muted-foreground" title={tm(reason)}>{tm(reason)}</p>}
              {href && <Link to={href} className="text-xs text-muted-foreground hover:text-foreground hover:underline">{t("ui.tasks.open_result")}</Link>}
            </div>
          );
        },
      }),
      column.accessor("kind", {
        header: t("ui.data.col.type"),
        cell: ({ getValue }) => <span className="whitespace-nowrap text-muted-foreground">{t(`ui.data.kind.${getValue()}`, { defaultValue: getValue() })}</span>,
      }),
      column.accessor((batch) => batch.activity_status ?? batch.status, {
        id: "status",
        header: t("ui.data.col.status"),
        cell: ({ getValue }) => {
          const status = getValue();
          return (
            <Badge variant={["FAILED", "PARTIAL"].includes(status) ? "outline" : "secondary"} className={cn(["FAILED", "PARTIAL"].includes(status) && "border-warn/40 text-warn")}>
              {ACTIVE.includes(status) && status !== "WAITING" && <LoaderCircle className="motion-safe:animate-spin" />}
              {t(`ui.status.${status}`, { defaultValue: status })}
            </Badge>
          );
        },
      }),
      column.display({
        id: "progress",
        header: t("ui.data.col.progress"),
        cell: ({ row }) => {
          const { done, total } = progress(row.original);
          if (!total) return <span className="text-muted-foreground">—</span>;
          return (
            <div className="w-28">
              <div className="h-1.5 overflow-hidden rounded-full bg-secondary">
                <div className="h-full rounded-full bg-primary/70 transition-[width] duration-300" style={{ width: `${Math.min(100, (done / total) * 100)}%` }} />
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground tabular-nums">{t("ui.tasks.progress", { done, total })}</p>
            </div>
          );
        },
      }),
      column.accessor("trigger", {
        header: t("ui.data.col.by"),
        cell: ({ getValue }) => <span className="whitespace-nowrap text-muted-foreground">{t(getValue() === "automatic" ? "ui.tasks.automatic" : "ui.data.by_you")}</span>,
      }),
      column.accessor("updated_at", {
        header: t("ui.data.col.updated"),
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-muted-foreground tabular-nums" title={`${t("ui.data.created", { time: formatEt(row.original.created_at) })}`}>
            {formatAgo(row.original.updated_at)}
          </span>
        ),
      }),
      column.display({
        id: "actions",
        header: () => <span className="sr-only">{t("ui.data.col.actions")}</span>,
        cell: ({ row }) => (
          <div className="flex justify-end gap-1">
            {actions(row.original.status).map((action) => (
              <Button key={action} size="xs" variant="outline" disabled={control.isPending}
                onClick={() => control.mutate({ id: row.original.id, action })}>
                {t(`ui.tasks.action.${action}`)}
              </Button>
            ))}
          </div>
        ),
      }),
    ],
    [t, control],
  );
  const table = useReactTable({ data: list.data?.items ?? [], columns, getCoreRowModel: getCoreRowModel() });
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <h2 className="mr-2 text-sm font-semibold">{t("ui.data.tasks")}</h2>
        <div className="inline-flex overflow-hidden rounded-md border bg-card">
          {CATEGORIES.map((value) => (
            <button key={value} type="button" aria-pressed={category === value} onClick={() => choose("category", value)}
              className={cn("h-7 border-r px-2.5 last:border-r-0 hover:bg-accent", category === value && "bg-primary text-primary-foreground hover:bg-primary")}>
              {t(`ui.data.category.${value}`)}
            </button>
          ))}
        </div>
        <select aria-label={t("ui.data.col.type")} value={kind} onChange={(event) => choose("kind", event.target.value === "all" ? null : event.target.value)}
          className="h-7 rounded-md border bg-card px-1.5">
          {KINDS.map((value) => <option key={value} value={value}>{t(`ui.data.kind_filter.${value}`)}</option>)}
        </select>
        <label className="inline-flex items-center gap-1.5">
          <input type="checkbox" checked={mine} onChange={(event) => choose("by", event.target.checked ? "me" : null)} />
          {t("ui.data.only_mine")}
        </label>
        {list.isFetching && <LoaderCircle className="size-3.5 text-muted-foreground motion-safe:animate-spin" />}
        {control.error && <span className="text-destructive">{control.error.message}</span>}
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full table-fixed text-sm">
          <colgroup>
            <col />
            <col className="w-32" />
            <col className="w-32" />
            <col className="w-36" />
            <col className="w-24" />
            <col className="w-28" />
            <col className="w-52" />
          </colgroup>
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="border-b">
                {group.headers.map((header) => (
                  <th key={header.id} className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {list.isPending
              ? Array.from({ length: 5 }, (_, index) => (
                  <tr key={index} className="border-b last:border-b-0"><td colSpan={7} className="px-3 py-2.5"><Skeleton className="h-4 w-full" /></td></tr>
                ))
              : table.getRowModel().rows.map((row) => (
                  <tr key={row.id} className="border-b last:border-b-0 hover:bg-accent/40">
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-3 py-2 align-middle">{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                ))}
          </tbody>
        </table>
        {!list.isPending && !list.data?.items.length && (
          <p className="px-4 py-8 text-center text-sm text-muted-foreground">{t(`ui.data.empty.${category}`)}</p>
        )}
      </div>
      {(cursors.length > 1 || list.data?.next_cursor) && (
        <div className="flex justify-end gap-2">
          <Button size="xs" variant="outline" disabled={cursors.length <= 1} onClick={() => setCursors((value) => value.slice(0, -1))}>{t("ui.data.newer")}</Button>
          <Button size="xs" variant="outline" disabled={!list.data?.next_cursor} onClick={() => setCursors((value) => [...value, list.data!.next_cursor!])}>{t("ui.data.older")}</Button>
        </div>
      )}
    </section>
  );
}

export function DataPage() {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <Health />
      <Automatic />
      <Tasks />
      <p className="text-xs text-muted-foreground">
        <Link to="/data/diagnostics" className="hover:text-foreground hover:underline">{t("ui.data.diagnostics")}</Link>
      </p>
    </div>
  );
}
