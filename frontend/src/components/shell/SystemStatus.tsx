import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { ReactNode } from "react";
import { client, unwrap } from "@/lib/api-client";
import { tm } from "@/i18n";
import { formatEt, formatNumber } from "@/lib/format";
import { systemRefreshInterval } from "@/systemRefresh";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

function Dot({ ok }: { ok: boolean | null | undefined }) {
  return (
    <span
      aria-hidden
      className={cn("mr-1.5 inline-block size-2 rounded-full", ok ? "bg-up" : ok === false ? "bg-warn" : "bg-neutral-mark")}
    />
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 border-b py-2 last:border-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** Local service, storage and backup state; read only. */
export function SystemStatus({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["system"],
    queryFn: () => unwrap(client.GET("/api/v1/system")),
    enabled: open,
    refetchInterval: (state) => systemRefreshInterval(state.state.data?.storage.inventory_status),
  });
  const data = query.data;
  const storage = data?.storage;
  const latestBackup = storage?.backup_list[0];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("ui.system.title")}</DialogTitle>
          <DialogDescription>{t("ui.system.description")}</DialogDescription>
        </DialogHeader>
        {query.error && <p className="text-sm text-destructive">{query.error.message}</p>}
        {!data || !storage ? (
          <div className="space-y-2">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-6" />)}</div>
        ) : (
          <dl className="text-sm">
            <Row label={t("ui.system.worker")}>
              <Dot ok={data.worker.online} />
              {data.worker.online === null
                ? tm(data.worker.error) || t("ui.system.unknown")
                : data.worker.online ? t("ui.system.online") : t("ui.system.offline")}
              {data.worker.last_seen && <span className="text-muted-foreground"> · {formatEt(data.worker.last_seen)}</span>}
            </Row>
            <Row label={t("ui.system.sec_user_agent")}>
              <Dot ok={data.sec_user_agent.configured} />
              {data.sec_user_agent.configured ? t("ui.system.configured") : tm(data.sec_user_agent.message)}
            </Row>
            <Row label={t("ui.system.free_disk")}>
              {typeof storage.free_bytes === "number" ? `${formatNumber(storage.free_bytes / GIB, 1)} GiB` : t("ui.system.unknown")}
            </Row>
            <Row label={t("ui.system.database")}>
              {typeof storage.database === "number" ? `${formatNumber(storage.database / MIB, 0)} MiB` : t("ui.system.unknown")}
            </Row>
            <Row label={t("ui.system.filings")}>
              {storage.inventory_status === "fresh"
                ? t("ui.system.filings_value", { count: storage.objects ?? 0, size: formatNumber((storage.bytes ?? 0) / MIB, 0) })
                : t(`ui.system.inventory.${storage.inventory_status ?? "stale"}`, { defaultValue: t("ui.system.unknown") })}
            </Row>
            <Row label={t("ui.system.latest_backup")}>
              {latestBackup?.completed_at ? (
                <>
                  {formatEt(latestBackup.completed_at)}
                  <span className="text-muted-foreground">
                    {" · "}
                    {latestBackup.restore_verified_at ? t("ui.system.restore_verified") : t("ui.system.restore_unverified")}
                  </span>
                </>
              ) : t("ui.system.no_backup")}
            </Row>
            <Row label={t("ui.system.migration")}>{tm(data.migration)}</Row>
            <Row label={t("ui.system.version")}>{data.version}</Row>
          </dl>
        )}
        {data && <p className="text-xs text-muted-foreground">{tm(data.automatic_collection_scope)}</p>}
      </DialogContent>
    </Dialog>
  );
}
