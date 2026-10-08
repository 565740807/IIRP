import { useTranslation } from "react-i18next";
import { share } from "@/lib/analysis";

export function UpShare({ stats }: { stats: { n: number; up: number; up_ratio?: string | null; up_low?: string | null; up_high?: string | null } }) {
  const { t } = useTranslation();
  if (!stats.n) return <>—</>;
  return <span className="inline-flex flex-col leading-snug"><strong className="tabular-nums">{t("ui.analysis.up_count", { up: stats.up, n: stats.n, ratio: share(stats.up_ratio) })}</strong><span className="text-[11px] font-normal text-muted-foreground">{t("ui.analysis.col.up_interval", { low: share(stats.up_low), high: share(stats.up_high) })}</span></span>;
}
