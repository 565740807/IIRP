import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts/core";
import { CandlestickChart, ScatterChart } from "echarts/charts";
import { GridComponent, MarkLineComponent, ToolboxComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useReducedMotion } from "motion/react";
import { benchmarkName, pct, yearLabel, type Candle } from "@/lib/analysis";
import { formatDay, formatPrice } from "@/lib/format";

echarts.use([CandlestickChart, ScatterChart, GridComponent, TooltipComponent, MarkLineComponent, ToolboxComponent, CanvasRenderer]);

// The blue (up) and orange (down) of the CSS tokens, as canvas colors.
const UP = "#2156d9";
const DOWN = "#c2531a";
const INK = "#334155";
const MUTED = "#64748b";

/**
 * One candle per year for the chosen month or interval, drawn from a common
 * zero: the open is 0%, the close is the change, the wicks are the period's
 * high and low relative to the open (all computed by the server). The
 * benchmark's change over the same dates is a gray dash. The year in progress
 * is hollow and labelled. The toolbox saves the chart as PNG.
 */
export function YearCandles({
  candles,
  cross,
  benchmark,
  title,
  height = 300,
}: {
  candles: Candle[];
  cross: boolean;
  benchmark: string | null;
  title: string;
  height?: number;
}) {
  const { t, i18n } = useTranslation();
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const reduce = useReducedMotion();

  useEffect(() => {
    if (!element.current) return;
    const instance = echarts.init(element.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(element.current);
    return () => {
      resize.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    const shown = candles.filter((candle) => candle.change != null || !candle.current);
    const labels = shown.map((candle) => yearLabel(candle, cross) + (candle.status === "in_progress" ? "*" : ""));
    const percent = (value: number) => pct(value, Math.abs(value) < 0.1 ? 1 : 0);
    const data = shown.map((candle) => {
      if (candle.change == null) return { value: ["-", "-", "-", "-"] };
      const close = Number(candle.change);
      const value = [0, close, Number(candle.low_change ?? Math.min(0, close)), Number(candle.high_change ?? Math.max(0, close))];
      const color = close >= 0 ? UP : DOWN;
      return candle.status === "in_progress"
        ? { value, itemStyle: { color: "#ffffff", color0: "#ffffff", borderColor: color, borderColor0: color, borderType: "dashed" as const, borderWidth: 1.5 } }
        : { value };
    });
    instance.setOption(
      {
        animation: !reduce,
        animationDuration: 250,
        grid: { left: 8, right: 56, top: 30, bottom: 28, containLabel: false },
        toolbox: {
          left: 4,
          top: 0,
          itemSize: 14,
          iconStyle: { borderColor: MUTED },
          feature: { saveAsImage: { title: t("ui.analysis.save_png"), name: title.replace(/[^\w\-]+/g, "_"), pixelRatio: 2, backgroundColor: "#ffffff" } },
        },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          confine: true,
          formatter: (items: { dataIndex: number }[]) => {
            const candle = shown[items[0]?.dataIndex ?? -1];
            if (!candle) return "";
            if (candle.change == null) return `<b>${yearLabel(candle, cross)}</b><br/>${t(`ui.analysis.status.${candle.status}`)}`;
            const rows = [
              `<b>${yearLabel(candle, cross)}</b>${candle.status === "in_progress" ? ` · ${t("ui.analysis.status.in_progress")}` : ""}`,
              `${formatDay(candle.start_date, { year: true })} → ${formatDay(candle.end_date, { year: true })}`,
              `${t("ui.analysis.col.open")} ${formatPrice(candle.open)} → ${t("ui.analysis.col.close")} ${formatPrice(candle.close)}`,
              `<b style="color:${Number(candle.change) >= 0 ? UP : DOWN}">${pct(candle.change, 2)}</b> · ${t("ui.analysis.col.high")} ${pct(candle.high_change, 1)} · ${t("ui.analysis.col.low")} ${pct(candle.low_change, 1)}`,
            ];
            if (benchmark && candle.benchmark_change != null)
              rows.push(`${benchmarkName(benchmark)} ${pct(candle.benchmark_change, 2)} · ${t("ui.analysis.col.excess")} ${pct(candle.excess, 2)}`);
            return rows.join("<br/>");
          },
        },
        xAxis: {
          type: "category",
          data: labels,
          axisLine: { lineStyle: { color: "#cbd5e1" } },
          axisTick: { show: false },
          axisLabel: { color: MUTED, fontSize: 11 },
        },
        yAxis: {
          type: "value",
          position: "right",
          splitLine: { lineStyle: { color: "#eef2f6" } },
          axisLabel: { color: MUTED, fontSize: 11, formatter: percent },
        },
        series: [
          {
            type: "candlestick",
            name: title,
            barMaxWidth: 28,
            data,
            itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
            markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: INK, width: 1, opacity: 0.5 }, data: [{ yAxis: 0 }] },
          },
          ...(benchmark
            ? [
                {
                  type: "scatter" as const,
                  name: benchmarkName(benchmark),
                  symbol: "rect",
                  symbolSize: [18, 2.5],
                  itemStyle: { color: INK, opacity: 0.75 },
                  data: shown.map((candle) => (candle.benchmark_change == null ? "-" : Number(candle.benchmark_change))),
                  z: 5,
                },
              ]
            : []),
        ],
      },
      { notMerge: true },
    );
  }, [candles, cross, benchmark, title, reduce, t, i18n.language]);

  return <div ref={element} style={{ height }} className="w-full" role="img" aria-label={title} />;
}
