export function chartSampleLabel(path: Array<{n?: unknown}>): string | null {
  if (!path.length || !path.some((point) => Number(point.n) > 0)) return null;
  return path.some((point) => Number(point.n) >= 2)
    ? "历史中位路径"
    : "单一样本路径（N=1）";
}
