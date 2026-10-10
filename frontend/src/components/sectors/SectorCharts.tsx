import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts/core";
import { CandlestickChart } from "echarts/charts";
import { AxisPointerComponent, DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useReducedMotion } from "motion/react";
import { locale } from "@/i18n";
import { formatDay } from "@/lib/format";
import type { SectorCandles, SectorItem } from "@/lib/sectors";
import { Skeleton } from "@/components/ui/skeleton";

echarts.use([CandlestickChart, GridComponent, TooltipComponent, AxisPointerComponent, DataZoomComponent, CanvasRenderer]);

// Same blue (up) and orange (down) as the CSS tokens, as canvas colors.
const UP = "#2156d9";
const DOWN = "#c2531a";
const GROUP = "sector-candles";

type Candle = SectorCandles["items"][number]["candles"][number];

/** Starts rendering once the element comes near the viewport, and stays rendered. */
function useNearViewport() {
  const element = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    if (seen || !element.current) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setSeen(true);
    }, { rootMargin: "200px 0px" });
    observer.observe(element.current);
    return () => observer.disconnect();
  }, [seen]);
  return { element, seen };
}

function CandleChart({ dates, candles, label }: { dates: string[]; candles: Candle[]; label: string }) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const reduce = useReducedMotion();

  useEffect(() => {
    if (!element.current) return;
    const instance = echarts.init(element.current, undefined, { renderer: "canvas" });
    instance.group = GROUP;
    echarts.connect(GROUP);
    chart.current = instance;
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(element.current);
    return () => { resize.disconnect(); instance.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    // Every chart shares the same date axis, so zoom and crosshair line up.
    const byDate = new Map(candles.map((candle) => [candle.date, candle]));
    const number = new Intl.NumberFormat(locale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    instance.setOption({
      animation: !reduce,
      animationDuration: 250,
      grid: { left: 8, right: 56, top: 8, bottom: 22 },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        confine: true,
        formatter: (params: { dataIndex: number }[]) => {
          const candle = byDate.get(dates[params[0]?.dataIndex]);
          if (!candle) return "";
          const range = candle.end_date !== candle.date ? `${formatDay(candle.date, { year: true })} – ${formatDay(candle.end_date, { year: true })}` : formatDay(candle.date, { year: true });
          return `${label}<br/>${range}<br/>O ${number.format(Number(candle.open))} · H ${number.format(Number(candle.high))}<br/>L ${number.format(Number(candle.low))} · C ${number.format(Number(candle.close))}`;
        },
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "#cbd5e1" } },
        axisTick: { show: false },
        axisLabel: { color: "#64748b", fontSize: 10, hideOverlap: true, formatter: (value: string) => value.slice(5) },
      },
      yAxis: {
        type: "value",
        scale: true,
        position: "right",
        splitNumber: 3,
        splitLine: { lineStyle: { color: "#eef2f6" } },
        axisLabel: { color: "#64748b", fontSize: 10 },
      },
      dataZoom: [{ type: "inside" }],
      series: [{
        type: "candlestick",
        data: dates.map((day) => {
          const candle = byDate.get(day);
          return candle ? [Number(candle.open), Number(candle.close), Number(candle.low), Number(candle.high)] : "-";
        }),
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
      }],
    }, { notMerge: true });
  }, [dates, candles, label, reduce]);

  return <div ref={element} className="h-44 w-full"/>;
}

function SectorChartRow({ item, dates, candles }: { item: SectorItem; dates: string[]; candles: Candle[] }) {
  const { t } = useTranslation();
  const { element, seen } = useNearViewport();
  const name = t(`ui.sectors.name.${item.id}`);
  return (
    <div ref={element} className="border-b last:border-b-0">
      <div className="flex items-baseline gap-2 px-3 pt-2 text-sm">
        <span className="font-medium">{name}</span>
        <span className="font-mono text-xs text-muted-foreground">{item.etf}</span>
        {item.first_date && candles.length > 0 && candles[0].date > dates[0] && (
          <span className="text-[11px] text-muted-foreground">{t("ui.sectors.chart_since", { day: formatDay(item.first_date, { year: true }) })}</span>
        )}
      </div>
      {seen
        ? candles.length ? <CandleChart dates={dates} candles={candles} label={`${name} · ${item.etf}`}/>
          : <p className="flex h-44 items-center justify-center text-xs text-muted-foreground">{t("ui.sectors.no_chart")}</p>
        : <Skeleton className="m-3 h-38"/>}
    </div>
  );
}

/** One daily, weekly or monthly K-line per sector, stacked, on one shared range and axis. */
export function SectorCharts({ items, data, loading }: { items: SectorItem[]; data?: SectorCandles; loading: boolean }) {
  const byEtf = useMemo(() => new Map((data?.items ?? []).map((item) => [item.etf, item.candles])), [data]);
  const dates = useMemo(() => [...new Set((data?.items ?? []).flatMap((item) => item.candles.map((candle) => candle.date)))].sort(), [data]);
  if (!data && loading) {
    return <div className="space-y-2 rounded-lg border bg-card p-3">{Array.from({ length: 3 }, (_, index) => <Skeleton key={index} className="h-44"/>)}</div>;
  }
  return (
    <div className="rounded-lg border bg-card">
      {items.map((item) => <SectorChartRow key={item.id} item={item} dates={dates} candles={byEtf.get(item.etf) ?? []}/>)}
    </div>
  );
}
