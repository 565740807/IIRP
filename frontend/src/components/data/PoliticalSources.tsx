import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { client, unwrap } from "@/lib/api-client";
import { formatEt } from "@/lib/format";
import { usePageVisible } from "@/lib/refresh";
import { tm } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { InfoTip } from "@/components/ui/info";

/** Archive counts and source availability; no requests to disclosure websites. */
export function PoliticalSources() {
  const { t } = useTranslation(["data", "translation"]);
  const visible = usePageVisible();
  const qc = useQueryClient();
  const check = useMutation({
    mutationFn: (source: "house" | "senate" | "oge") => unwrap(client.POST("/api/v1/political/sources/{source}/refresh", { params: { path: { source } } })),
    onSuccess: (data) => { qc.setQueryData(["political-sources"], data); void qc.invalidateQueries({ queryKey: ["jobs"] }); },
  });
  const query = useQuery({
    queryKey: ["political-sources"],
    queryFn: () => unwrap(client.GET("/api/v1/political/sources")),
    refetchInterval: visible ? 30_000 : false,
  });
  return (
    <section className="rounded-lg border bg-card" aria-labelledby="political-sources-heading">
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <h2 id="political-sources-heading" className="card-title">{t("ui.data.political.title")}</h2>
        <InfoTip>{t("ui.data.political.scope")} {t("ui.data.political.treasury_exemption")}</InfoTip>
      </header>
      {query.isPending && <div className="space-y-2 p-4"><Skeleton className="h-5 w-full" /><Skeleton className="h-5 w-full" /><Skeleton className="h-5 w-full" /></div>}
      {check.error && <p className="px-4 py-3 text-sm text-destructive">{check.error.message}</p>}
      {query.error && <p className="px-4 py-3 text-sm text-destructive">{query.error.message}</p>}
      {query.data && <ul className="divide-y text-sm">
        {query.data.items.map((item) => (
          <li key={item.source} className="space-y-1 px-4 py-2.5">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <span className="w-44 font-medium">{t(`ui.data.political.source.${item.source}`)}</span>
              <Badge variant="outline" className={item.status === "unavailable" ? "text-warn" : ""}>{t(`ui.data.political.status.${item.status}`)}</Badge>
              <span className="text-xs text-muted-foreground">{t("ui.data.political.checked", { time: item.last_check_at ? formatEt(item.last_check_at) : t("ui.index_lists.never") })}</span>
              <span className="text-xs tabular-nums">{t("ui.data.political.counts", { reports: item.report_count, transactions: item.transaction_count, review: item.review_count })}</span>
              <Button variant="outline" size="sm" disabled={item.checking || (check.isPending && check.variables === item.source)} onClick={() => check.mutate(item.source)}>{t(item.checking ? "ui.data.political.checking" : "ui.data.political.check_now")}</Button>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
              <span>{t("ui.data.political.progress", { done: item.pages_done, total: item.page_count, pending: item.pending_reports })}</span>
              <span>{t("ui.data.political.ocr", { pages: item.ocr_pages, seconds: Math.round(item.ocr_seconds), per_page: item.ocr_pages ? (item.ocr_seconds / item.ocr_pages).toFixed(1) : "—" })}</span>
            </div>
            {item.last_error && <p className="text-xs text-warn">{tm(item.last_error)}</p>}
          </li>
        ))}
      </ul>}
    </section>
  );
}
