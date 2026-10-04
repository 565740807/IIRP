import type { ApiFailure } from "./api";

const replacements: Record<string, string> = {
  "company.exchange": "company.exchange_mic（交易所 MIC，例如 XNAS）",
  "company.mic": "company.exchange_mic（交易所 MIC，例如 XNAS）",
  "company.security_type": "无此字段；股类先澄清并在候选核对页确认证券身份",
  "scope.include_future": "scope.include_scheduled（true/false）",
};

export function formatRequestedQuarters(value: unknown): string {
  if (!Array.isArray(value) || !value.length || value.some((quarter) => !Number.isInteger(quarter) || quarter < 1 || quarter > 4))
    return "待确认";
  return value.map((quarter) => `Q${quarter}`).join("、");
}

export function importedScopeHistoricalYears(scope: unknown): number | null {
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return null;
  const values = scope as Record<string, unknown>;
  const start = values.fiscal_year_start ?? values.year_start;
  const end = values.fiscal_year_end ?? values.year_end;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  const count = Number(end) - Number(start) + 1;
  return count >= 1 && count <= 9997 ? count : null;
}

export function importedScopeDateCategory(scope: unknown, kind: string): string | null {
  if (kind !== "earnings" || !scope || typeof scope !== "object" || Array.isArray(scope)) return null;
  const quarters = (scope as Record<string, unknown>).fiscal_quarters;
  return Array.isArray(quarters) && quarters.length === 1 &&
    Number.isInteger(quarters[0]) && quarters[0] >= 1 && quarters[0] <= 4
      ? `Q${quarters[0]}`
      : null;
}

export function requestedQuarterError(raw: string, conditions: string, kind: string): string | null {
  if (kind !== "earnings") return null;
  const explicit = /(?:仅|只看|only)\s*Q\s*([1-4])|Q\s*([1-4])\s*(?:only|仅)|(?:财季|fiscal quarter)\s*[:：]\s*(?:only|仅)?\s*Q\s*([1-4])/i.exec(conditions);
  if (!explicit) return null;
  let payload: any;
  try { payload = JSON.parse(raw.trim().replace(/^```(?:json)?\s*\n|\n```$/g, "")); } catch { return null; }
  const quarters = payload?.scope?.fiscal_quarters;
  const expected = Number(explicit[1] ?? explicit[2] ?? explicit[3]);
  if (!Array.isArray(quarters) || quarters.length !== 1 || quarters[0] !== expected)
    return `已要求仅 Q${expected}，但返回的 scope.fiscal_quarters ${quarters == null ? "缺失（旧格式默认 Q1—Q4）" : "与要求不一致"}。请让 AI 明确写 [${expected}]，核对后再导入。`;
  return null;
}

function supportChoices(schema?: Record<string, any>): string[] {
  const definitions = schema?.$defs ?? {};
  const resolve = (value: any): any => value?.$ref ? definitions[String(value.$ref).split("/").at(-1) ?? ""] : value;
  const event = resolve(schema?.properties?.events?.items);
  const source = resolve(event?.properties?.sources?.items);
  const values = resolve(source?.properties?.supports?.items)?.enum;
  return Array.isArray(values) ? values.filter((item): item is string => typeof item === "string") : [];
}

export function importGuidance(error: ApiFailure, kind: string, conditions: string, inputSchema?: Record<string, any>): {message: string; repair: string} {
  const supports = supportChoices(inputSchema);
  const details = Array.isArray(error.details) ? error.details : [];
  const issues = details.map((item: any) => {
    const path = (item.loc ?? []).filter((part: unknown) => part !== "body").join(".");
    if (item.type === "extra_forbidden") return `不支持 ${path}；${replacements[path] ?? "请只使用下方合法字段"}`;
    if (item.type === "missing") return `缺少必填字段 ${path}`;
    if (item.type === "literal_error" && /\.sources\.\d+\.supports\.\d+$/.test(path) && supports.length)
      return `${path}：支持项不适用于${kind === "earnings" ? "财报" : "自定义事件"}；合法值：${supports.join("、")}`;
    return `${path || "JSON"}：${item.type === "literal_error" ? "取值不在允许范围" : "格式或取值无效"}`;
  });
  const syntax = /line (\d+) column (\d+)/i.exec(error.message);
  const message = issues.length
    ? issues.join("；")
    : syntax && /^(Expecting|Extra data|Unterminated|Invalid control character|Invalid \\escape)/i.test(error.message)
      ? `JSON 语法错误：第 ${syntax[1]} 行、第 ${syntax[2]} 列；请检查引号、逗号和括号。`
      : error.message;
  const fields = kind === "earnings"
    ? "company:{name,ticker,exchange_mic}; scope:{fiscal_year_start,fiscal_year_end,fiscal_quarters,include_scheduled}; events:[{fiscal_year,fiscal_quarter,date,period_kind,sources:[{url,note,supports}]}]"
    : "company:{name,ticker,exchange_mic}; scope:{year_start,year_end,event_types,include_scheduled}; events:[{name,event_type,date,sources:[{url,note,supports}]}]";
  const supportHint = supports.length && issues.some((issue) => issue.includes("支持项")) ? `\n来源 supports 合法值：${supports.join("、")}。` : "";
  const repair = `请只修正以下 JSON 格式问题，保持所有已核实的公司、证券、日期、财年财季、范围、来源与未知状态，不猜测或丢弃条件。\n错误：${message}${supportHint}\n合法结构要点：schema_version、research_as_of、${fields}。未知事实用 null；来源 URL 不等于已核验；JSON 键名和枚举不可随语言翻译。${conditions.trim() ? `\n原始用户条件：${conditions.trim()}` : ""}\n只返回修订后的完整 JSON 对象。`;
  return { message, repair };
}
