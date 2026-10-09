import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { client, unwrap } from "@/lib/api-client";
import { formatDay, formatNumber } from "@/lib/format";
import { actionLabel, readableCase } from "@/lib/insider";
import { Button } from "@/components/ui/button";
import type { Trade } from "./TradeTable";

const ACTIONS = ["ADD", "REPLACE", "UNCHANGED", "REMOVE"] as const;

/**
 * An amended filing whose rows could not be matched to the original
 * automatically: the reader records what the amendment does to which row,
 * with the evidence. Both source observations stay saved.
 */
export function AmendmentReview({ relation, transaction }: { relation: { id: string; action: string; original_event_id?: string | null }; transaction: Trade }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [action, setAction] = useState<(typeof ACTIONS)[number]>("ADD");
  const [original, setOriginal] = useState(relation.original_event_id ?? "");
  const [evidence, setEvidence] = useState("");
  const candidates = useQuery({
    queryKey: ["amendment-candidates", transaction.issuer_id],
    queryFn: () => unwrap(client.GET("/api/v1/companies/{entity_id}", { params: { path: { entity_id: transaction.issuer_id }, query: { limit: 100 } } })),
    enabled: open && action !== "ADD",
  });
  const save = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/amendments/{relation_id}/resolve", {
        params: { path: { relation_id: relation.id }, query: { action, original_event_id: action === "ADD" ? "" : original, evidence } },
      })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["transaction"] }),
  });
  return (
    <section className="rounded-lg border bg-card px-4 py-3 text-sm">
      <div className="flex items-center gap-2">
        <h3 className="font-semibold">{t("ui.amendment.title")}</h3>
        <span className="text-xs text-muted-foreground">{t("ui.amendment.hint")}</span>
        {!open && <Button size="xs" variant="outline" className="ml-auto" onClick={() => setOpen(true)}>{t("ui.amendment.open")}</Button>}
      </div>
      {open && (
        <form className="mt-2 grid gap-2 md:grid-cols-[14rem_minmax(0,1fr)]" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
          <label className="text-xs text-muted-foreground">
            {t("ui.amendment.action")}
            <select value={action} onChange={(event) => setAction(event.target.value as (typeof ACTIONS)[number])} className="mt-1 h-8 w-full rounded-md border bg-card px-2 text-sm text-foreground">
              {ACTIONS.map((value) => <option key={value} value={value}>{t(`ui.amendment.choice.${value}`)}</option>)}
            </select>
          </label>
          {action !== "ADD" ? (
            <label className="text-xs text-muted-foreground">
              {t("ui.amendment.original")}
              <select required value={original} onChange={(event) => setOriginal(event.target.value)} className="mt-1 h-8 w-full rounded-md border bg-card px-2 text-sm text-foreground">
                <option value="">{t("ui.amendment.original_pick")}</option>
                {candidates.data?.items.filter((row) => row.id !== transaction.id).map((row) => (
                  <option key={row.id} value={row.id}>
                    {formatDay(row.transaction_date, { year: true })} · {readableCase(row.owners[0]?.name ?? "")} · {actionLabel(row, t)} · {formatNumber(row.shares, 0)}
                  </option>
                ))}
              </select>
            </label>
          ) : <span />}
          <label className="text-xs text-muted-foreground md:col-span-2">
            {t("ui.amendment.evidence")}
            <textarea required rows={2} value={evidence} onChange={(event) => setEvidence(event.target.value)} placeholder={t("ui.amendment.evidence_hint")}
              className="mt-1 w-full rounded-md border bg-card px-2 py-1.5 text-sm text-foreground" />
          </label>
          <div className="flex items-center gap-2 md:col-span-2">
            <Button type="submit" size="sm" disabled={save.isPending}>{t("ui.amendment.save")}</Button>
            {save.isSuccess && <span className="text-xs text-muted-foreground">{t("ui.amendment.saved")}</span>}
            {(save.error || candidates.error) && <span className="text-xs text-destructive">{(save.error ?? candidates.error)!.message}</span>}
          </div>
        </form>
      )}
    </section>
  );
}
