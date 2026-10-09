import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { tm } from "@/i18n";
import { client, unwrap } from "@/lib/api-client";
import { formatDay } from "@/lib/format";
import { INDICES, type IndexFilter as Index } from "@/lib/insiderOverview";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export function IndexFilter({ value, choose }: { value: Index; choose: (value: string) => void }) {
  const { t } = useTranslation();
  return <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={value} aria-label={t("ui.overview.index_label")} onValueChange={(next) => next && choose(next)}>
    {INDICES.map((index) => <ToggleGroupItem key={index} value={index} className="px-2.5">{t(`ui.overview.index.${index}`)}</ToggleGroupItem>)}
  </ToggleGroup>;
}

export function IndexStatus({ value }: { value: Index }) {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ["insider-indices"], queryFn: () => unwrap(client.GET("/api/v1/insider/indices")), staleTime: 60_000 });
  return <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
    {query.data?.indices.filter((index) => value === "all" || index.index_name === value).map((index) => <span key={index.index_name}>
      {t(`ui.overview.index.${index.index_name}`)} · {t("ui.overview.index_updated", { day: index.updated_at ? formatDay(index.updated_at, { year: true }) : t("ui.overview.index_pending") })}
      {index.error && <span className="ml-1 text-warn">{t("ui.overview.index_error", { reason: tm(index.error) })}</span>}
    </span>)}
    {query.error && <span className="text-warn">{query.error.message}</span>}
  </div>;
}
