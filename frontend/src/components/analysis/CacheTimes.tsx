import { useTranslation } from "react-i18next";
import { benchmarkName } from "@/lib/analysis";
import { formatEt, formatLocal } from "@/lib/format";
import type { Schemas } from "@/lib/api-client";

export function CacheTimes({ sources, expires }: { sources: Schemas["CacheSource"][]; expires?: string | null }) {
  const { t } = useTranslation();
  if (!sources.length) return null;
  return <span className="flex flex-wrap gap-x-3 gap-y-1">
    {sources.map((source) => <span key={`${source.symbol}:${source.fetched_at}`} title={t("ui.time.local", { time: formatLocal(source.fetched_at) })}>{t("ui.analysis.cache_source", { symbol: benchmarkName(source.symbol), fetched: formatEt(source.fetched_at), expires: formatEt(source.expires_at) })}</span>)}
    <span className="basis-full">{t("ui.analysis.cache_result", { expires: formatEt(expires) })}</span>
  </span>;
}
