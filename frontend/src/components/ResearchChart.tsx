import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { LineChart, HeatmapChart, CustomChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
  TitleComponent,
  DataZoomComponent,
  AriaComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  facts,
  rows,
  percent,
  display,
  formatNumber,
  sourceLabel,
  type Fact,
} from "../api";
import { Button, EmptyState } from "./ui";
import { useReadingState } from "../researchStorage";
import { chartExportContext } from "../chartExportContext";
import { chartSampleLabel } from "../chartSampleLabel";
echarts.use([
  LineChart,
  HeatmapChart,
  CustomChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
  TitleComponent,
  DataZoomComponent,
  AriaComponent,
  CanvasRenderer,
]);
const number = (v: unknown) =>
  v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v);
const observation = (data: Fact, series: Fact) =>
  data.kind === "earnings" &&
  rows(data.rows).find(
    (row) => String(row.event_id) === String(series.sourceKey ?? series.key),
  )?.precise !== true;
const seriesLabel = (data: Fact, series: Fact) =>
  `${String(series.label ?? series.key)}${observation(data, series) ? "（日期观察）" : ""}`;

function ChartDataTable({ groups, price }: { groups: Fact[]; price: boolean }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [opened, setOpened] = useState(false);
  const chosen = Math.max(
    0,
    groups.findIndex((group, index) => String(group.key ?? index) === selected),
  );
  const points = rows(groups[chosen]?.points);
  const lastPage = Math.max(0, Math.ceil(points.length / 50) - 1);
  const currentPage = Math.min(page, lastPage);
  return (
    <details
      className="result-notes"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>查看图表数据与缺口</summary>
      {opened && (
        <>
          {groups.length > 1 && (
            <label>
              选择路径
              <select
                value={String(groups[chosen]?.key ?? chosen)}
                onChange={(event) => {
                  setSelected(event.target.value);
                  setPage(0);
                }}
              >
                {groups.map((group, index) => (
                  <option
                    key={group.key ?? index}
                    value={String(group.key ?? index)}
                  >
                    {display(group.label ?? group.key)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>位置 / 分组</th>
                  <th>实际日期</th>
                  <th>{price ? "价格" : "累计变化"}</th>
                  <th>有效 N</th>
                  <th>状态与缺口</th>
                </tr>
              </thead>
              <tbody>
                {points
                  .slice(currentPage * 50, (currentPage + 1) * 50)
                  .map((point, index) => (
                    <tr key={index}>
                      <td>{display(point.label ?? point.x)}</td>
                      <td>
                        {display(
                          point.date ??
                            point.actual_start ??
                            point.announced_date,
                        )}
                        {point.actual_end ? ` → ${point.actual_end}` : ""}
                      </td>
                      <td>
                        {price
                          ? number(point.value) == null
                            ? "—"
                            : formatNumber(Number(point.value))
                          : percent(number(point.value))}
                      </td>
                      <td>{display(point.n)}</td>
                      <td>
                        {point.status
                          ? sourceLabel(point.status)
                          : number(point.value) == null
                            ? "缺少价格或尚未形成"
                            : "可用"}
                        {point.observation && (
                          <small>日期观察，时点尚不满足精确统计条件</small>
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {points.length > 50 && (
            <div className="pagination">
              <Button
                disabled={currentPage === 0}
                onClick={() => setPage(currentPage - 1)}
              >
                上一页
              </Button>
              <span>
                {currentPage + 1} / {lastPage + 1}
              </span>
              <Button
                disabled={currentPage === lastPage}
                onClick={() => setPage(currentPage + 1)}
              >
                下一页
              </Button>
            </div>
          )}
        </>
      )}
    </details>
  );
}
export function ResearchChart({
  data,
  title,
  sampleTitleSuffix,
  readingId,
  provenance = "",
  exportNotes = [],
  heatmap = false,
  onCell,
}: {
  data: Fact;
  title: string;
  sampleTitleSuffix?: string;
  readingId?: string;
  provenance?: string;
  exportNotes?: string[];
  heatmap?: boolean;
  onCell?: (cell: Fact) => void;
}) {
  const el = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.EChartsType | null>(null);
  const [chartWidth, setChartWidth] = useState(0);
  const click = useRef(onCell);
  click.current = onCell;
  const originalSeries = rows(data.series);
  const [benchmarkYear, setBenchmarkYear] = useReadingState(
    `chart:${readingId ?? `${title}:${provenance}`}:year`,
    "",
  );
  const paired =
    originalSeries.find((s) => s.key === benchmarkYear) ??
    originalSeries.find(
      (s) =>
        s.group === "current" && rows(s.points).some((p) => p.value != null),
    ) ??
    originalSeries[0];
  // Keep the saved reading key stable while the visible title follows the sample.
  const chartTitle =
    sampleTitleSuffix && data.benchmark && paired
      ? `${paired.label} · ${sampleTitleSuffix}`
      : title;
  const series =
      data.benchmark && !heatmap && paired
        ? [
            { ...paired, label: `${paired.label} · 股票`, group: "current" },
            {
              ...paired,
              // The rendering key is unique; fact eligibility belongs to the original event.
              sourceKey: paired.key,
              key: `${paired.key}-benchmark`,
              label: `${paired.label} · ${display(facts(data.benchmark).name)}`,
              group: "benchmark",
              points: rows(paired.points).map((p) => ({
                ...p,
                value: p.benchmark,
                status: p.benchmark_status,
              })),
            },
            {
              ...paired,
              sourceKey: paired.key,
              key: `${paired.key}-difference`,
              label: `${paired.label} · 领先/落后（百分点）`,
              group: "difference",
              points: rows(paired.points).map((p) => ({
                ...p,
                value: p.difference,
                status: p.benchmark_status,
              })),
            },
          ]
        : originalSeries,
    cells = rows(data.cells);
  const hasData = heatmap
    ? cells.length > 0
    : series.some((s) => rows(s.points).some((p) => number(p.value) != null)) ||
      rows(facts(data.summary).path).some((p) => number(p.median) != null);
  const missing = heatmap
    ? cells.filter((cell) => number(cell.endpoint) == null).length
    : series
        .flatMap((item) => rows(item.points))
        .filter((point) => number(point.value) == null).length;
  const description = `${chartTitle}。${heatmap ? `${cells.length} 个分组` : `${series.length} 条单独路径`}，${missing} 个位置尚无数值。缺少或尚未形成的价格保留空白，不连接缺口。可展开下方“查看图表数据与缺口”阅读具体日期、数值和状态。`;
  const tableGroups = heatmap
    ? [
        {
          key: "cells",
          label: title,
          points: cells.map((cell) => ({
            ...cell,
            label: `${cell.year ?? cell.fiscal_year} · ${cell.month ? `${cell.month}月` : `Q${cell.quarter}`}`,
            value: cell.endpoint,
            observation: data.kind === "earnings" && cell.precise !== true,
          })),
        },
      ]
    : [
        ...series.map((item) => ({
          ...item,
          label: seriesLabel(data, item),
          points: rows(item.points).map((point) => ({
            ...point,
            observation: observation(data, item),
          })),
        })),
        ...(!data.benchmark && chartSampleLabel(rows(facts(data.summary).path))
          ? [
              {
                key: "summary",
                label: chartSampleLabel(rows(facts(data.summary).path)) ?? "",
                points: rows(facts(data.summary).path).map((point) => ({
                  ...point,
                  value: point.median,
                  date: "各年对应日期见单独路径",
                })),
              },
            ]
          : []),
      ];
  useEffect(() => {
    if (!el.current || !hasData) return;
    const chart = echarts.init(el.current, undefined, { renderer: "canvas" });
    instance.current = chart;
    const observer = new ResizeObserver(() => {
      if (instance.current !== chart || chart.isDisposed() || !el.current?.isConnected) return;
      chart.resize();
      setChartWidth(chart.getWidth());
    });
    observer.observe(el.current);
    return () => {
      observer.disconnect();
      if (instance.current === chart) instance.current = null;
      if (!chart.isDisposed()) chart.dispose();
    };
  }, [hasData]);
  useEffect(() => {
    const chart = instance.current;
    if (!chart || chart.isDisposed()) return;
    // Preserve user legend choices when the same research receives another
    // data version. Financial series are replaced atomically below.
    const savedLegend = facts(rows(chart.getOption()?.legend)[0]);
    const savedSelection = readingId ? facts(savedLegend.selected) : {};
    const footer = provenance || display(facts(data.metadata).price_basis);
    const textWidth = Math.max(180, (chartWidth || chart.getWidth()) - 40);
    const footerHeight = new echarts.graphic.Text({
      style: {
        text: footer,
        width: textWidth,
        fontSize: 10,
        lineHeight: 13,
        fontFamily: '-apple-system,"PingFang SC",sans-serif',
        overflow: "break",
      },
    }).getBoundingRect().height;
    const headerBottom = Math.max(64, 42 + footerHeight);
    const price = data.value_kind === "price";
    const valueLabel = (v: unknown) =>
      data.value_kind === "price"
        ? v == null
          ? "—"
          : formatNumber(Number(v))
        : percent(v);
    const base = {
      animation: false,
      backgroundColor: "#fff",
      aria: { enabled: true, label: { enabled: true, description } },
      textStyle: {
        fontFamily: '-apple-system,"PingFang SC",sans-serif',
        color: "#56657a",
      },
      title: {
        text: chartTitle,
        textStyle: { fontSize: 14, color: "#172033", fontWeight: 500 },
        subtext: footer,
        subtextStyle: {
          fontSize: 10,
          lineHeight: 13,
          width: textWidth,
          overflow: "break",
        },
        left: 15,
        top: 8,
      },
      grid: {
        left: 65,
        right: 32,
        top: headerBottom + 35,
        bottom: 55,
        containLabel: false,
      },
      tooltip: {
        trigger: heatmap ? "item" : "axis",
        renderMode: "richText",
        confine: true,
      },
      toolbox: {},
    };
    if (heatmap) {
      const years = [
        ...new Set(cells.map((c) => String(c.year ?? c.fiscal_year))),
      ];
      const monthly = data.kind !== "earnings";
      const currentMonth = Number(
        new Intl.DateTimeFormat("en-US", {
          timeZone: "America/New_York",
          month: "numeric",
        }).format(new Date()),
      );
      const columns = Array.from(
        { length: monthly ? 12 : 4 },
        (_, i) => `${monthly ? "" : "Q"}${i + 1}${monthly ? "月" : ""}`,
      );
      chart.setOption(
        {
          ...base,
          grid: { left: 65, right: 25, top: headerBottom + 5, bottom: 75 },
          xAxis: {
            type: "category",
            data: columns,
            splitArea: { show: true },
            axisLabel: {
              interval: 0,
              formatter: (value: string) =>
                monthly && value === `${currentMonth}月`
                  ? `{current|${value}\n当前}`
                  : value,
              rich: {
                current: {
                  color: "#1d4ed8",
                  fontWeight: "bold",
                  backgroundColor: "#dbeafe",
                  padding: [4, 3],
                },
              },
            },
          },
          yAxis: { type: "category", data: years, splitArea: { show: true } },
          visualMap: {
            min: -0.2,
            max: 0.2,
            calculable: false,
            orient: "horizontal",
            left: "center",
            bottom: 8,
            inRange: { color: ["#efb5b5", "#f6f8fa", "#a5d7b7"] },
            formatter: (v: number) => percent(v),
          },
          series: [
            {
              type: "heatmap",
              data: cells.map((c) => ({
                value: [
                  Number(c.month ?? c.quarter) - 1,
                  years.indexOf(String(c.year ?? c.fiscal_year)),
                  number(c.endpoint) ?? 0,
                ],
                cell: c,
                itemStyle:
                  number(c.endpoint) == null
                    ? { color: "#edf0f4" }
                    : data.kind === "earnings" && c.precise !== true
                      ? {
                          borderColor: "#a67a31",
                          borderWidth: 1,
                          borderType: "dashed",
                        }
                      : undefined,
                label: {
                  show: true,
                  formatter:
                    number(c.endpoint) == null
                      ? sourceLabel(c.status)
                      : `${percent(number(c.endpoint))}${data.kind === "earnings" && c.precise !== true ? "\n日期观察" : ""}`,
                  color: "#172033",
                  fontSize: 11,
                  width: Math.max(50, (chart.getWidth() - 90) / columns.length - 6),
                  overflow: "break",
                  lineHeight: 12,
                },
              })),
              emphasis: {
                itemStyle: { borderColor: "#2563eb", borderWidth: 2 },
              },
              tooltip: {
                formatter: (p: { data: { cell: Fact } }) => {
                  const c = p.data.cell;
                  return `${c.year ?? c.fiscal_year} · ${c.month ? `${c.month}月` : `Q${c.quarter}`}\n${percent(number(c.endpoint))} · ${sourceLabel(c.status)}${data.kind === "earnings" && c.precise !== true ? " · 日期观察，时点待核对" : ""}\n${display(c.actual_start ?? c.announced_date)} → ${display(c.actual_end)}`;
                },
              },
            },
          ],
        },
        true,
      );
      chart.off("click");
      chart.on("click", (p) => {
        const item = facts(p.data);
        if (item.cell) click.current?.(facts(item.cell));
      });
      return;
    }
    const summary = data.benchmark ? {} : facts(data.summary);
    const path = rows(summary.path);
    const allX = [
      ...new Set([
        ...series.flatMap((s) => rows(s.points).map((p) => Number(p.x))),
        ...path.map((p) => Number(p.x)),
      ]),
    ]
      .filter(Number.isFinite)
      .sort((a, b) => a - b);
    const lines = series.map((s) => ({
      name: seriesLabel(data, s),
      type: "line",
      showSymbol: false,
      connectNulls: false,
      data: allX.map((x) => {
        const p = rows(s.points).find((p) => Number(p.x) === x);
        return {
          value: p ? number(p.value) : null,
          actual: p?.date,
          n: p?.n,
          status: p?.status,
        };
      }),
      lineStyle: {
        width: s.group === "current" ? 3 : 1.2,
        opacity: observation(data, s) ? 0.5 : s.group === "current" ? 1 : 0.65,
        type: observation(data, s) ? "dashed" : "solid",
      },
      itemStyle: s.group === "current" ? { color: "#2563eb" } : undefined,
    }));
    const median = chartSampleLabel(path)
      ? [
          {
            name: chartSampleLabel(path) ?? "",
            type: "line",
            showSymbol: false,
            connectNulls: false,
            lineStyle: { width: 2.5, color: "#172033" },
            itemStyle: { color: "#172033" },
            data: allX.map((x) =>
              number(path.find((p) => Number(p.x) === x)?.median),
            ),
          },
        ]
      : [];
    const bands = path.some((p) => Number(p.n) >= 2)
      ? [
          {
            type: "custom",
            name: "中间 50%（非预测区间）",
            silent: true,
            dimensions: ["position", "lower", "upper"],
            encode: { x: "position", y: ["lower", "upper"] },
            // 让坐标轴同时包含后端返回的上下界，避免色带超出绘图区。
            data: path
              .filter((p) => Number(p.n) >= 2 && number(p.q25) != null && number(p.q75) != null)
              .map((p) => [
                allX.indexOf(Number(p.x)),
                number(p.q25),
                number(p.q75),
              ]),
            clip: true,
            z: 0,
            renderItem: (
              params: { dataIndex: number },
              api: { coord: (v: [number, number]) => number[] },
            ) => {
              if (params.dataIndex !== 0) return null;
              const segments: Fact[][] = [];
              let segment: Fact[] = [];
              for (const x of allX) {
                const p = path.find((p) => Number(p.x) === x);
                if (p && Number(p.n) >= 2 && number(p.q25) != null && number(p.q75) != null) {
                  segment.push(p);
                } else if (segment.length) {
                  segments.push(segment);
                  segment = [];
                }
              }
              if (segment.length) segments.push(segment);
              return {
                type: "group",
                children: segments.map((s) => ({
                  type: "polygon",
                  shape: {
                    points: [
                      ...s.map((p) =>
                        api.coord([allX.indexOf(Number(p.x)), Number(p.q25)]),
                      ),
                      ...s
                        .slice()
                        .reverse()
                        .map((p) =>
                          api.coord([allX.indexOf(Number(p.x)), Number(p.q75)]),
                        ),
                    ],
                  },
                  style: { fill: "rgba(37,99,235,.10)" },
                })),
              };
            },
          },
        ]
      : [];
    chart.setOption(
      {
        ...base,
        legend: {
          type: "scroll",
          data: [
            ...bands.map((s) => s.name),
            ...median.map((s) => s.name),
            ...series
              .filter((s) => s.group !== "historical")
              .map((s) => seriesLabel(data, s)),
            ...series
              .filter((s) => s.group === "historical")
              .map((s) => seriesLabel(data, s)),
          ],
          top: headerBottom,
          left: 60,
          right: 20,
          selected: {
            ...Object.fromEntries(
              series
                .filter((s) => s.group === "historical")
                .map((s) => [seriesLabel(data, s), false]),
            ),
            ...savedSelection,
          },
          scrollDataIndex: readingId ? savedLegend.scrollDataIndex : 0,
        },
        xAxis: {
          type: "category",
          data: price
            ? allX.map(
                (x) =>
                  rows(series[0]?.points).find((p) => Number(p.x) === x)
                    ?.date ?? x,
              )
            : allX,
          name: price
            ? "实际交易日期"
            : display(facts(data.metadata).alignment) === "calendar"
              ? "日历位置"
              : "交易日位置",
          nameLocation: "middle",
          nameGap: 32,
          boundaryGap: false,
        },
        yAxis: {
          type: "value",
          scale: data.value_kind === "price",
          axisLabel: { formatter: (v: number) => valueLabel(v) },
          splitLine: { lineStyle: { color: "#edf1f6" } },
        },
        tooltip: {
          trigger: "axis",
          renderMode: "richText",
          confine: true,
          formatter: (input: unknown) => {
            const entries = Array.isArray(input) ? input : [];
            const x = Number(entries[0]?.axisValue);
            const p = path.find((p) => Number(p.x) === x);
            return [
              `位置 ${x}${p ? ` · 有效 N=${p.n ?? 0}` : ""}`,
              ...entries
                .filter((e) => e.seriesType === "line")
                .map(
                  (e) =>
                    `${e.seriesName}：${valueLabel(number(e.value))}${e.data?.actual ? `（${e.data.actual}）` : ""}${e.data?.status && e.data.status !== "available" ? `\n  ${sourceLabel(e.data.status)}` : ""}`,
                ),
            ].join("\n");
          },
        },
        series: [...bands, ...lines, ...median],
      },
      true,
    );
  }, [
    data,
    paired,
    chartTitle,
    provenance,
    heatmap,
    hasData,
    chartWidth,
    description,
    readingId,
  ]);
  function download() {
    const chart = instance.current;
    if (!chart || chart.isDisposed()) return;
    const original = chart.getOption();
    const legend = facts(rows(original.legend)[0]);
    const grid = facts(rows(original.grid)[0]);
    const originalHeight = chart.getHeight();
    if (!heatmap && Object.keys(legend).length) {
      const selection = facts(legend.selected);
      const labels = (
        Array.isArray(legend.data)
          ? legend.data
          : rows(original.series).map((s) => s.name)
      )
        .map(String)
        .filter((name) => selection[name] !== false);
      let used = 0,
        lines = 1;
      for (const label of labels) {
        const width =
          new echarts.graphic.Text({
            style: {
              text: label,
              fontSize: 12,
              fontFamily: '-apple-system,"PingFang SC",sans-serif',
            },
          }).getBoundingRect().width + 45;
        if (used && used + width > chart.getWidth() - 90) {
          lines++;
          used = 0;
        }
        used += width;
      }
      const extra = (lines - 1) * 25;
      chart.resize({ height: originalHeight + extra, silent: true });
      chart.setOption({
        legend: { ...legend, type: "plain", data: labels },
        grid: { ...grid, top: Number(grid.top) + extra },
      });
    }
    const imageUrl = chart.getDataURL({
      type: "png",
      pixelRatio: 2,
      backgroundColor: "#fff",
      excludeComponents: ["toolbox"],
    });
    const context = chartExportContext(
      data, provenance, exportNotes,
      data.benchmark && !heatmap && paired ? String(paired.label) : undefined,
    );
    const filename = `${chartTitle.replace(/[^\p{L}\p{N}-]/gu, "_")}.png`;
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      const scratch = canvas.getContext("2d");
      if (!scratch) return;
      scratch.font = '24px -apple-system,"PingFang SC",sans-serif';
      const maxWidth = image.width - 56;
      const wrapped: string[] = [];
      for (const line of context) {
        let current = "";
        for (const char of line) {
          if (current && scratch.measureText(current + char).width > maxWidth) {
            wrapped.push(current);
            current = "";
          }
          current += char;
        }
        wrapped.push(current);
      }
      const header = Math.max(90, wrapped.length * 34 + 32);
      canvas.height = image.height + header;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#172033";
      ctx.font = '24px -apple-system,"PingFang SC",sans-serif';
      wrapped.forEach((line, index) => ctx.fillText(line, 28, 34 + index * 34));
      ctx.drawImage(image, 0, header);
      const a = document.createElement("a");
      a.download = filename;
      a.href = canvas.toDataURL("image/png");
      a.click();
    };
    image.src = imageUrl;
    if (!heatmap) {
      chart.setOption(
        { legend: original.legend, grid: original.grid },
        { replaceMerge: ["legend", "grid"] },
      );
      if (!chart.isDisposed()) chart.resize({ height: originalHeight, silent: true });
    }
  }
  return (
    <div className="chart-wrap">
      {data.benchmark && !heatmap && (
        <label>
          同一实际日期的走势图对照
          <select
            value={paired?.key ?? ""}
            onChange={(e) => setBenchmarkYear(e.target.value)}
          >
            {originalSeries.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
          <small>
            股票、{display(facts(data.benchmark).name)}
            及逐日差值；缺失保留空白。
          </small>
        </label>
      )}
      {hasData ? (
        <>
          <div
            className={heatmap ? "heatmap-scroll" : undefined}
            role={heatmap ? "region" : undefined}
            tabIndex={heatmap ? 0 : undefined}
            aria-label={heatmap ? `${title}，窄屏可左右滚动查看全部列` : undefined}
          >
            <div
              ref={el}
              className={`research-chart ${heatmap ? `heatmap-chart ${data.kind === "earnings" ? "quarter-heatmap" : "monthly-heatmap"}` : ""}`}
              role="img"
              aria-label={description}
            />
          </div>
          <p className="context-baseline">
            {heatmap
              ? "灰色单元格表示尚无数值。窄屏可左右滚动查看全部列，键盘聚焦图表区域后可用方向键滚动。"
              : "缺少或尚未形成的价格保留空白，曲线不跨缺口连接。"}
            {data.kind === "earnings" &&
              "日期观察以虚线或边框标明，不代表已核对的精确事件。"}
            {!heatmap && (data.benchmark && paired
              ? `当前图仅展示 ${paired.label} 这一个样本的股票、基准及差值，三条曲线不代表三个独立样本。全部历史统计的 N 见上方摘要。`
              : rows(facts(data.summary).path).length > 0 &&
                "汇总曲线按各位置可用样本统计；期末汇总按完整样本统计。位置 N 可在悬停和数据表查看。")}
          </p>
          <div className="chart-actions">
            <Button variant="ghost" onClick={download}>
              导出此图 PNG
            </Button>
            <span>图片包含当前图例、范围及版本说明</span>
          </div>
        </>
      ) : (
        <EmptyState
          title="这个范围尚无可绘制的价格路径"
          description="已获取的记录与具体缺口保留在下方；补齐后会生成新结果版本。"
        />
      )}
      {tableGroups.length > 0 && (
        <ChartDataTable
          groups={tableGroups}
          price={data.value_kind === "price"}
        />
      )}
    </div>
  );
}
