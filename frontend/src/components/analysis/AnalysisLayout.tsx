import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { analysisHref } from "@/lib/navigation";
import { cn } from "@/lib/utils";

const TABS = ["monthly", "interval", "earnings", "events"] as const;
export type AnalysisTab = (typeof TABS)[number];
const COLLAPSED = "iirp.analysis.conditions_collapsed";

/** The four analysis tabs; monthly and interval reopen the research last viewed there. */
export function AnalysisTabs({ current }: { current: AnalysisTab }) {
  const { t } = useTranslation();
  return (
    <nav aria-label={t("ui.analysis.tabs_label")} className="flex flex-row gap-1 border-b">
      {TABS.map((tab) => (
        <NavLink
          key={tab}
          to={tab === "monthly" || tab === "interval" ? analysisHref(tab) : `/analysis/${tab}`}
          className={cn(
            "-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground",
            tab === current && "border-primary font-medium text-foreground",
          )}
        >
          {t(`ui.analysis.tab.${tab}`)}
        </NavLink>
      ))}
    </nav>
  );
}

function initiallyCollapsed() {
  try {
    return localStorage.getItem(COLLAPSED) === "1";
  } catch {
    return false;
  }
}

/**
 * Shared frame of the analysis center: tabs on top, a collapsible conditions
 * panel on the left and the results on the right (conclusion → main chart →
 * ranking and statistics → details → method).
 */
export function AnalysisLayout({ tab, conditions, children }: { tab: AnalysisTab; conditions: ReactNode; children: ReactNode }) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(initiallyCollapsed);
  const toggle = () =>
    setCollapsed((value) => {
      try {
        localStorage.setItem(COLLAPSED, value ? "0" : "1");
      } catch {
        /* Optional convenience. */
      }
      return !value;
    });
  return (
    <div className="space-y-4">
      <AnalysisTabs current={tab} />
      <div className={cn("grid items-start gap-4", collapsed ? "grid-cols-[2.25rem_minmax(0,1fr)]" : "grid-cols-[16.5rem_minmax(0,1fr)]")}>
        <aside className="sticky top-16 rounded-lg border bg-card">
          <div className={cn("flex items-center border-b", collapsed ? "justify-center py-1" : "justify-between py-1 pr-1 pl-3")}>
            {!collapsed && <h2 className="text-sm font-semibold">{t("ui.analysis.conditions")}</h2>}
            <button
              type="button"
              onClick={toggle}
              aria-expanded={!collapsed}
              aria-label={t(collapsed ? "ui.analysis.show_conditions" : "ui.analysis.hide_conditions")}
              title={t(collapsed ? "ui.analysis.show_conditions" : "ui.analysis.hide_conditions")}
              className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
            </button>
          </div>
          <div className={cn(collapsed && "hidden")}>{conditions}</div>
        </aside>
        <div className="min-w-0 space-y-4">{children}</div>
      </div>
    </div>
  );
}
