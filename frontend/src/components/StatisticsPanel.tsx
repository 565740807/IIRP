import type { components } from "../generated/api";
import { display, facts, percent, rows, sourceLabel, type Fact } from "../api";
import { useReadingState } from "../researchStorage";
import { FiscalReviewLink, useFiscalCalendar } from "./FiscalCalendar";

type Distribution = components["schemas"]["Distribution"];
type Stats = components["schemas"]["DistributionStatistics"];
const statisticValue = (value: unknown, difference = false) =>
  difference ? percent(value).replace(/%$/, " 个百分点") : percent(value);
const metricLabel: Record<string, string> = {
  endpoint: "所选窗口涨跌幅",
  before5: "D−6 至 D−1 · 前5日",
  day0: "D−1 至 D0 · 当日",
  after5: "D0 至 D+5 · 后5日",
  through5: "D−1 至 D+5 · 含当天",
  "1": "精确反应 · 1交易日",
  "5": "精确反应 · 5交易日",
  "20": "精确反应 · 20交易日",
  "60": "精确反应 · 60交易日",
};
export const gapLabel = (value: unknown) =>
  (
    ({
      before_listing: "ETF成立/上市前，无可配对行情",
      benchmark_missing_prices: "基准同一窗口缺行情",
      benchmark_prices_pending: "基准行情待获取",
      benchmark_identity_pending: "基准身份待核对",
      benchmark_identity_unconfirmed: "基准身份未确认",
      benchmark_not_etf_or_index: "来源未确认是ETF或所选指数",
      benchmark_calendar_or_currency_mismatch: "基准日历或币种不匹配",
      missing_event: "缺少该财季实际公告",
      unverified_event_date: "已有资料，日期待核对",
      unsupported_event_date: "已有资料，日期来源尚未支持",
      user_excluded: "已有资料，已主动排除",
      outside_requested_years: "已有资料，不在所选年份范围内",
      unassigned_year: "已有资料，财年归属待核对",
      unverified_or_nonstandard_fiscal_period:
        "已有资料，财年财季或财期类型及来源尚未满足常规汇总资格",
      missing_window: "窗口日期未确定",
      unconfirmed_event_time: "公告时刻/财期资格待核对",
      not_common_fiscal_year: "其他季度同窗口缺少有效样本",
      incomplete_path: "窗口中有价格缺口",
      in_progress: "窗口仍在形成",
      not_yet_formed: "窗口尚未形成",
      unverified_event: "公告事实待核对",
      available: "有效",
      missing_baseline: "缺少起点收盘价",
    }) as Record<string, string>
  )[String(value)] ?? sourceLabel(value);

export function BenchmarkPicker({
  value,
  custom,
  onChange,
}: {
  value: string;
  // Empty ETF entry is a draft choice, distinct from a restored empty benchmark.
  // Its owner must restore it together with the other research conditions.
  custom: boolean;
  onChange: (value: string, custom: boolean) => void;
}) {
  return (
    <>
      <label>
        基准对照
        <select
          value={
            value === "^GSPC" || value === "^IXIC"
              ? value
              : value || custom
                ? "etf"
                : ""
          }
          onChange={(e) => {
            const next = e.target.value;
            onChange(next === "etf" ? "" : next, next === "etf");
          }}
        >
          <option value="">仅查看股票</option>
          <option value="^GSPC">标普 500</option>
          <option value="^IXIC">纳斯达克综合</option>
          <option value="etf">自行选择行业 ETF</option>
        </select>
      </label>
      {((custom && !value) ||
        (value && !["^GSPC", "^IXIC"].includes(value))) && (
        <label>
          行业ETF代码
          <input
            value={value}
            maxLength={20}
            onChange={(e) =>
              onChange(e.target.value.toUpperCase().trim(), true)
            }
            placeholder="输入你选择的ETF代码"
          />
          <small>按来源核对ETF身份。可随时更换；未据公司名称推测行业。</small>
        </label>
      )}
    </>
  );
}

function Box({
  stats,
  min,
  max,
  label,
  difference = false,
}: {
  stats: Stats;
  min: number;
  max: number;
  label: string;
  difference?: boolean;
}) {
  if (!stats.n) return <span className="muted">无有效样本</span>;
  // Coordinates only: all finance and quantiles come from the backend contract.
  const x = (value: string | null | undefined) =>
    12 + ((Number(value) - min) / (max - min || 1)) * 216;
  return (
    <svg
      className="distribution-box"
      viewBox="0 0 240 42"
      role="img"
      aria-label={`${label}，N=${stats.n}，最低${statisticValue(stats.min, difference)}，下四分位${statisticValue(stats.q25, difference)}，中位${statisticValue(stats.median, difference)}，均值${statisticValue(stats.mean, difference)}，上四分位${statisticValue(stats.q75, difference)}，最高${statisticValue(stats.max, difference)}`}
    >
      <line x1="12" x2="228" y1="35" y2="35" stroke="#e2e8f0" />
      {stats.n === 1 ? (
        <circle cx={x(stats.median)} cy="19" r="4" fill="#2563eb" />
      ) : (
        <>
          <line
            x1={x(stats.min)}
            x2={x(stats.max)}
            y1="19"
            y2="19"
            stroke="#475569"
          />
          {[stats.min, stats.max].map((v, i) => (
            <line
              key={i}
              x1={x(v)}
              x2={x(v)}
              y1="10"
              y2="28"
              stroke="#475569"
            />
          ))}
          <rect
            x={x(stats.q25)}
            y="7"
            width={Math.max(1, x(stats.q75) - x(stats.q25))}
            height="24"
            fill="#dbeafe"
            stroke="#2563eb"
          />
          <line
            x1={x(stats.median)}
            x2={x(stats.median)}
            y1="7"
            y2="31"
            stroke="#172033"
            strokeWidth="2"
          />
        </>
      )}
      <circle cx={x(stats.mean)} cy="19" r="3" fill="#d97706" stroke="white" />
    </svg>
  );
}

export function StatisticsPanel({
  data,
  defaultMetric = "endpoint",
  readingId,
  title = "历史窗口涨跌幅分布",
  group,
  onSelect,
  busy = false,
}: {
  data: Fact;
  defaultMetric?: string;
  readingId?: string;
  title?: string;
  group?: string;
  onSelect?: (selection: { metric?: string; group?: string }) => void;
  busy?: boolean;
}) {
  const distributions = (data.distributions ?? []) as Distribution[];
  const monthly = data.kind === "monthly";
  const currentMonth = Number(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      month: "numeric",
    }).format(new Date()),
  );
  const monthRanks = (data.monthly_rankings ??
    []) as components["schemas"]["MonthlyRanking"][];
  const monthRank = (group: string) =>
    monthRanks.find((r) => r.month === Number(group));
  const readingKey = readingId
    ? `statistics:${readingId}:${title}`
    : `statistics:${title}:${JSON.stringify(data.metadata)}`;
  const [ranking, setRanking] = useReadingState(
    readingKey + ":ranking",
    "median",
  );
  const [monthOrder, setMonthOrder] = useReadingState(
    readingKey + ":month-order",
    "calendar",
  );
  const [reverse, setReverse] = useReadingState(readingKey + ":reverse", false);
  const coverage = facts(data.fiscal_coverage);
  const calendar = useFiscalCalendar(data, readingKey);
  const metrics = [
    ...new Set([
      ...distributions.map((d) => d.metric),
      ...rows(coverage.rankings).map((r) => String(r.window)),
    ]),
  ];
  const metric = metrics.includes(defaultMetric) ? defaultMetric : metrics[0];
  const groups = [
    ...new Set([
      ...distributions.map((d) => d.group),
      ...rows(coverage.rankings).map((r) => String(r.quarter)),
    ]),
  ];
  const defaultGroup =
    data.kind === "monthly"
      ? String(facts(facts(data.metadata).params).month ?? groups[0])
      : data.kind === "earnings"
        ? `Q${facts(facts(data.metadata).params).quarter ?? 1}`
        : (groups[0] ?? "");
  const selectedGroup = group || defaultGroup;
  const selected = distributions
    .filter(
      (d) => d.metric === metric && (monthly || d.group === selectedGroup),
    )
    .sort((a, b) => {
      if (!monthly) return 0;
      if (monthOrder === "calendar") return Number(a.group) - Number(b.group);
      const key = `${monthOrder}${reverse ? "_reverse" : ""}_rank` as
        | "mean_rank"
        | "median_rank"
        | "mean_reverse_rank"
        | "median_reverse_rank";
      return (
        (monthRank(a.group)?.[key] ?? 999) -
          (monthRank(b.group)?.[key] ?? 999) ||
        Number(a.group) - Number(b.group)
      );
    });
  const benchmark = facts(data.benchmark);
  const items = selected.flatMap((d) => [
    {
      key: d.key,
      label:
        monthly && monthRanks.length
          ? `${d.group}月`
          : `${sourceLabel(d.label)} · 股票全部合格样本`,
      stats: d.stock,
      parent: d,
    },
    ...(d.benchmark
      ? [
          {
            key: `${d.key}-paired`,
            label: `${sourceLabel(d.label)} · 股票（共同样本）`,
            stats: d.paired_stock!,
            parent: d,
          },
          {
            key: `${d.key}-benchmark`,
            label: `${sourceLabel(d.label)} · ${benchmark.name ?? "基准"}（共同样本）`,
            stats: d.benchmark,
            parent: d,
          },
          {
            key: `${d.key}-difference`,
            label: `${sourceLabel(d.label)} · 领先/落后基准（百分点）`,
            stats: d.difference!,
            parent: d,
          },
        ]
      : []),
  ]);
  const values = items.flatMap((d) =>
    d.stats.n ? [Number(d.stats.min), Number(d.stats.max)] : [],
  );
  const min = Math.min(0, ...values),
    max = Math.max(0, ...values);
  const rankKey = `${ranking}${reverse ? "_reverse" : ""}_rank`;
  const ranked = rows(coverage.rankings)
    .filter((r) => r.window === metric)
    .sort((a, b) => Number(a[rankKey] ?? 999) - Number(b[rankKey] ?? 999));
  if (!distributions.length && !rows(coverage.rankings).length)
    return (
      <p className="reason">
        此保留版本尚无箱线统计。重新应用研究条件会使用本地行情生成新版，原结果仍保留。
      </p>
    );
  return (
    <section className="statistics-panel">
      <div className="section-heading">
        <h3>{title}</h3>
        <div className="inline-controls">
          {!monthly && groups.length > 1 && (
            <label>
              分布分组
              <select
                aria-label="分布分组"
                value={selectedGroup}
                disabled={!onSelect || busy}
                onChange={(e) => onSelect?.({ group: e.target.value })}
              >
                {groups.map((g) => (
                  <option key={g} value={g}>
                    {data.kind === "monthly" ? `${g}月` : sourceLabel(g)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {!monthly && (
            <label>
              统计窗口
              <select
                aria-label="统计窗口"
                value={metric}
                disabled={!onSelect || busy}
                onChange={(e) => onSelect?.({ metric: e.target.value })}
              >
                {metrics.map((m) => (
                  <option key={m} value={m}>
                    {metricLabel[m] ?? m}
                  </option>
                ))}
              </select>
            </label>
          )}
          {monthly && (
            <>
              <label>
                月份排序
                <select
                  aria-label="月份排序"
                  value={monthOrder}
                  onChange={(e) => setMonthOrder(e.target.value)}
                >
                  <option value="calendar">1—12 月顺序</option>
                  <option value="mean" disabled={!monthRanks.length}>
                    历史平均值
                  </option>
                  <option value="median" disabled={!monthRanks.length}>
                    历史中位数
                  </option>
                </select>
              </label>
              {monthOrder !== "calendar" && (
                <label>
                  <input
                    type="checkbox"
                    checked={reverse}
                    onChange={(e) => setReverse(e.target.checked)}
                  />{" "}
                  最低到最高
                </label>
              )}
            </>
          )}
        </div>
      </div>
      {monthly && monthRanks.length > 0 && (
        <p className="context-baseline">
          高亮 {currentMonth}{" "}
          月为当前月份（美东）。全年箱线图与排名使用股票每个月各自的完整历史样本，数值越高名次越靠前；并列同名次，后续名次连续。缺失月份不排名，各月有效
          N 可能不同；当前年度另见热力图与逐样本明细。
        </p>
      )}
      {monthly && !monthRanks.length && (
        <p className="reason">
          此保留版本尚无全年排名，部分月份可能使用原来的相同进度口径。点击“查看分析”复用本地行情生成新版；原结果保留。
        </p>
      )}
      <p className="context-baseline">
        每个历史样本在所选窗口的收盘涨跌幅。须线为真实最小/最大，箱体为上下四分位，黑线为中位，橙点为平均；N=1仅显示单点。当前年度独立观察。仅拆股调整，不含分红再投资。
      </p>
      {benchmark.symbol && (
        <p className="context-baseline">
          基准：{display(benchmark.name)} ·{" "}
          {benchmark.status === "available"
            ? "按股票相同实际日期逐样本配对，再计算股票、基准和差值的共同有效N。"
            : gapLabel(benchmark.status)}{" "}
          差值正数为领先、负数为落后（百分点），缺失不填0。
        </p>
      )}
      {ranked.length > 0 && (
        <>
          <div className="section-heading">
            <h3>Q1—Q4 财报附近股价表现排名</h3>
            <div className="inline-controls">
              <label>
                排序依据
                <select
                  value={ranking}
                  onChange={(e) => setRanking(e.target.value)}
                >
                  <option value="median">历史中位数</option>
                  <option value="mean">历史平均数</option>
                </select>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={reverse}
                  onChange={(e) => setReverse(e.target.checked)}
                />{" "}
                最差到最好
              </label>
            </div>
          </div>
          <p>
            目标{" "}
            {rows(coverage.gaps).length
              ? display(coverage.target_years)
              : "财年待确定"}
            ；当前财年 {display(coverage.current_fiscal_year)} 另列。
            {coverage.target_reason}
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>排名 / 季度</th>
                  <th>财季覆盖月份</th>
                  <th>财报公布月份</th>
                  <th>有效年份 / 目标</th>
                  <th>平均</th>
                  <th>中位</th>
                  <th>最高 / 最低</th>
                  <th>上涨次数 / N</th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((item) => {
                  const s = facts(item.statistics);
                  return (
                    <tr key={item.quarter}>
                      <td>
                        {display(item[rankKey])} · {item.quarter}
                      </td>
                      {calendar.cells(String(item.quarter))}
                      <td>
                        {(item.valid_years ?? []).length} /{" "}
                        {(item.target_years ?? []).length} 年
                      </td>
                      <td>{percent(s.mean)}</td>
                      <td>{percent(s.median)}</td>
                      <td>
                        {percent(s.max)} / {percent(s.min)}
                      </td>
                      <td>
                        {s.up} / {s.n}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {calendar.details}
        </>
      )}
      <p className="table-hint">分布表可横向滚动；聚焦表格后可用方向键阅读。</p>
      <div className="table-scroll" role="region" aria-label="统计分布表" tabIndex={0}>
        <table className="statistics-table">
          <caption>
            {values.length
              ? `共同标尺 ${percent(min)} 至 ${percent(max)}`
              : "当前没有有效样本，不绘制分布标尺"}
            ；数值与图来自同一冻结结果
          </caption>
          <thead>
            <tr>
              <th>样本 / 窗口</th>
              {monthly && (
                <>
                  <th>平均排名</th>
                  <th>中位排名</th>
                </>
              )}
              <th>箱线图</th>
              <th>有效 N</th>
              <th>最低</th>
              <th>下四分位</th>
              <th>中位</th>
              <th>平均</th>
              <th>上四分位</th>
              <th>最高</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr
                key={item.key}
                className={
                  monthly && Number(item.parent.group) === currentMonth
                    ? "current-month-row"
                    : undefined
                }
              >
                <th scope="row">
                  {item.label}
                  {monthly && Number(item.parent.group) === currentMonth && (
                    <span className="current-month-badge">当前月份</span>
                  )}
                </th>
                {monthly && (
                  <>
                    <td>
                      {item.key === item.parent.key
                        ? display(monthRank(item.parent.group)?.mean_rank)
                        : "—"}
                    </td>
                    <td>
                      {item.key === item.parent.key
                        ? display(monthRank(item.parent.group)?.median_rank)
                        : "—"}
                    </td>
                  </>
                )}
                <td>
                  <Box
                    stats={item.stats}
                    min={min}
                    max={max}
                    label={item.label}
                    difference={item.key.endsWith("-difference")}
                  />
                </td>
                <td>{item.stats.n}</td>
                {(["min", "q25", "median", "mean", "q75", "max"] as const).map(
                  (k) => (
                    <td key={k}>
                      {statisticValue(
                        item.stats[k],
                        item.key.endsWith("-difference"),
                      )}
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected
        .filter((d) => !d.stock.n)
        .map((d) => (
          <p className="reason" key={d.key}>
            {sourceLabel(d.label)}：{d.empty_reason}
          </p>
        ))}
      {!selected.length && (
        <p className="reason">
          {sourceLabel(selectedGroup)} · 有效
          N=0：当前范围没有已核对且价格窗口完整的样本；请查看季度覆盖与资料核对入口。
        </p>
      )}
      <details className="result-notes">
        <summary>覆盖缺口、逐样本数值与真实配对日期</summary>
        {selected.map((d) => (
          <div key={d.key}>
            <h4>
              {sourceLabel(d.label)} · {metricLabel[d.metric]}
            </h4>
            {Object.entries(d.missing ?? {}).map(([reason, n]) => (
              <p key={reason}>
                {gapLabel(reason)}：{n} 次
              </p>
            ))}
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>年份 / 样本</th>
                    <th>实际起止日期</th>
                    <th>股票</th>
                    <th>基准</th>
                    <th>领先/落后百分点</th>
                    <th>资格 / 缺口</th>
                  </tr>
                </thead>
                <tbody>
                  {d.samples?.map((s, i) => (
                    <tr key={i}>
                      <td>
                        {display(s.year)} · {display(s.key)}
                      </td>
                      <td>
                        {display(s.start_date)} → {display(s.end_date)}
                      </td>
                      <td>{percent(s.stock)}</td>
                      <td>{percent(s.benchmark)}</td>
                      <td>{statisticValue(s.difference, true)}</td>
                      <td>
                        {gapLabel(s.status)}
                        {s.benchmark_status
                          ? ` · ${gapLabel(s.benchmark_status)}`
                          : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
        {rows(coverage.gaps)
          .filter((g) => g.window === metric && g.status !== "available")
          .map((g, i) => (
            <p key={i} className="fiscal-coverage-gap">
              <span>
                FY{g.year} {g.quarter}：{gapLabel(g.status)}
              </span>
              {Array.isArray(g.reasons) &&
                g.reasons
                  .filter((r) => r !== g.status)
                  .map((r) => <small key={String(r)}>{gapLabel(r)}</small>)}
              {g.event_keys?.length > 0 && (
                <FiscalReviewLink data={data} eventKey={g.event_keys[0]} />
              )}
            </p>
          ))}
      </details>
    </section>
  );
}
