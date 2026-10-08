import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts/core";
import { BoxplotChart, ScatterChart } from "echarts/charts";
import { GridComponent, TooltipComponent, ToolboxComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { pct } from "@/lib/analysis";
import type { Schemas } from "@/lib/api-client";

echarts.use([BoxplotChart, ScatterChart, GridComponent, TooltipComponent, ToolboxComponent, CanvasRenderer]);
export type BoxGroup = {
  key: string;
  label: string;
  stats: Schemas["DistributionStatistics"];
  benchmark?: Schemas["DistributionStatistics"];
  points: { id: string; label: string; value: string | null | undefined; current?: boolean }[];
};
export const escapeHtml = (value: string) => value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

/** Whiskers, quartiles and outliers come from Python; points retain their identity. */
export function BoxPlot({ groups, title, selected, onSelect, onGroup, height = 250, compact = false, benchmarkLabel }: {
  groups: BoxGroup[]; title: string; selected?: string | null; onSelect?: (id: string) => void;
  onGroup?: (key: string) => void; height?: number; compact?: boolean; benchmarkLabel?: string;
}) {
  const { t, i18n } = useTranslation();
  const element = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!element.current) return;
    const chart = echarts.init(element.current);
    const box = (s: BoxGroup["stats"]) => s.n ? [s.whisker_low, s.q25, s.median, s.q75, s.whisker_high].map(Number) : ["-", "-", "-", "-", "-"];
    chart.setOption({
      animation: false,
      grid: { left: compact ? 4 : 12, right: compact ? 4 : 64, top: compact ? 5 : 30, bottom: compact ? 6 : 32 },
      toolbox: compact ? undefined : { left: 4, top: 0, feature: { saveAsImage: { title: t("ui.analysis.save_png"), name: title, pixelRatio: 2 } } },
      tooltip: { trigger: "item", confine: true, formatter: (item: { seriesType: string; seriesName: string; dataIndex: number; data: { label?: string; value: number[] } }) => {
        if (item.seriesType === "scatter") return `${escapeHtml(item.data.label ?? "")}<br/><b>${pct(item.data.value[1], 2)}</b>`;
        const g = groups[item.dataIndex];
        const s = item.seriesName === benchmarkLabel ? g.benchmark! : g.stats;
        return `${escapeHtml(g.label)} · ${escapeHtml(item.seriesName)}<br/>${t("ui.analysis.col.median")} ${pct(s.median, 2)}<br/>${t("ui.analysis.col.quartiles")} ${pct(s.q25, 2)} – ${pct(s.q75, 2)}<br/>${t("ui.analysis.box.whiskers")} ${pct(s.whisker_low, 2)} – ${pct(s.whisker_high, 2)}<br/>N ${s.n}`;
      } },
      xAxis: { type: "category", data: groups.map((g) => g.label), show: !compact, axisTick: { show: false }, axisLabel: { fontSize: 11, hideOverlap: true } },
      yAxis: { type: "value", position: "right", show: !compact, axisLabel: { formatter: (v: number) => pct(v) }, splitLine: { lineStyle: { color: "#eef2f6" } } },
      series: [
        { type: "boxplot", name: t("ui.analysis.box.sample"), data: groups.map((g) => ({ value: box(g.stats), itemStyle: { color: "#e2e8f0", borderColor: "#334155", borderWidth: 1.5 } })), boxWidth: compact ? [12, 24] : [16, 45] },
        ...(benchmarkLabel ? [{ type: "boxplot", name: benchmarkLabel, data: groups.map((g) => box(g.benchmark ?? { n: 0 } as BoxGroup["stats"])), boxWidth: [10, 30], itemStyle: { color: "#fff", borderColor: "#64748b", opacity: 0.65 } }] : []),
        { type: "scatter", name: t("ui.analysis.box.observations"), z: 5, data: groups.flatMap((g, index) => g.points.filter((p) => p.value != null).map((p, i) => ({
          value: [index, Number(p.value)], id: p.id, label: p.label, group: g.key,
          symbolOffset: [((i % 7) - 3) * (compact ? 2 : 4), 0],
          symbol: p.current ? "diamond" : "circle",
          symbolSize: selected === p.id ? 12 : compact ? 4 : 6,
          itemStyle: { color: Number(p.value) >= 0 ? "#2156d9" : "#c2531a", opacity: selected && selected !== p.id ? 0.2 : 0.8, borderColor: "#334155", borderWidth: selected === p.id || (!p.current && g.stats.outliers?.some((v) => Number(v) === Number(p.value))) ? 1.5 : 0 },
        }))) },
      ],
    });
    chart.on("click", (raw) => {
      const item = raw as unknown as { seriesType: string; dataIndex: number; data: { id: string; group: string } };
      if (item.seriesType === "scatter") { onSelect?.(item.data.id); onGroup?.(item.data.group); }
      else onGroup?.(groups[item.dataIndex].key);
    });
    const observer = new ResizeObserver(() => chart.resize()); observer.observe(element.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [groups, title, selected, onSelect, onGroup, compact, benchmarkLabel, t, i18n.language]);
  return <div ref={element} style={{ height }} className="w-full" role="img" aria-label={title} />;
}
