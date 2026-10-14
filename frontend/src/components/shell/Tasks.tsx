import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { tm } from "@/i18n";
import { formatAgo } from "@/lib/format";
import { ACTIVE_TASKS as ACTIVE, useMyBatches } from "@/lib/personalTasks";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Skeleton } from "@/components/ui/skeleton";

type Batch = Schemas["BatchView"];
type Action = Schemas["BatchAction"]["action"];
/** Controls a batch offers in its state (same rules as the Data page). */
export function actions(status: string): Action[] {
  return [
    ...(["QUEUED", "RUNNING", "RETRY_WAIT", "WAITING"].includes(status) ? ["pause" as const] : []),
    ...(status === "PAUSED" ? ["resume" as const] : []),
    ...(["FAILED", "PARTIAL"].includes(status) ? ["retry_failed" as const] : []),
    ...([...ACTIVE, "PAUSED", "PARTIAL"].includes(status) && status !== "CANCEL_REQUESTED" ? ["cancel" as const] : []),
  ];
}

export function resultHref(batch: Batch) {
  if (!batch.analysis_id) return null;
  const purpose = batch.kind === "event_dates"
    ? batch.params.event_kind === "earnings" ? "earnings" : "events"
    : String(batch.params.purpose ?? "monthly");
  const kind = ["monthly", "interval", "earnings", "events"].includes(purpose) ? purpose : "monthly";
  // Monthly and interval pages read ?id=; the event pages still read ?a=.
  return `/analysis/${kind}?${kind === "monthly" || kind === "interval" ? "id" : "a"}=${batch.analysis_id}`;
}

function TaskRow({ batch, onNavigate }: { batch: Batch; onNavigate: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const control = useMutation({
    mutationFn: (action: Action) =>
      unwrap(client.POST("/api/v1/batches/{batch_id}/actions", {
        params: { path: { batch_id: batch.id } },
        body: { action },
      })),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["my-tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["batches"] });
    },
  });
  const done = batch.items.filter((item) => item.status === "READY").length;
  const current = batch.items.find((item) => item.progress?.activity_status === "RUNNING") ?? batch.items[0];
  const stage = current?.progress?.stage ?? current?.wait_reason;
  const href = resultHref(batch);
  const status = batch.activity_status ?? batch.status;
  return (
    <li className="rounded-lg border bg-card p-3">
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 font-medium leading-snug">{tm(batch.title)}</p>
        <Badge variant="secondary" className="shrink-0">
          {ACTIVE.includes(status) && status !== "WAITING" && <LoaderCircle className="motion-safe:animate-spin" />}
          {t(`ui.status.${status}`, { defaultValue: status })}
        </Badge>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {[
          batch.trigger === "automatic" ? t("ui.tasks.automatic") : null,
          batch.items.length ? t("ui.tasks.progress", { done, total: batch.items.length }) : null,
          stage ? tm(String(stage)) : null,
        ].filter(Boolean).join(" · ")}
      </p>
      {control.error && <p className="mt-1 text-xs text-destructive">{control.error.message}</p>}
      <div className="mt-2 flex items-center gap-1">
        {actions(batch.status).map((action) => (
          <Button key={action} size="xs" variant="outline" disabled={control.isPending} onClick={() => control.mutate(action)}>
            {t(`ui.tasks.action.${action}`)}
          </Button>
        ))}
        {href && (
          <Button asChild size="xs" variant="ghost">
            <Link to={href} onClick={onNavigate}>{t("ui.tasks.open_result")}</Link>
          </Button>
        )}
        <span className="ml-auto text-xs text-muted-foreground">{formatAgo(batch.updated_at)}</span>
      </div>
    </li>
  );
}

/** Top-bar entry for the reader's own tasks, with a drawer of active and attention items. */
export function TaskPanel({ open, onOpenChange: setOpen, initialCategory }: {
  open: boolean; onOpenChange: (open: boolean) => void; initialCategory: "active" | "attention";
}) {
  const { t } = useTranslation();
  const [category, setCategory] = useState<"active" | "attention">(initialCategory);
  const summary = useMyBatches("active");
  const list = useMyBatches(category, open);
  const counts = summary.data?.counts ?? {};
  const running = (counts.running ?? 0) + (counts.waiting ?? 0);
  const attention = counts.attention ?? 0;
  return (
    <>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="w-[420px] sm:max-w-[420px]">
          <SheetHeader>
            <SheetTitle>{t("ui.tasks.title")}</SheetTitle>
            <SheetDescription>{t("ui.tasks.description")}</SheetDescription>
          </SheetHeader>
          <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pb-4">
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              spacing={0}
              value={category}
              onValueChange={(value) => value && setCategory(value as typeof category)}
            >
              <ToggleGroupItem value="active">{t("ui.tasks.active")} {running || ""}</ToggleGroupItem>
              <ToggleGroupItem value="attention">{t("ui.tasks.attention")} {attention || ""}</ToggleGroupItem>
            </ToggleGroup>
            {list.error && <p className="text-sm text-destructive">{list.error.message}</p>}
            <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
              {list.isPending
                ? Array.from({ length: 3 }, (_, index) => <Skeleton key={index} className="h-20" />)
                : list.data?.items.length
                  ? list.data.items.map((batch) => <TaskRow key={batch.id} batch={batch} onNavigate={() => setOpen(false)} />)
                  : <li className="py-8 text-center text-sm text-muted-foreground">{t(`ui.tasks.empty.${category}`)}</li>}
            </ul>
            <Button asChild variant="outline" className="w-full">
              <Link to="/data" onClick={() => setOpen(false)}>{t("ui.tasks.open_data")}</Link>
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
