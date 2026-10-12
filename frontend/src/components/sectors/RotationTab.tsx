import { useTranslation } from "react-i18next";
import { useRotation } from "@/lib/rotation";
import { Explain } from "@/components/ui/info";
import { PageContents } from "@/components/ui/page-contents";
import type { SectorState } from "@/lib/sectorState";
import {
  RotationDetailView,
  RotationFacts,
  RotationHeat,
  RotationLoading,
  RotationSection,
  RotationSummary,
  RotationYearly,
} from "./Rotation";

/**
 * The monthly rotation tab (every month, one shown in full) and the interval
 * tab (one interval). Ranks, statistics and leaders are computed by the backend
 * from the long-term daily store; switching anything here never fetches prices.
 */
export function RotationTab({ state, update }: {
  state: SectorState; update: (changes: Partial<SectorState>) => void;
}) {
  const { t } = useTranslation();
  const interval = state.tab === "interval";
  const choice = { level: state.level, rank: state.rank, years: state.years };
  const query = useRotation({ ...choice, month: interval ? null : state.month,
    interval: interval ? { start: state.start, end: state.end } : null });
  const data = query.data;
  const month = state.month ?? data?.current_month ?? null;
  if (query.error && !data) return <p className="text-sm text-destructive">{query.error.message}</p>;
  if (!data) return <RotationLoading />;
  if (!data.periods.length || !data.detail) {
    return <p className="rounded-md border bg-card px-3 py-2 text-sm text-muted-foreground">{t("ui.rotation.empty")}</p>;
  }
  const contents = [
    { id: "r-summary", label: t("ui.analysis.toc.summary") },
    ...(!interval ? [{ id: "r-heat", label: t("ui.analysis.toc.heat") }] : []),
    { id: "r-ranking", label: t("ui.analysis.toc.ranking") },
    { id: "r-box", label: t("ui.analysis.toc.box") },
    { id: "r-yearly", label: t("ui.rotation.toc.yearly") },
  ];
  return (
    <div className="space-y-5">
      <div id="r-summary" className="relative scroll-mt-28 space-y-1">
        {query.isFetching && (
          <div className="absolute inset-x-0 -top-2 h-0.5 overflow-hidden" role="progressbar" aria-label={t("ui.rotation.loading")}>
            <div className="h-full w-full origin-left bg-primary/60 motion-safe:animate-progress" />
          </div>
        )}
        <RotationSummary data={data} detail={data.detail} choice={choice} />
        <RotationFacts data={data} />
        {query.error && <p className="text-xs text-destructive">{query.error.message}</p>}
      </div>
      <PageContents items={contents} />
      {!interval && month != null && (
        <RotationSection id="r-heat" title={t(`ui.rotation.heat_title.${choice.rank}`)} hint={t("ui.rotation.heat_hint")}>
          <RotationHeat data={data} choice={choice} selected={month} onSelect={(next) => update({ month: next })} />
        </RotationSection>
      )}
      <RotationDetailView data={data} choice={choice} />
      <RotationSection id="r-yearly" title={t("ui.rotation.yearly_title")} hint={t(`ui.rotation.yearly_hint.${choice.rank}`)}>
        <RotationYearly data={data} choice={choice} selected={interval ? null : month} onSelect={(next) => update({ month: next })} />
      </RotationSection>
      <Explain summary={t("ui.rotation.method")}/>
    </div>
  );
}
