import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { CandlestickChart, ScatterChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useReducedMotion } from "motion/react";
import { locale } from "@/i18n";

echarts.use([CandlestickChart, ScatterChart, GridComponent, TooltipComponent, MarkLineComponent, DataZoomComponent, CanvasRenderer]);

// Same blue (up / buy) and orange (down / sell) as the CSS tokens, as canvas colors.
const UP = "#2156d9";
const DOWN = "#c2531a";
const INK = "#334155";

export type Bar = { date: string; open?: string | null; high?: string | null; low?: string | null; close?: string | null };
export type Marker = { id: string; date: string; price: number; side: "buy" | "sell"; size: number; label: string };
export type DayLine = { date: string; label: string };

/**
 * Daily candles (blue up, orange down) with insider buys and sells on top:
 * blue triangles up for purchases, orange triangles down for sales, sized by
 * amount. Clicking a marker reports its row.
 */
export function PriceChart({
  bars,
  markers = [],
  lines = [],
  height = 300,
  selected,
  onSelect,
  zoom = false,
}: {
  bars: Bar[];
  markers?: Marker[];
  lines?: DayLine[];
  height?: number;
  selected?: string | null;
  onSelect?: (id: string) => void;
  zoom?: boolean;
}) {
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
      const id = (event.data as { id?: string } | undefined)?.id;
      if (id) select.current?.(id);
    });
    return () => {
      resize.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    const dates = bars.map((bar) => bar.date);
    const index = new Map(dates.map((day, position) => [day, position]));
    // A trade on a non-session day sits on the next session's candle.
    const place = (day: string) => dates.find((value) => value >= day) ?? dates.at(-1);
    const number = new Intl.NumberFormat(locale(), { maximumFractionDigits: 2 });
    const series = (side: "buy" | "sell") => ({
      type: "scatter" as const,
      name: side,
      z: 5,
      symbol: "triangle",
      symbolRotate: side === "buy" ? 0 : 180,
      symbolSize: (_value: unknown, params: { data?: { size?: number } }) => params.data?.size ?? 10,
      itemStyle: { color: side === "buy" ? UP : DOWN, borderColor: "#fff", borderWidth: 1, opacity: 0.9 },
      emphasis: { scale: 1.3 },
      data: markers
        .filter((marker) => marker.side === side && place(marker.date))
        .map((marker) => ({
          id: marker.id,
          value: [place(marker.date)!, marker.price],
          size: marker.size,
          label: marker.label,
          itemStyle: marker.id === selected ? { borderColor: INK, borderWidth: 2.5, opacity: 1 } : undefined,
        })),
    });
    instance.setOption(
      {
        animation: !reduce,
        animationDuration: 250,
        grid: { left: 8, right: 56, top: 12, bottom: zoom ? 44 : 24, containLabel: false },
        tooltip: {
          trigger: "item",
          confine: true,
          formatter: (params: { seriesType?: string; data?: { label?: string } | unknown[]; name?: string }) => {
            if (params.seriesType === "scatter") return (params.data as { label?: string })?.label ?? "";
            const value = params.data as (string | number)[];
            if (!Array.isArray(value)) return "";
            const [, open, close, low, high] = value;
            return `${params.name}<br/>O ${number.format(Number(open))} · H ${number.format(Number(high))}<br/>L ${number.format(Number(low))} · C ${number.format(Number(close))}`;
          },
        },
        xAxis: {
          type: "category",
          data: dates,
          boundaryGap: true,
          axisLine: { lineStyle: { color: "#cbd5e1" } },
          axisTick: { show: false },
          axisLabel: { color: "#64748b", fontSize: 11, hideOverlap: true, formatter: (value: string) => value.slice(5) },
        },
        yAxis: {
          type: "value",
          scale: true,
          position: "right",
          splitLine: { lineStyle: { color: "#eef2f6" } },
          axisLabel: { color: "#64748b", fontSize: 11 },
        },
        dataZoom: zoom ? [{ type: "inside" }, { type: "slider", height: 18, bottom: 6, borderColor: "transparent", showDetail: false }] : [],
        series: [
          {
            type: "candlestick",
            name: "price",
            data: bars.map((bar) => [Number(bar.open), Number(bar.close), Number(bar.low), Number(bar.high)]),
            itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
            markLine: lines.length
              ? {
                  symbol: "none",
                  silent: true,
                  label: { formatter: "{b}", color: INK, fontSize: 11, position: "insideEndTop" },
                  lineStyle: { color: INK, type: "dashed", width: 1 },
                  data: lines
                    .filter((line) => place(line.date))
                    .map((line) => ({ name: line.label, xAxis: index.get(place(line.date)!) })),
                }
              : undefined,
          },
          series("buy"),
          series("sell"),
        ],
      },
      { notMerge: true },
    );
  }, [bars, markers, lines, selected, reduce, zoom]);

  return <div ref={element} style={{ height }} className="w-full" role="img" />;
}
