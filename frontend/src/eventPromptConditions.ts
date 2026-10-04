export function eventPromptConditions(
  params: URLSearchParams,
  kind: "custom" | "earnings",
  language: "zh" | "en",
): string {
  const zh = language === "zh";
  const quarter = params.get("quarter");
  const years = params.get("historical_years");
  const start = params.get("year_start");
  const end = params.get("year_end");
  const selectedYears = params.getAll("years").flatMap((value) => value.split(/[,，\s]+/)).filter(Boolean);
  const excludedYears = params.getAll("excluded_years").flatMap((value) => value.split(/[,，\s]+/)).filter(Boolean);
  const currentFiscalYear = params.get("current_fiscal_year");
  const unit = kind === "earnings" ? "财年" : "自然年";
  const englishUnit = kind === "earnings" ? "fiscal" : "calendar";
  const lookupYears = [...new Set([
    ...selectedYears.filter((value) => !excludedYears.includes(value)),
    ...(kind === "earnings" && currentFiscalYear ? [currentFiscalYear] : []),
  ])].map(Number).sort((a, b) => a - b);
  const nonContiguous = selectedYears.length > 0 && lookupYears.every((value) => Number.isInteger(value) && value > 0)
    && lookupYears.some((value, index) => index > 0 && value !== lookupYears[index - 1] + 1);
  return [
    zh ? "沟通语言：中文" : "Communication language: English",
    zh ? `研究类型：${kind === "earnings" ? "财报业绩首次发布" : "自定义事件"}`
       : `Research type: ${kind === "earnings" ? "earnings first release" : "custom event"}`,
    params.get("company") ? `${zh ? "公司" : "Company"}: ${params.get("company")}` : "",
    params.get("event_type") || params.get("date_category")
      ? `${zh ? "事件类别" : "Event type"}: ${params.get("event_type") ?? params.get("date_category")}`
      : "",
    params.get("ticker") || params.get("tickers")
      ? `${zh ? "证券代码" : "Security ticker"}: ${params.get("ticker") ?? params.get("tickers")}`
      : "",
    years && !selectedYears.length && !(start && end)
      ? zh
        ? `希望研究过去 ${years} 个完整${kind === "earnings" ? "财年" : "自然年"}，请确认具体起止年份。`
        : `Study the past ${years} complete ${kind === "earnings" ? "fiscal" : "calendar"} years; confirm exact start and end years.`
      : "",
    quarter ? (zh ? `仅财季：Q${quarter}` : `Fiscal quarter: only Q${quarter}`) : "",
    selectedYears.length
      ? zh ? `指定历史${unit}（仅这些年份）：${selectedYears.join("、")}` : `Historical ${englishUnit} years (only these years): ${selectedYears.join(", ")}`
      : start && end ? (zh ? `年份：${start}—${end}` : `Years: ${start}–${end}`) : "",
    excludedYears.length
      ? zh ? `排除历史${unit}：${excludedYears.join("、")}` : `Excluded historical ${englishUnit} years: ${excludedYears.join(", ")}`
      : "",
    kind === "earnings"
      ? currentFiscalYear
        ? zh ? `当前对照财年：${currentFiscalYear}（纳入资料查询，单独列示，不计入历史样本）` : `Current comparison fiscal year: ${currentFiscalYear} (include in the lookup separately; not a historical sample)`
        : zh ? "当前对照财年未知：不推断，不额外纳入未知当前财年。" : "Current comparison fiscal year is unknown: do not infer or add an unspecified current fiscal year."
      : "",
    nonContiguous
      ? zh
        ? "资料查询年份非连续；当前导入格式只支持起止年，先说明限制并确认拆分查询方案，不得自动扩大为连续年份范围或把未请求年份计入覆盖。确认前不要返回猜测的 JSON。"
        : "Lookup years are non-contiguous. The current import format only supports a start/end range: first explain this limit and ask the user to confirm a split-query plan; do not automatically widen the range or include unrequested years in coverage. Do not return guessed JSON before confirmation."
      : "",
  ].filter(Boolean).join(zh ? "；" : "; ");
}

const PREFILL_KEYS = [
  "company", "ticker", "tickers", "event_type", "date_category",
  "historical_years", "year_start", "year_end", "years", "excluded_years", "current_fiscal_year", "quarter", "language",
] as const;

export function eventPromptDraftKey(
  params: URLSearchParams,
  kind: "custom" | "earnings",
): string {
  const setId = params.get("set");
  if (setId) return `event-draft:${kind}:${setId}`;
  const draftId = params.get("draft");
  if (draftId) return `event-draft:${kind}:new:${draftId}`;
  const prefill = new URLSearchParams();
  for (const key of PREFILL_KEYS) {
    for (const value of params.getAll(key)) prefill.append(key, value);
  }
  const signature = prefill.toString();
  return signature
    ? `event-draft:${kind}:new:prefill:${signature}`
    : `event-draft:${kind}:new`;
}

/** Match the previous generated prefill exactly before upgrading a saved draft. */
export function restoreEventPromptConditions(
  current: string,
  params: URLSearchParams,
  kind: "custom" | "earnings",
  language: "zh" | "en",
): string {
  const legacyParams = new URLSearchParams(params);
  for (const key of ["years", "excluded_years", "current_fiscal_year"]) legacyParams.delete(key);
  const separator = language === "zh" ? "；" : "; ";
  const generated = eventPromptConditions(legacyParams, kind, language);
  // The old template omitted the unknown-current-year sentence and always
  // included historical_years even when year_start/year_end were present.
  let legacy = generated.split(separator).filter((line) => !line.startsWith("当前对照财年未知") && !line.startsWith("Current comparison fiscal year is unknown")).join(separator);
  if (params.get("historical_years") && params.get("year_start") && params.get("year_end")) {
    const range = language === "zh"
      ? `希望研究过去 ${params.get("historical_years")} 个完整${kind === "earnings" ? "财年" : "自然年"}，请确认具体起止年份。`
      : `Study the past ${params.get("historical_years")} complete ${kind === "earnings" ? "fiscal" : "calendar"} years; confirm exact start and end years.`;
    const marker = params.get("quarter") ? (language === "zh" ? "仅财季：" : "Fiscal quarter:") : (language === "zh" ? "年份：" : "Years:");
    const index = legacy.indexOf(marker);
    legacy = index >= 0 ? legacy.slice(0, index) + range + separator + legacy.slice(index) : legacy;
  }
  return current === legacy ? eventPromptConditions(params, kind, language) : current;
}

/** Translate only our untouched prefill; free-form user conditions remain verbatim. */
export function eventConditionsForLanguage(
  current: string,
  params: URLSearchParams,
  kind: "custom" | "earnings",
  previous: "zh" | "en",
  next: "zh" | "en",
): string {
  return restoreEventPromptConditions(current, params, kind, previous) === eventPromptConditions(params, kind, previous)
    ? eventPromptConditions(params, kind, next)
    : current;
}

export function eventCommonYearsLabel(quarters: unknown): string {
  const values = Array.isArray(quarters) && quarters.length > 0
    && quarters.every((value) => Number.isInteger(value) && value >= 1 && value <= 4)
    ? [...new Set(quarters as number[])].sort((a, b) => a - b)
    : [];
  return values.length === 4
    ? "只比较四季共同完整财年（Q1—Q4；各窗口分别核对）"
    : `只比较所请求财季的共同完整财年（${values.length ? values.map((value) => `Q${value}`).join("、") : "范围待确认"}；各窗口分别核对）`;
}
