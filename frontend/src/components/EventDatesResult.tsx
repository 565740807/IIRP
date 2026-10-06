import { EventOverlapDetails } from "./EventOverlapDetails";
import { useMemo } from "react";
import { useReadingState } from "../researchStorage";
import {
  display,
  facts,
  formatNumber,
  formatTime,
  percent,
  rows,
  sourceLabel,
  type Fact,
} from "../api";
import { ResearchChart } from "./ResearchChart";
import { eventExportIdentity } from "../eventExportIdentity";
import { fiscalVersionNotice } from "../fiscalVersionNotice";
import { Button, EmptyState } from "./ui";
import { StatisticsPanel } from "./StatisticsPanel";
import { StatisticsPanel as ResearchEvidence } from "../StatisticsPanel";
import { useFiscalCalendar } from "./FiscalCalendar";

const labels: Record<string, string> = {
  earnings_release: "业绩发布",
  developer_keynote: "开发者大会主题演讲",
  wwdc_keynote: "WWDC 主题演讲",
  iphone_launch: "iPhone 发布会",
  mac_launch: "Mac 发布会",
  product_launch: "产品发布会",
  unassigned_earnings: "财期归属待核对",
  occurred: "已发生",
  scheduled: "计划事件",
  cancelled: "已取消",
  unknown: "未知",
  supported: "资料有日期依据，仍需人工核对",
  conflicting: "日期冲突",
  unverified: "待核对",
  searched: "资料报告已查询",
  partial: "部分查询",
  not_searched: "尚未查询",
  events_found: "找到事件",
  not_found: "未找到可靠资料",
  confirmed_none: "有依据确认未举办",
  unresolved: "尚未解决",
  minute: "分钟时刻",
  date: "只有日期",
  official_schedule: "官方日程时刻",
  reported_actual: "实际时刻报道",
  before_open: "盘前",
  during_session: "盘中",
  after_close: "盘后",
  regular: "标准财期",
  transition: "过渡财期",
  historical: "历史事件",
  current: "当前年对照",
  observation: "尚不能归入历史统计：请确认当前财年或事件年份",
  before_listing: "上市前无行情",
  unverified_event_date: "事件日期待核对",
  unsupported_event_date: "日期缺乏支持资料",
  event_not_confirmed_occurred: "尚未确认实际发生",
  unassigned_year: "年份归属待核对",
  unverified_or_nonstandard_fiscal_period: "财期待核对或属于非标准财期",
  duplicate_fiscal_period: "同一财季有多个主发布候选",
  incomplete_full_window: "完整窗口仍有缺口或尚未形成",
  missing_or_conflicting_event_date: "事件日期缺失或冲突",
  user_excluded: "用户已排除",
  not_historical_year: "不计入历史样本",
};
export const eventLabel = (value: unknown) =>
  labels[String(value)] ?? sourceLabel(value);
const windows = [
  ["before5", "前 5 个交易日"],
  ["day0", "对齐日 D0"],
  ["after5", "后 5 个交易日"],
  ["through5", "D0 至后第 5 日（含当天）"],
] as const;
const position = (value: number) =>
  value === 0 ? "D0" : `D${value > 0 ? "+" : "−"}${Math.abs(value)}`;
function sourceUrl(value: unknown) {
  try {
    const url = new URL(String(value));
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
export function EventDateSources({ sources }: { sources: unknown }) {
  return (
    <div>
      {rows(sources).map((source, index) => {
        const url = sourceUrl(source.url);
        return (
          <p key={`${source.url}-${index}`}>
            {url ? (
              <a
                className="text-link"
                href={url}
                target="_blank"
                rel="noreferrer"
              >
                {display(source.title ?? source.publisher)} ↗
              </a>
            ) : (
              <span>来源地址无效</span>
            )}
            {source.publisher && (
              <small>
                {display(source.publisher)} ·{" "}
                {source.source_kind === "primary"
                  ? "一手来源"
                  : source.source_kind === "secondary"
                    ? "二手来源"
                    : "来源类型待核对"}
                {source.published_date
                  ? ` · 发布于 ${source.published_date}`
                  : ""}
              </small>
            )}
            {source.evidence_note && (
              <small>{display(source.evidence_note)}</small>
            )}
            {Array.isArray(source.supports) && source.supports.length === 0 && (
              <small>未说明支持哪些事实；补充对应依据后再核对。</small>
            )}
          </p>
        );
      })}
    </div>
  );
}
export function EventDatesResult({
  data,
  title = "事件当天与前后 5 个交易日",
  csvUrl,
  jsonUrl,
  onSelect,
  busy = false,
  readingScope,
  resultId,
  analysisId,
  securitySymbol,
  nativeEarnings = false,
}: {
  data: Fact;
  title?: string;
  csvUrl?: string;
  jsonUrl?: string;
  onSelect?: (selection: { metric?: string; group?: string }) => void;
  busy?: boolean;
  readingScope?: string;
  resultId?: string;
  analysisId?: string;
  securitySymbol?: string;
  nativeEarnings?: boolean;
}) {
  const readingKey = readingScope
    ? `event-research:${readingScope}`
    : `event-result:${facts(data.metadata).event_version_id ?? "automatic"}:${facts(data.metadata).dataset_id ?? "missing"}:${JSON.stringify(facts(data.metadata).params ?? {})}`;
  const meta = facts(data.metadata);
  const exportIdentity = eventExportIdentity(meta, securitySymbol, nativeEarnings);
  const calendar = useFiscalCalendar(data, readingKey + ":categories");
  const category =
    facts(meta.params).date_category ??
    (meta.quarter ? `Q${meta.quarter}` : "");
  const metric = String(meta.date_window ?? "after5");
  const legacy = !Array.isArray(data.window_series);
  const windowLabel = windows.find(([key]) => key === metric)?.[1] ?? metric;
  const windowDefinition =
    meta.window_start != null
      ? `${position(meta.window_start)} 收盘 → ${position(meta.window_end)} 收盘`
      : "D−1 收盘归零 · 保留版本";
  const [year, setYear] = useReadingState(`${readingKey}:year`, "");
  const [chosenKey, setChosenKey] = useReadingState(`${readingKey}:chosen`, "");
  const [showPending, setShowPending] = useReadingState(
    `${readingKey}:pending`,
    false,
  );
  const [curveKeys, setCurveKeys] = useReadingState<string[]>(
    `${readingKey}:curves`,
    [],
  );
  const [page, setPage] = useReadingState(`${readingKey}:page`, 0);
  const allRows = rows(data.rows);
  const summaries = rows(data.category_summary);
  const categories = [...new Set(allRows.map((row) => String(row.category)))];
  const currentCategory = category || categories[0] || "";
  const pending = (row: Fact) =>
    row.date_verified !== true ||
    row.event_status !== "occurred" ||
    (Array.isArray(row.exclusion_reasons) &&
      row.exclusion_reasons.includes("user_excluded"));
  const filtered = allRows
    .filter(
      (row) =>
        row.category === currentCategory &&
        (!year || String(row.year) === year) &&
        (showPending || !pending(row)),
    )
    .sort((a, b) =>
      String(facts(b.anchor).original_date ?? "").localeCompare(
        String(facts(a.anchor).original_date ?? ""),
      ),
    );
  const chosen = filtered.find((row) => row.key === chosenKey) ?? filtered[0];
  const anchor = facts(chosen?.anchor);
  const summary = summaries.find((item) => item.category === currentCategory);
  const curveIds = [
    ...new Set([chosen?.key, ...curveKeys].filter(Boolean)),
  ].slice(0, 5);
  // Background task/freshness updates must not replace an unchanged chart option
  // and clear the user's stationary tooltip. Every actual reading choice remains a dependency.
  const chart = useMemo(
    () => ({
      benchmark: data.benchmark,
      metadata: {
        ...meta,
        symbol: exportIdentity.symbol,
        alignment: "trading",
      },
      series: rows(data.window_series ?? data.series)
        .filter(
          (item) =>
            item.category === currentCategory &&
            curveIds.includes(item.key) &&
            filtered.some((row) => row.key === item.key),
        )
        .map((item) => ({
          ...item,
          group: item.key === chosen?.key ? "current" : "comparison",
          label: `${display(item.year)} · ${item.label}`,
        })),
      summary: { path: rows(summary?.window_path ?? summary?.path) },
    }),
    [
      data,
      currentCategory,
      chosen?.key,
      curveKeys,
      year,
      showPending,
      exportIdentity.symbol,
    ],
  );
  const lastPage = Math.max(0, Math.ceil(filtered.length / 25) - 1);
  const currentPage = Math.min(page, lastPage);
  return (
    <section className="panel event-dates-result">
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          <p>
            按真实事件日期对齐 · 当前窗口：{windowLabel} · {windowDefinition}
          </p>
        </div>
        {(csvUrl || jsonUrl) && (
          <details className="result-notes">
            <summary>导出当前版本</summary>
            <div className="button-row">
              {csvUrl && (
                <a className="button button-secondary" href={csvUrl}>
                  导出此结果 CSV
                </a>
              )}
              {jsonUrl && (
                <a className="button button-secondary" href={jsonUrl}>
                  导出此结果 JSON
                </a>
              )}
            </div>
          </details>
        )}
      </div>
      {!nativeEarnings && meta.event_set_id && fiscalVersionNotice("event_import", meta.calculation_version) && (
        <p className="reason" role="status">
          {fiscalVersionNotice("event_import", meta.calculation_version)}
        </p>
      )}
      <p className="context-baseline">
        行情截至 {display(meta.cutoff_date)} · {sourceLabel(meta.price_basis)}
        {meta.event_version != null ? ` · 事件版本 ${meta.event_version}` : ""}
      </p>
      <details className="result-notes">
        <summary>计算与日历版本</summary>
        <p>
          计算版本 {display(meta.calculation_version)} · 日历{" "}
          {display(meta.calendar_version)}
        </p>
      </details>
      <ResearchEvidence
        data={data}
        defaultMetric={metric}
        group={currentCategory}
        readingId={readingScope}
      />
      <StatisticsPanel
        key={String(
          meta.event_version_id ?? meta.dataset_id ?? meta.cutoff_date,
        )}
        data={data}
        defaultMetric={metric}
        group={currentCategory}
        onSelect={onSelect}
        busy={busy}
        title="日期观察 · 历史窗口涨跌幅分布"
      />
      {summaries.length > 0 && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>事件类型 / 财季</th>
                {data.fiscal_coverage && (
                  <>
                    <th>财季覆盖月份</th>
                    <th>财报公布月份</th>
                  </>
                )}
                <th>事件数 / 年数</th>
                <th>完整历史样本</th>
                {windows.slice(0, 3).map(([key, label]) => (
                  <th key={key}>{label}中位变化 / N</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {summaries.map((item) => (
                <tr key={item.category}>
                  <td>{eventLabel(item.category)}</td>
                  {data.fiscal_coverage &&
                    (/^Q[1-4]$/.test(item.category) ? (
                      calendar.cells(item.category)
                    ) : (
                      <td colSpan={2}>财季归属待核对，月份未归入标准季度</td>
                    ))}
                  <td>
                    {display(item.event_count)} 次 / {display(item.year_count)}{" "}
                    年
                  </td>
                  <td>
                    {display(item.effective_n)} /{" "}
                    {display(item.historical_event_count)}
                    <small>
                      其中无可靠分钟时刻 {display(item.date_only_n)} 次
                    </small>
                    {Number(item.effective_n) <= 1 && (
                      <small>
                        {Number(item.effective_n) === 0
                          ? "暂无完整有效历史样本"
                          : "仅 1 个完整样本"}
                      </small>
                    )}
                  </td>
                  {windows.slice(0, 3).map(([key]) => (
                    <td key={key}>
                      {percent(facts(facts(item.windows)[key]).median)}
                      <small>
                        有效 N={display(facts(facts(item.windows)[key]).n)}
                      </small>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="context-baseline">
        各类型分别汇总，每次事件等权；当前年独立对照。各窗口与曲线位置的有效 N
        可不同，价格完整不能代替日期核对。
      </p>
      {calendar.details}
      <div className="analysis-form">
        <label>
          事件类型 / 财季
          <select
            aria-label="事件类型 / 财季"
            value={currentCategory}
            disabled={!onSelect || busy}
            onChange={(event) => {
              onSelect?.({ group: event.target.value });
              setYear("");
              setCurveKeys([]);
              setPage(0);
            }}
          >
            {[...new Set([currentCategory, ...categories])]
              .filter(Boolean)
              .map((item) => (
                <option key={item} value={item}>
                  {eventLabel(item)}
                </option>
              ))}
          </select>
        </label>
        <label>
          查看某年明细（统计范围见上方）
          <select
            value={year}
            onChange={(event) => {
              setYear(event.target.value);
              setCurveKeys([]);
              setPage(0);
            }}
          >
            <option value="">全部年份</option>
            {[
              ...new Set(
                allRows
                  .filter(
                    (row) =>
                      row.category === currentCategory && row.year != null,
                  )
                  .map((row) => String(row.year)),
              ),
            ]
              .sort()
              .reverse()
              .map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={showPending}
            onChange={(event) => {
              setShowPending(event.target.checked);
              setPage(0);
            }}
          />
          查看待核对、计划与已排除观察（{allRows.filter(pending).length} 次）
        </label>
      </div>
      {filtered.length ? (
        <details className="result-notes">
          <summary>
            逐年事件与每日涨跌明细（{filtered.length} 次，点击选择事件）
          </summary>
          <div className="table-scroll">
            <table>
              <caption>
                每日收盘涨跌：选择一次事件查看实际日期、来源与完整指标
              </caption>
              <thead>
                <tr>
                  <th>年份 / 事件</th>
                  <th>事件原日期</th>
                  {Array.from({ length: 11 }, (_, i) => (
                    <th key={i}>{position(i - 5)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered
                  .slice(currentPage * 25, (currentPage + 1) * 25)
                  .map((row) => (
                    <tr key={row.key} aria-selected={row.key === chosen?.key}>
                      <td>
                        <Button
                          variant="ghost"
                          onClick={() => setChosenKey(row.key)}
                        >
                          {display(row.year)} · {row.label}
                        </Button>
                        <small>
                          重叠事件：
                          {row.overlap?.total ??
                            row.overlapping_event_ids?.length ??
                            "未记录"}{" "}
                          次
                        </small>
                        <small>
                          {eventLabel(row.group)} ·{" "}
                          {row.date_verified ? "日期已核对" : "日期待核对"}
                        </small>
                      </td>
                      <td>
                        {display(facts(row.anchor).original_date)}
                        <small>
                          D0：{display(facts(row.anchor).anchor_date)}
                        </small>
                        {row.event_time && (
                          <small>
                            {row.event_time} · {display(row.timezone)}
                          </small>
                        )}
                      </td>
                      {Array.from({ length: 11 }, (_, i) => {
                        const point = rows(row.points).find(
                          (p) => Number(p.x) === i - 5,
                        );
                        return (
                          <td
                            key={i}
                            title={
                              point
                                ? `${point.date} · ${eventLabel(point.daily_status)}`
                                : "日期尚未对齐"
                            }
                            className={
                              point?.daily_return == null
                                ? "muted"
                                : Number(point.daily_return) >= 0
                                  ? "trade-buy"
                                  : "trade-sell"
                            }
                          >
                            {percent(point?.daily_return)}
                            <small>{display(point?.date)}</small>
                            {point?.daily_status !== "available" && (
                              <small>
                                {point
                                  ? eventLabel(point.daily_status)
                                  : "日期待核对"}
                              </small>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {filtered.length > 25 && (
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
        </details>
      ) : (
        <EmptyState
          title="当前条件没有可展示的已核对事件"
          description="可切换事件类型或年份，或打开待核对观察。已保存事件与排除原因仍然保留。"
        />
      )}
      {chosen && (
        <>
          <div className="section-heading">
            <div>
              <h3>{chosen.label}</h3>
              <p>
                {eventLabel(chosen.group)} · 实际日期{" "}
                {display(anchor.original_date)}
                {chosen.event_time
                  ? ` ${chosen.event_time} ${chosen.timezone}`
                  : " · 事件具体时刻未知，按日期观察"}
              </p>
              <p className="muted">
                首次系统观察{" "}
                {chosen.first_observed_at
                  ? formatTime(chosen.first_observed_at, "America/New_York")
                  : "未记录"}
                {" · "}日期核验{" "}
                {chosen.date_verified_at
                  ? formatTime(chosen.date_verified_at, "America/New_York")
                  : "未记录"}
                {" · "}来源发表时间见逐项来源，不能由首次观察或核验时间反推。
              </p>
            </div>
          </div>
          <div className="panel-body">
            <p>
              D0：{display(anchor.anchor_date)} · 当前窗口归零基准：
              {display(
                facts(facts(chosen.windows)[metric]).start_date ??
                  anchor.baseline_date,
              )}{" "}
              · 首个后续交易日：
              {display(anchor.first_subsequent_session)}
            </p>
            {chosen.fiscal_year != null && (
              <p>
                FY{chosen.fiscal_year} Q{display(chosen.fiscal_quarter)} ·
                财季实际覆盖 {display(chosen.period_start)} →{" "}
                {display(chosen.period_end)} ·{" "}
                {chosen.period_verified
                  ? "已确认财年/财季归属"
                  : "财年/财季未确认，暂不计入历史排名；可在事件来源管理中修订核对"}
                {!chosen.period_start &&
                  "；来源缺少财期起点，未按自然季度推算月份"}
              </p>
            )}
            {Array.isArray(anchor.warnings) &&
              anchor.warnings.map((message: string) => (
                <p className="reason" key={message}>
                  {message}
                </p>
              ))}
            <EventOverlapDetails
              key={`${analysisId}:${resultId}:${chosen.key}`}
              analysisId={analysisId}
              resultId={resultId}
              eventKey={String(chosen.key)}
              summary={chosen.overlap}
              legacyIds={chosen.overlapping_event_ids}
            />
            {Array.isArray(chosen.exclusion_reasons) &&
              chosen.exclusion_reasons.length > 0 && (
                <p>
                  未纳入完整历史汇总的原因：
                  {chosen.exclusion_reasons.map(eventLabel).join("；")}
                </p>
              )}
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>观察窗口</th>
                  <th>累计变化</th>
                  <th>实际起止收盘日期</th>
                  <th>价格覆盖</th>
                  <th>历史样本资格</th>
                </tr>
              </thead>
              <tbody>
                {windows.map(([key, label]) => {
                  const item = facts(facts(chosen.windows)[key]);
                  return (
                    <tr key={key}>
                      <td>{label}</td>
                      <td>{percent(item.value)}</td>
                      <td>
                        {display(item.start_date)} → {display(item.end_date)}
                      </td>
                      <td>
                        {item.complete ? "完整" : eventLabel(item.status)}
                        <small>
                          {display(item.available_closes)} /{" "}
                          {display(item.expected_closes)} 个收盘
                        </small>
                      </td>
                      <td>
                        {item.eligible
                          ? "计入该窗口历史 N"
                          : "不计入该窗口历史 N"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <details className="result-notes">
            <summary>添加历年对照曲线（最多 5 次事件）</summary>
            <div className="ticker-checkboxes">
              {filtered.map((row) => (
                <label key={row.key}>
                  <input
                    type="checkbox"
                    checked={curveIds.includes(row.key)}
                    disabled={
                      row.key === chosen.key ||
                      (!curveIds.includes(row.key) && curveIds.length >= 5)
                    }
                    onChange={(event) =>
                      setCurveKeys((keys) =>
                        event.target.checked
                          ? [...keys, row.key]
                          : keys.filter((key) => key !== row.key),
                      )
                    }
                  />
                  {display(row.year)} · {row.label}
                </label>
              ))}
            </div>
          </details>
          <ResearchChart
            data={chart}
            readingId={`${readingKey}:chart:${chosen.key}:${metric}`}
            sampleTitleSuffix={
              legacy ? "完整日期观察 D−5…D+5（旧版）" : `${windowLabel}日期观察`
            }
            title={
              legacy
                ? `${chosen.label} · 完整日期观察 D−5…D+5（旧版）`
                : `${chosen.label} · ${windowLabel}日期观察`
            }
            provenance={`${windowDefinition} · ${sourceLabel(meta.price_basis)} · 行情截至 ${display(meta.cutoff_date)}\n结果 ${resultId ?? "未记录"} · ${exportIdentity.factVersion}\n行情获取于 ${display(meta.price_fetched_at ?? meta.price_as_of)}\n计算 ${display(meta.calculation_version)} · 日历 ${display(meta.calendar_version)}`}
            exportNotes={[
              `证券 ${exportIdentity.symbol} · ${display(currentCategory)} · ${windowLabel} · ${windowDefinition}`,
              `历史目标 ${Array.isArray(facts(meta.params).years) ? facts(meta.params).years.join("、") : meta.historical_years ? `${meta.historical_years} 个完整年` : "见研究条件"} · 所选${windowLabel}有效 N=${display(facts(facts(summary?.windows)[metric]).n ?? 0)} · 当前事件 N=${filtered.filter((row) => row.group === "current").length}`,
              `所选事件 ${chosen?.label ?? "无"} · 实际 D0 ${display(anchor.anchor_date)} · 原始事件日期 ${display(anchor.original_date)}`,
              ...filtered.filter((row) => curveIds.includes(row.key)).map((row) => `${display(row.year)} · 实际 D0 ${display(facts(row.anchor).anchor_date)} · ${display(facts(facts(row.windows)[metric]).start_date)} → ${display(facts(facts(row.windows)[metric]).end_date)} · ${facts(facts(row.windows)[metric]).complete ? "窗口完整" : "窗口不完整"}`),
              `结果版本 ${resultId ?? "未记录"} · ${exportIdentity.factVersion}`,
            ]}
          />
          <details className="result-notes">
            <summary>事件来源、逐日价格与额外基准</summary>
            <EventDateSources sources={chosen.sources} />
            <p>
              前 5 个交易日额外基准 D−6：{display(chosen.baseline_extra_date)} ·
              收盘{" "}
              {chosen.baseline_extra_close == null
                ? "缺少"
                : formatNumber(chosen.baseline_extra_close)}
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>位置</th>
                    <th>实际交易日</th>
                    <th>收盘价</th>
                    <th>较前收盘涨跌</th>
                    <th>相对 D−1 收盘</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {rows(chosen.points).map((point) => (
                    <tr key={point.x}>
                      <td>{position(Number(point.x))}</td>
                      <td>{point.date}</td>
                      <td>
                        {point.close == null ? "—" : formatNumber(point.close)}
                      </td>
                      <td>{percent(point.daily_return)}</td>
                      <td>{percent(point.value)}</td>
                      <td>
                        {eventLabel(point.daily_status)} ·{" "}
                        {eventLabel(point.normalized_status)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
      <p className="panel-footer">
        这里描述事件日期附近的历史股价表现；同期涨跌不证明由事件造成。当前页面筛选只调整显示，分类汇总沿用同一冻结版本的合格历史样本。
      </p>
    </section>
  );
}
