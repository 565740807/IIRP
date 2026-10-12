import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, LoaderCircle } from "lucide-react";
import { benchmarkName } from "@/lib/analysis";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import type { Schemas } from "@/lib/api-client";
import { cn } from "@/lib/utils";

/** Every price input with its fetch and deletion time, and when the result itself is deleted. */
export function CacheTimes({ sources, expires }: { sources: Schemas["CacheSource"][]; expires?: string | null }) {
  const { t } = useTranslation();
  if (!sources.length) return null;
  return <span className="flex flex-wrap gap-x-3 gap-y-1">
    {sources.map((source) => <span key={`${source.symbol}:${source.fetched_at}`} title={t("ui.time.local", { time: formatLocal(source.fetched_at) })}>
      {t("ui.analysis.cache_source", { symbol: benchmarkName(source.symbol), fetched: formatEt(source.fetched_at), expires: formatEt(source.expires_at) })}
      {source.local && <span className="ml-1 text-foreground/70">· {t("ui.analysis.source.local_tag")}</span>}
    </span>)}
    <span className="basis-full">{t("ui.analysis.cache_result", { expires: formatEt(expires) })}</span>
  </span>;
}

/**
 * One line about the prices behind a result: "Prices: local long-term prices ·
 * through Oct 9" when every input was rebuilt from the long-term daily prices,
 * otherwise "24-hour cache · deleted <time>". "Data sources" opens the
 * per-input fetch and deletion times.
 */
export function SourceLine({ sources, expires, cutoff, refreshing }: {
  sources: Schemas["CacheSource"][];
  expires?: string | null;
  cutoff?: string | null;
  refreshing: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (refreshing)
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
        {t("ui.analysis.expired_refetching")}
      </span>
    );
  const local = sources.length > 0 && sources.every((source) => source.local);
  const through = cutoff ? formatDay(cutoff, { year: false }) : null;
  const summary = local
    ? t("ui.analysis.source.local", { day: through ?? "—" })
    : t("ui.analysis.source.cache", { expires: formatEt(expires), day: through ?? "—" });
  return (
    <div className="min-w-0 text-xs text-muted-foreground">
      <p className="flex flex-wrap items-center gap-x-2">
        <span>{summary}</span>
        {sources.length > 0 && (
          <button type="button" data-sources-toggle aria-expanded={open} onClick={() => setOpen((value) => !value)}
            className="inline-flex items-center gap-0.5 font-medium text-foreground/70 hover:text-foreground">
            {t("ui.analysis.source.details")}
            <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
          </button>
        )}
      </p>
      {open && <div className="mt-1 rounded-md border bg-card px-2.5 py-1.5"><CacheTimes sources={sources} expires={expires} /></div>}
    </div>
  );
}
