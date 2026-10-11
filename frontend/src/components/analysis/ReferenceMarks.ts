import { CHART } from "@/lib/colors";

/** Short dashed or dotted strokes distinguish references beside each candle. */
export function referenceMarks(name: string, values: (string | null | undefined)[], sector = false) {
  return {
    type: "custom" as const, name, z: 5,
    data: values.map((value, index) => [index, value == null ? "-" : Number(value)]),
    renderItem: (_params: unknown, api: { value: (dimension: number) => number | string; coord: (value: number[]) => number[] }) => {
      const value = api.value(1);
      if (value === "-" || !Number.isFinite(Number(value))) return null;
      const [x, y] = api.coord([Number(api.value(0)), Number(value)]);
      return { type: "line" as const, shape: { x1: x - 10, x2: x + 10, y1: y, y2: y }, style: { stroke: sector ? CHART.muted : CHART.ink, lineWidth: 2, lineDash: sector ? [1, 3] : [5, 3] } };
    },
  };
}
