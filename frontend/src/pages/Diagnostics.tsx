import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap } from "@/lib/api-client";
import { formatLocal } from "@/lib/format";
import { Button } from "@/components/ui/button";

type Probe = "market_probe" | "sec_probe" | "fixture_check";
const PROBES: Probe[] = ["market_probe", "sec_probe", "fixture_check"];
const DONE = ["SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"];

/** Development probes (SAMPLE_ONLY): they never become coverage or research results. */
export function DiagnosticsPage() {
  const { t } = useTranslation();
  const [ticker, setTicker] = useState("AAPL");
  const start = useMutation({
    mutationFn: (kind: Probe) =>
      unwrap(client.POST("/api/v1/diagnostics/collections", { body: { kind, ticker } })),
  });
  const id = start.data?.job_id;
  const job = useQuery({
    queryKey: ["job", id],
    enabled: !!id,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: id! } }, signal })),
    refetchInterval: (state) => (state.state.data && DONE.includes(state.state.data.status) ? false : 1_500),
  });
  const current = job.data ?? start.data?.job;
  return (
    <div className="max-w-3xl space-y-4">
      <Link to="/data" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" />
        {t("ui.diagnostics.back")}
      </Link>
      <div>
        <h1 className="text-lg font-semibold tracking-tight">{t("ui.data.diagnostics")}</h1>
        <p className="text-sm text-muted-foreground">{t("ui.diagnostics.intro")}</p>
      </div>
      <section className="space-y-3 rounded-lg border bg-card p-4">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t("ui.diagnostics.ticker")}</span>
          <input
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            className="h-8 w-28 rounded-md border bg-background px-2 uppercase outline-none focus-visible:border-ring"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          {PROBES.map((kind) => (
            <Button key={kind} variant="outline" size="sm" disabled={start.isPending} onClick={() => start.mutate(kind)}>
              {start.isPending && start.variables === kind && <LoaderCircle className="motion-safe:animate-spin" />}
              {t(`ui.diagnostics.${kind}`)}
            </Button>
          ))}
        </div>
        {start.error && <p className="text-sm text-destructive">{start.error.message}</p>}
        {current && (
          <div className="rounded-md border px-3 py-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{tm(current.title)}</span>
              <span className="text-xs text-muted-foreground">{t(`ui.status.${current.status}`, { defaultValue: current.status })}</span>
              {!DONE.includes(current.status) && <LoaderCircle className="size-3.5 text-muted-foreground motion-safe:animate-spin" />}
              <span className="ml-auto text-xs text-muted-foreground">{formatLocal(String(current.updated_at))}</span>
            </div>
            {current.error && <p className="mt-1 text-xs text-destructive">{tm(current.error)}</p>}
            {current.result && (
              <pre className="mt-2 max-h-60 overflow-auto rounded bg-muted/40 p-2 text-xs">{JSON.stringify(current.result, null, 2)}</pre>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
