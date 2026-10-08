import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts/core";
import { CandlestickChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import { GridComponent, MarkLineComponent, ToolboxComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useReducedMotion } from "motion/react";
import { benchmarkName, pct } from "@/lib/analysis";
import { fiscalLabel, offsetLabel, sessionLabel, type EventResult, type EventRow } from "@/lib/events";
import { formatDay, formatPrice } from "@/lib/format";

echarts.use([CandlestickChart, CustomChart, LineChart, ScatterChart, GridComponent, TooltipComponent, MarkLineComponent, ToolboxComponent, CanvasRenderer]);

// The blue (up) and orange (down) of the CSS tokens, as canvas colors.
const UP = "#2156d9";
const DOWN = "#c2531a";
const INK = "#334155";
const MUTED = "#64748b";
const GRID = "#eef2f6";
const AXIS = "#cbd5e1";

/** One ECharts instance bound to a div, resized with it and disposed with it. */
function useChart(option: () => echarts.EChartsCoreOption, deps: unknown[]) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
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
    chart.current?.setOption(option(), { notMerge: true });
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return element;
}

function toolbox(title: string, label: string) {
  return {
    left: 4,
    top: 0,
    itemSize: 14,
    iconStyle: { borderColor: MUTED },
    feature: { saveAsImage: { title: label, name: title.replace(/[^\w-]+/g, "_"), pixelRatio: 2, backgroundColor: "#ffffff" } },
  };
}

const percentAxis = (value: number) => pct(value, Math.abs(value) < 0.1 ? 1 : 0);

/** "Oct 31, 2025" plus FY and session, for tooltips. */
function eventHead(row: EventRow) {
  const fiscal = fiscalLabel(row);
  return `<b>${row.name}</b>${fiscal ? ` · ${fiscal}` : ""}<br/>${formatDay(row.date, { year: true })} · ${sessionLabel(row.session)}`;
}

/**
 * Main chart A: one candle per event, the reaction day R drawn from the
 * previous close C(R−1) = 0. The open is the gap, the body runs open → close,
 * the wicks are the day's high and low. The color is the reaction (blue up,
 * orange down against C(R−1)); hollow bodies closed above their open, filled
 * ones below. Ordered by date; the benchmark's reaction day is a gray dash.
 */
export function ReactionCandles({ result, benchmark, title, height = 300 }: { result: EventResult; benchmark: string | null; title: string; height?: number }) {
  const { t, i18n } = useTranslation();
  const reduce = useReducedMotion();
  const element = useChart(() => {
    const rows = result.rows;
    const labels = rows.map((row) => (row.fiscal_year ? `${String(row.fiscal_year).slice(2)}Q${row.fiscal_quarter}` : row.date.slice(0, 7)));
    const data = rows.map((row) => {
      const candle = row.reaction_candle;
      if (!candle) return { value: ["-", "-", "-", "-"] };
      const open = Number(candle.open ?? 0);
      const close = Number(candle.close);
      // Color = the reaction (close against C(R−1)); hollow = closed above its open, filled = below.
      const color = close >= 0 ? UP : DOWN;
      return {
        value: [open, close, Number(candle.low ?? Math.min(open, close)), Number(candle.high ?? Math.max(open, close))],
        itemStyle: { color: "#ffffff", color0: color, borderColor: color, borderColor0: color, borderWidth: 1.5 },
      };
    });
    return {
      animation: !reduce,
      animationDuration: 250,
      grid: { left: 8, right: 56, top: 30, bottom: 28 },
      toolbox: toolbox(title, t("ui.analysis.save_png")),
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        confine: true,
        formatter: (items: { dataIndex: number }[]) => {
          const row = rows[items[0]?.dataIndex ?? -1];
          if (!row) return "";
          const head = `${eventHead(row)} · R ${formatDay(row.reaction_date, { year: true })}`;
          const candle = row.reaction_candle;
          if (!candle) return `${head}<br/>${t("ui.events.no_reaction_yet")}`;
          const lines = [
            head,
            `${t("ui.events.window.gap")} ${pct(candle.open, 2)} · ${t("ui.events.window.reaction")} <b style="color:${Number(candle.close) >= 0 ? UP : DOWN}">${pct(candle.close, 2)}</b>`,
            `${t("ui.analysis.col.high")} ${pct(candle.high, 2)} · ${t("ui.analysis.col.low")} ${pct(candle.low, 2)}`,
          ];
          const paired = row.windows.reaction?.benchmark;
          if (benchmark && paired != null) lines.push(`${benchmarkName(benchmark)} ${pct(paired, 2)}`);
          return lines.join("<br/>");
        },
      },
      xAxis: { type: "category", data: labels, axisLine: { lineStyle: { color: AXIS } }, axisTick: { show: false }, axisLabel: { color: MUTED, fontSize: 11, hideOverlap: true } },
      yAxis: { type: "value", position: "right", splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: MUTED, fontSize: 11, formatter: percentAxis } },
      series: [
        {
          type: "candlestick",
          name: title,
          barMaxWidth: 22,
          data,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: INK, width: 1, opacity: 0.5 }, data: [{ yAxis: 0 }] },
        },
        ...(benchmark
          ? [{
              type: "scatter" as const,
              name: benchmarkName(benchmark),
              symbol: "rect",
              symbolSize: [14, 2.5],
              itemStyle: { color: INK, opacity: 0.75 },
              data: rows.map((row) => (row.windows.reaction?.benchmark == null ? "-" : Number(row.windows.reaction.benchmark))),
              z: 5,
            }]
          : []),
      ],
    };
  }, [result, benchmark, title, reduce, t, i18n.language]);
  return <div ref={element} style={{ height }} className="w-full" role="img" aria-label={title} />;
}

/**
 * Main chart B: the average path from R−n to R+n, cumulative from C(R−1). The
 * line is the median of all events, the band the middle half (25th–75th
 * percentile); the benchmark's median path is dashed.
 */
export function PathChart({ result, benchmark, showBenchmark, title, height = 280 }: { result: EventResult; benchmark: string | null; showBenchmark: boolean; title: string; height?: number }) {
  const { t, i18n } = useTranslation();
  const reduce = useReducedMotion();
  const element = useChart(() => {
    const points = result.path;
    const labels = points.map((point) => offsetLabel(point.offset));
    const value = (text: string | null | undefined) => (text == null ? "-" : Number(text));
    // The middle half as one polygon: q75 left to right, then q25 back.
    const band = points.flatMap((point, index) => (point.q25 == null || point.q75 == null ? [] : [[index, Number(point.q25), Number(point.q75)]]));
    return {
      animation: !reduce,
      animationDuration: 250,
      grid: { left: 8, right: 56, top: 30, bottom: 28 },
      toolbox: toolbox(title, t("ui.analysis.save_png")),
      tooltip: {
        trigger: "axis",
        confine: true,
        formatter: (items: { dataIndex: number }[]) => {
          const point = points[items[0]?.dataIndex ?? -1];
          if (!point) return "";
          const lines = [
            `<b>${offsetLabel(point.offset)}</b> · N ${point.n}`,
            `${t("ui.analysis.col.median")} <b>${pct(point.median, 2)}</b>`,
            `${t("ui.events.middle_half")} ${pct(point.q25, 1)} ${t("ui.analysis.range_to")} ${pct(point.q75, 1)}`,
          ];
          if (benchmark && showBenchmark) lines.push(`${benchmarkName(benchmark)} ${pct(point.benchmark_median, 2)}`);
          return lines.join("<br/>");
        },
      },
      xAxis: {
        type: "category",
        data: labels,
        boundaryGap: false,
        axisLine: { lineStyle: { color: AXIS } },
        axisTick: { show: false },
        axisLabel: { color: MUTED, fontSize: 11, hideOverlap: true },
      },
      yAxis: { type: "value", position: "right", splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: MUTED, fontSize: 11, formatter: percentAxis } },
      series: [
        {
          type: "custom",
          name: t("ui.events.middle_half"),
          data: band,
          encode: { x: 0, y: [1, 2] },
          silent: true,
          z: 1,
          renderItem: (params: { dataIndex: number }, api: { coord: (value: number[]) => number[] }) => {
            if (params.dataIndex !== 0 || band.length < 2) return null;
            const upper = band.map(([index, , high]) => api.coord([index, high]));
            const lower = band.map(([index, low]) => api.coord([index, low])).reverse();
            return { type: "polygon", shape: { points: [...upper, ...lower] }, style: { fill: "#94a3b8", opacity: 0.25 } };
          },
        },
        {
          type: "line",
          name: t("ui.analysis.col.median"),
          data: points.map((point) => value(point.median)),
          symbol: "circle",
          symbolSize: 5,
          lineStyle: { color: INK, width: 2 },
          itemStyle: { color: INK },
          markLine: {
            symbol: "none",
            silent: true,
            label: { show: false },
            data: [
              { yAxis: 0, lineStyle: { color: INK, width: 1, opacity: 0.4, type: "solid" } },
              { xAxis: "R", lineStyle: { color: MUTED, width: 1, type: "dashed" } },
            ],
          },
        },
        ...(benchmark && showBenchmark
          ? [{
              type: "line" as const,
              name: benchmarkName(benchmark),
              data: points.map((point) => value(point.benchmark_median)),
              symbol: "none",
              lineStyle: { color: MUTED, width: 1.5, type: "dashed" as const },
            }]
          : []),
      ],
    };
  }, [result, benchmark, showBenchmark, title, reduce, t, i18n.language]);
  return <div ref={element} style={{ height }} className="w-full" role="img" aria-label={title} />;
}

/** One event's daily candles from R−n to R+n (prices), R marked; the dashed line is C(R−1). */
export function EventKline({ row, height = 220 }: { row: EventRow; height?: number }) {
  const { t, i18n } = useTranslation();
  const element = useChart(() => {
    const candles = row.candles;
    const base = candles.find((candle) => candle.date === row.baseline_date)?.close;
    // The server's C(t)/C(R−1) − 1 per offset; nothing is recomputed here.
    const path: Record<number, (typeof row.path)[number] | undefined> = Object.fromEntries(row.path.map((point) => [point.offset, point]));
    return {
      animation: false,
      grid: { left: 8, right: 64, top: 12, bottom: 36 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        confine: true,
        formatter: (items: { dataIndex: number }[]) => {
          const candle = candles[items[0]?.dataIndex ?? -1];
          if (!candle) return "";
          if (candle.close == null) return `<b>${offsetLabel(candle.offset)}</b> · ${formatDay(candle.date, { year: true })}<br/>${t("ui.events.not_formed")}`;
          return [
            `<b>${offsetLabel(candle.offset)}</b> · ${formatDay(candle.date, { year: true })}`,
            `${t("ui.analysis.col.open")} ${formatPrice(candle.open)} · ${t("ui.analysis.col.close")} ${formatPrice(candle.close)}`,
            `${t("ui.analysis.col.high")} ${formatPrice(candle.high)} · ${t("ui.analysis.col.low")} ${formatPrice(candle.low)}`,
            path[candle.offset]?.value != null ? `${t("ui.events.from_base")} ${pct(path[candle.offset]!.value, 2)}` : "",
          ].filter(Boolean).join("<br/>");
        },
      },
      xAxis: {
        type: "category",
        data: candles.map((candle) => `${offsetLabel(candle.offset)}\n${formatDay(candle.date, { year: false })}`),
        axisLine: { lineStyle: { color: AXIS } },
        axisTick: { show: false },
        axisLabel: { color: MUTED, fontSize: 10, hideOverlap: true },
      },
      yAxis: { type: "value", scale: true, position: "right", splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: MUTED, fontSize: 11 } },
      series: [
        {
          type: "candlestick",
          barMaxWidth: 18,
          data: candles.map((candle) => (candle.open == null || candle.close == null ? ["-", "-", "-", "-"] : [Number(candle.open), Number(candle.close), Number(candle.low ?? candle.open), Number(candle.high ?? candle.open)])),
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markLine: {
            symbol: "none",
            silent: true,
            label: { show: false },
            data: [
              { xAxis: candles.findIndex((candle) => candle.offset === 0), lineStyle: { color: MUTED, type: "dashed", width: 1 } },
              ...(base ? [{ yAxis: Number(base), lineStyle: { color: INK, opacity: 0.45, type: "dashed" as const, width: 1 } }] : []),
            ],
          },
        },
      ],
    };
  }, [row, t, i18n.language]);
  return <div ref={element} style={{ height }} className="w-full" role="img" aria-label={t("ui.events.kline_label", { name: row.name })} />;
}
