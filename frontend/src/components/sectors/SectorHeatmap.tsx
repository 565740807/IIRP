import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts/core";
import { TreemapChart } from "echarts/charts";
import { TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useReducedMotion } from "motion/react";
import { formatDay, formatSignedRatio } from "@/lib/format";
import { periodChange, periodWeight, type Period, type PricedRow } from "@/lib/sectors";

echarts.use([TreemapChart, TooltipComponent, CanvasRenderer]);

// The CSS tokens' blue (up) and orange (down) as canvas colors, and a neutral tile.
const UP: [number, number, number] = [33, 86, 217];
const DOWN: [number, number, number] = [194, 83, 26];
const NEUTRAL: [number, number, number] = [241, 245, 249];

function mix(target: [number, number, number], share: number) {
  const channel = (index: number) => Math.round(NEUTRAL[index] + (target[index] - NEUTRAL[index]) * share);
  return `rgb(${channel(0)}, ${channel(1)}, ${channel(2)})`;
}

/**
 * Treemap of the sectors or industry groups for one period. Tile area is the
 * backend's weight; color is the direction (blue up, orange down) and, by
 * depth, the size of the change relative to the largest one shown.
 */
export function SectorHeatmap({ items, period, onSelect, name, label }: {
  items: PricedRow[]; period: Period; onSelect: (etf: string) => void; name: (id: string) => string; label: string;
}) {
  const { t } = useTranslation();
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const reduce = useReducedMotion();
  const select = useRef(onSelect);
  select.current = onSelect;

  useEffect(() => {
    if (!element.current) return;
    const instance = echarts.init(element.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(element.current);
    instance.on("click", (event) => {
      const etf = (event.data as { etf?: string } | undefined)?.etf;
      if (etf) select.current(etf);
    });
    return () => { resize.disconnect(); instance.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    const values = items.map((item) => periodChange(item, period).value).filter((value): value is string => value != null).map(Number);
    const largest = Math.max(...values.map(Math.abs), 0);
    const data = items.map((item) => {
      const change = periodChange(item, period);
      const value = change.value == null ? null : Number(change.value);
      const weight = periodWeight(item, period);
      const share = value == null || largest === 0 ? 0 : 0.3 + 0.7 * Math.abs(value) / largest;
      const color = value == null || value === 0 ? mix(UP, 0) : mix(value > 0 ? UP : DOWN, share);
      const dark = share > 0.55;
      return {
        name: name(item.id),
        etf: item.etf,
        // Tiles without a change keep the smallest area.
        value: weight ?? 0.2,
        change: value,
        start: change.start_date,
        end: change.end_date,
        itemStyle: { color, borderColor: "#fff", borderWidth: 2, gapWidth: 2 },
        label: { color: dark ? "#fff" : "#0f172a" },
      };
    });
    instance.setOption({
      animation: !reduce,
      animationDurationUpdate: reduce ? 0 : 400,
      tooltip: {
        confine: true,
        formatter: (params: { data?: { name: string; etf: string; change: number | null; start?: string | null; end?: string | null } }) => {
          const item = params.data;
          if (!item) return "";
          const basis = item.start && item.end ? `<br/>${t("ui.sectors.basis_close", { start: formatDay(item.start, { year: true }), end: formatDay(item.end, { year: true }) })}` : "";
          return `${item.name} · ${item.etf}<br/><b>${item.change == null ? "—" : formatSignedRatio(item.change, 2)}</b>${basis}`;
        },
      },
      series: [{
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        width: "100%",
        height: "100%",
        top: 0,
        left: 0,
        squareRatio: 1,
        label: {
          show: true,
          formatter: (params: { data?: { name: string; etf: string; change: number | null } }) => {
            const item = params.data;
            return item ? `{name|${item.name}}\n{etf|${item.etf}}\n{change|${item.change == null ? "—" : formatSignedRatio(item.change, 2)}}` : "";
          },
          rich: {
            name: { fontSize: 13, fontWeight: 600, lineHeight: 18 },
            etf: { fontSize: 11, lineHeight: 16, opacity: 0.85 },
            change: { fontSize: 15, fontWeight: 700, lineHeight: 22 },
          },
        },
        upperLabel: { show: false },
        data,
      }],
    }, { notMerge: false });
  }, [items, period, reduce, t, name]);

  return <div ref={element} className="h-[560px] w-full rounded-lg border bg-card p-1" role="img" aria-label={t("ui.sectors.heatmap_label", { level: label, period: t(`ui.sectors.period.${period}`) })}/>;
}
