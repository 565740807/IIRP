/** Explain a known qualification change without rewriting immutable old results. */
export function fiscalVersionNotice(
  kind: "native_earnings" | "native_event_dates" | "event_import",
  version: unknown,
): string | null {
  const match = typeof version === "string" ? (kind === "native_earnings"
    ? /^research-v(\d+)(?:-|$)/
    : /^event-dates-v(\d+)(?:-|$)/).exec(version) : null;
  if (match && Number(match[1]) >= (kind === "native_earnings" ? 10 : 7)) return null;
  if (!match) return "该冻结结果未记录可识别的计算版本，无法确认其财期资格与覆盖分母口径；请按当前版本新建同条件研究核对。";
  if (kind === "native_earnings" && match && Number(match[1]) === 9)
    return "这是旧版冻结财报结果：公告日期来源及实际公开时刻归因尚未独立核对，精确反应 N 可能偏高。请用当前版本新建同条件研究；旧结果保留供版本复现。";
  if (kind === "native_earnings")
    return "这是旧版冻结财报结果：旧计算可能把未核对常规财期类型或实际公开时刻来源的事件计入历史 N/精确反应。请用当前版本新建同条件研究；旧结果保留供版本复现。";
  if (kind === "native_event_dates")
    return "这是旧版冻结原生财报日期观察结果：旧计算可能把未获来源支持的常规财期计入历史 N；请核对公告日期来源及财期资格，并用当前版本新建同条件研究。旧结果保留供版本复现。";
  return "这是旧版冻结导入事件日期结果：旧计算可能扩大导入年份/财季的覆盖分母；财报日期研究还可能把未获来源支持的常规财期计入 N。请用当前版本新建同条件研究；旧结果保留供版本复现。";
}
