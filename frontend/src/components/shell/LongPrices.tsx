import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatEt, formatNumber } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

type Action = Schemas["ManagementInput"]["action"];
const ACTIONS: Action[] = ["all", "year", "half_year", "quarter", "refill"];

export function LongPricesSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const query = useQuery({ queryKey: ["long-prices"], queryFn: () => unwrap(client.GET("/api/v1/long-prices")), refetchInterval: 5000 });
  const save = useMutation({
    mutationFn: (selected: Action) => unwrap(client.POST("/api/v1/long-prices/manage", { body: { action: selected, confirmed: true } })),
    onSuccess: () => { setAction(null); void queryClient.invalidateQueries({ queryKey: ["long-prices"] }); },
  });
  const data = query.data;
  return <section className="space-y-3 border-t pt-4" aria-label={t("ui.long_prices.title")}>
    <h3 className="text-sm font-semibold">{t("ui.long_prices.title")}</h3>
    <p className="text-xs text-muted-foreground">{t("ui.long_prices.hint", { years: data?.history_years ?? 10 })}</p>
    {data && <>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <div><dt className="text-muted-foreground">{t("ui.long_prices.symbols")}</dt><dd>{formatNumber(data.ready_symbols, 0)} / {formatNumber(data.active_symbols, 0)} ({t("ui.long_prices.total", { count: data.symbols })})</dd></div>
        <div><dt className="text-muted-foreground">{t("ui.long_prices.bars")}</dt><dd>{formatNumber(data.bars, 0)}</dd></div>
        <div><dt className="text-muted-foreground">{t("ui.long_prices.range")}</dt><dd>{data.start_date ?? "—"} – {data.end_date ?? "—"}</dd></div>
        <div><dt className="text-muted-foreground">{t("ui.long_prices.size")}</dt><dd>{t("ui.long_prices.megabytes", { value: formatNumber(data.bytes / 1024 / 1024, 2) })}</dd></div>
        <div><dt className="text-muted-foreground">{t("ui.long_prices.updated")}</dt><dd>{formatEt(data.updated_at)}</dd></div>
        <div><dt className="text-muted-foreground">{t("ui.long_prices.status")}</dt><dd>{t(`ui.long_prices.state.${data.status}`)}</dd></div>
      </dl>
      {data.gap_symbols > 0 && <details className="text-xs"><summary>{t("ui.long_prices.gaps", { symbols: data.gap_symbols, days: data.gap_days })}</summary>
        {data.gaps.map((row) => <p key={row.symbol}>{row.symbol} · {row.dates.join(", ")} · {t(row.accepted ? "ui.long_prices.gaps_accepted" : "ui.long_prices.gaps_pending")}</p>)}
      </details>}
      {data.removed.length > 0 && <details className="text-xs"><summary>{t("ui.long_prices.removed_list", { count: data.removed.length })}</summary>
        {data.removed.map((row) => <p key={row.symbol}>{row.symbol} · {t("ui.long_prices.removed", { date: row.removed_on })}</p>)}
      </details>}
    </>}
    <div className="flex flex-wrap gap-2">{ACTIONS.map((selected) => <Button key={selected} size="xs" variant="outline" onClick={() => setAction(selected)}>{t(`ui.long_prices.action.${selected}`, { years: data?.history_years ?? 10 })}</Button>)}</div>
    {save.isSuccess && <p role="status" className="text-xs">{t("ui.long_prices.queued")}</p>}
    {(query.error || save.error) && <p role="alert" className="text-xs text-destructive">{(query.error ?? save.error)?.message}</p>}
    <Dialog open={action !== null} onOpenChange={(open) => { if (!open && !save.isPending) setAction(null); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t("ui.long_prices.confirm_title")}</DialogTitle><DialogDescription>{t(action === "refill" ? "ui.long_prices.confirm_refill" : "ui.long_prices.confirm_delete", { action: action ? t(`ui.long_prices.action.${action}`, { years: data?.history_years ?? 10 }) : "", years: data?.history_years ?? 10 })}</DialogDescription></DialogHeader>
        <div className="flex justify-end gap-2"><Button variant="outline" disabled={save.isPending} onClick={() => setAction(null)}>{t("ui.long_prices.cancel")}</Button><Button disabled={save.isPending} onClick={() => action && save.mutate(action)}>{t("ui.long_prices.confirm")}</Button></div>
      </DialogContent>
    </Dialog>
  </section>;
}
