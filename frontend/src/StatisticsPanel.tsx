import type { components } from "./generated/api";
import { display, facts, percent, type Fact } from "./api";
import { useReadingState } from "./researchStorage";
import { gapLabel } from "./components/StatisticsPanel";
import "./statistics-evidence.css";

type Distribution = components["schemas"]["Distribution"];
type Robustness = components["schemas"]["DistributionRobustness"];
type Methodology = components["schemas"]["ResearchMethodology"];
const ratio = (value: string | null | undefined) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(Number(value));
const difference = (value: unknown) =>
  percent(value).replace(/%$/, " 个百分点");
const metricNames: Record<string, string> = {
  endpoint: "期末价格收益",
  before5: "前5日 · D−6至D−1",
  day0: "当日 · D−1至D0",
  after5: "后5日 · D0至D+5",
  through5: "含当天 · D−1至D+5",
  "1": "精确反应1日",
  "5": "精确反应5日",
  "20": "精确反应20日",
  "60": "精确反应60日",
};

function Sensitivity({
  value,
  paired = false,
}: {
  value: Robustness;
  paired?: boolean;
}) {
  const format = paired ? difference : percent;
  const loo = value.leave_one_out;
  const segments = value.historical_segments;
  return (
    <div className="research-sensitivity">
      <p>
        <strong>逐一去掉一个样本：</strong>
        {loo.available ? (
          <>
            均值范围 {format(loo.mean_min)} 至 {format(loo.mean_max)}
            ；中位数范围 {format(loo.median_min)} 至 {format(loo.median_max)}
            。对均值影响最大：{(loo.influential_sample_keys ?? []).join("、")}
            ，变化幅度 {difference(loo.max_abs_mean_change)}。
          </>
        ) : (
          "样本不足，去掉一个后无法比较。"
        )}{" "}
        {loo.interpretation}
      </p>
      <div className="research-segments">
        {[segments.earlier, segments.later].map((segment) => (
          <div key={segment.label}>
            <h5>
              {segment.label} ·{" "}
              {segment.target_years.length
                ? `${segment.target_years[0]}—${segment.target_years[segment.target_years.length - 1]}`
                : "无目标年份"}
            </h5>
            <p>
              有效 N={segment.statistics.n} · 均值{" "}
              {format(segment.statistics.mean)} · 中位{" "}
              {format(segment.statistics.median)}
            </p>
          </div>
        ))}
      </div>
      <p className="research-evidence-note">{segments.interpretation}</p>
      {loo.available && (
        <details>
          <summary>
            查看每个样本的影响（{(loo.points ?? []).length} 项）
          </summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>去掉的样本</th>
                  <th>原收益</th>
                  <th>剩余 N</th>
                  <th>新均值</th>
                  <th>新中位</th>
                  <th>均值变化</th>
                </tr>
              </thead>
              <tbody>
                {(loo.points ?? []).map((point) => (
                  <tr key={point.omitted_key}>
                    <td>
                      {point.omitted_year ?? "年份未知"} · {point.omitted_key}
                    </td>
                    <td>{format(point.omitted_return)}</td>
                    <td>{point.n}</td>
                    <td>{format(point.mean)}</td>
                    <td>{format(point.median)}</td>
                    <td>{difference(point.mean_change)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

/** Version-owned evidence only. Number conversions below format values or locate SVG points. */
export function StatisticsPanel({
  data,
  readingId,
  defaultMetric,
  group,
}: {
  data: Fact;
  readingId?: string;
  defaultMetric?: string;
  group?: string;
}) {
  const meta = facts(data.metadata);
  const params = facts(meta.params);
  const distributions = (data.distributions ?? []) as Distribution[];
  const metric =
    defaultMetric ??
    (data.kind === "earnings"
      ? String(params.window ?? 5)
      : data.kind === "event_dates"
        ? String(meta.date_window ?? params.date_window ?? "after5")
        : "endpoint");
  const selectedGroup =
    group ??
    (data.kind === "monthly"
      ? String(params.month ?? "")
      : data.kind === "earnings"
        ? `Q${params.quarter ?? 1}`
        : "");
  const selected =
    distributions.find(
      (item) => item.metric === metric && item.group === selectedGroup,
    ) ??
    distributions.find((item) => item.metric === metric) ??
    distributions[0];
  const readingKey = `research-evidence:${readingId ?? `${data.kind}:${JSON.stringify(params)}`}:${selected?.key ?? "empty"}`;
  const [page, setPage] = useReadingState(readingKey + ":page", 0);
  const [view, setView] = useReadingState(readingKey + ":view", "stock");
  const [detailsOpen, setDetailsOpen] = useReadingState(
    readingKey + ":details",
    false,
  );
  const [methodsOpen, setMethodsOpen] = useReadingState(
    readingKey + ":methods",
    false,
  );
  const robust = selected?.robustness;
  if (!selected || !robust)
    return (
      <section
        className="research-evidence research-evidence-legacy"
        aria-label="研究证据摘要"
      >
        <p>
          此固定版本未记录稳健性检查，原结果仍可复现。选择“以最新数据更新”可生成包含实际样本、敏感性与方法说明的新版本。
        </p>
      </section>
    );
  const methods = meta.methodology as Methodology | undefined;
  const benchmark = facts(data.benchmark);
  const paired =
    view === "difference" && !!selected.paired_difference_robustness;
  const shownRobust = paired ? selected.paired_difference_robustness! : robust;
  const stats = paired ? selected.difference! : selected.stock;
  const sampleKeys = new Set(shownRobust.sample_keys);
  const samples = (selected.samples ?? []).filter((sample) =>
    sampleKeys.has(String(sample.key)),
  );
  const pageCount = Math.max(1, Math.ceil(samples.length / 12));
  const currentPage = Math.max(0, Math.min(Number(page), pageCount - 1));
  const visible = samples.slice(currentPage * 12, (currentPage + 1) * 12);
  const min = Math.min(0, Number(stats.min ?? 0)),
    max = Math.max(0, Number(stats.max ?? 0));
  const position = (value: unknown) =>
    8 + ((Number(value) - min) / (max - min || 1)) * 384;
  const format = paired ? difference : percent;
  const unit = robust.sample_unit === "annual_sample" ? "年度样本" : "事件样本";
  const proportion = robust.proportion;
  return (
    <section className="research-evidence" aria-label="研究证据与稳健性">
      <div className="research-evidence-heading">
        <div>
          <h3>先看证据强弱</h3>
          <p>
            {display(selected.label)} ·{" "}
            {metricNames[selected.metric] ?? selected.metric} · {unit} N=
            {robust.n}
          </p>
        </div>
        <span className="research-evidence-badge">历史描述 · 探索性研究</span>
      </div>
      <p className="research-evidence-baseline">{methods?.baseline_rule}</p>
      <dl className="research-evidence-metrics">
        <div>
          <dt>有效历史{unit}</dt>
          <dd>
            {robust.n}
            <small> 个</small>
          </dd>
        </div>
        <div>
          <dt>收益中位数 / 均值</dt>
          <dd>
            {percent(selected.stock.median)}
            <small> / {percent(selected.stock.mean)}</small>
          </dd>
        </div>
        <div>
          <dt>历史上涨次数</dt>
          <dd>
            {proportion.up}
            <small>
              {" "}
              / {proportion.n}，平盘 {proportion.flat}
            </small>
          </dd>
        </div>
        <div>
          <dt>最深日收盘回撤</dt>
          <dd>
            {ratio(selected.closing_max_drawdown?.max)}
            <small> · 风险样本 N={selected.closing_max_drawdown?.n ?? 0}</small>
          </dd>
        </div>
      </dl>
      <p className="research-evidence-uncertainty">
        <strong>
          {proportion.lower == null || proportion.upper == null
            ? "上涨比例 Wilson 95% 区间：暂无有效样本，无法估计。"
            : `上涨比例 Wilson 95% 区间：${ratio(proportion.lower)}—${ratio(proportion.upper)}。`}
        </strong>
        只在样本独立、上涨概率恒定且事先确定的假设下解释；不是未来上涨概率或收益预测。
        {robust.n < 2
          ? "当前样本不足以评估稳定性。"
          : "有限样本需逐个查看；N本身不能证明规律可靠。"}
      </p>
      {selected.benchmark && (
        <div className="research-evidence-pair">
          <strong>
            {display(benchmark.symbol || "所选基准")} · 同日期配对 N=
            {selected.difference?.n ?? 0}
          </strong>
          <span>配对股票中位 {percent(selected.paired_stock?.median)}</span>
          <span>基准中位 {percent(selected.benchmark.median)}</span>
          <span>
            逐样本收益差中位 {difference(selected.difference?.median)}
          </span>
          <small>
            股票自身 N={selected.stock.n}；收益差不是风险调整 alpha，缺失不补0。
          </small>
        </div>
      )}
      {Object.entries(selected.missing ?? {}).length > 0 && (
        <p className="research-evidence-gaps">
          已知缺口：
          {Object.entries(selected.missing ?? {})
            .map(([reason, count]) => `${gapLabel(reason)} ${count} 项`)
            .join("；")}
        </p>
      )}
      <div className="research-evidence-heading">
        <h4>每个实际{unit}都是一个点</h4>
        {selected.paired_difference_robustness && (
          <label>
            点图内容{" "}
            <select
              value={paired ? "difference" : "stock"}
              onChange={(event) => setView(event.target.value)}
            >
              <option value="stock">股票收益</option>
              <option value="difference">相对基准收益差</option>
            </select>
          </label>
        )}
      </div>
      <p className="research-evidence-note">
        {paired ? "仅展示同日期配对样本。" : "仅展示有效历史样本，当前年另列。"}
        图中正负号表示涨跌；每点可用键盘聚焦查看起止日。完整分布箱线图保留在下方。
      </p>
      {samples.length > 0 ? (
        <div
          className="research-sample-plot"
          role="group"
          aria-label={`${unit}实际收益点，N=${shownRobust.n}`}
        >
          <div className="research-sample-axis">
            <span>样本</span>
            <span>
              {format(stats.min)} <i>← 收益标尺 →</i> {format(stats.max)}
            </span>
            <span>实际收益</span>
          </div>
          {visible.map((sample) => {
            const value = paired ? sample.difference : sample.stock;
            const description = `${sample.year ?? "年份未知"}，${display(sample.label || sample.key)}，${display(sample.start_date)}至${display(sample.end_date)}，${format(value)}`;
            return (
              <div className="research-sample-line" key={String(sample.key)}>
                <span title={description}>
                  {display(sample.year)}
                  {unit === "事件样本" && (
                    <small>{display(sample.label || sample.key)}</small>
                  )}
                </span>
                <svg
                  viewBox="0 0 400 28"
                  preserveAspectRatio="none"
                  aria-label={description}
                >
                  <line x1="8" x2="392" y1="14" y2="14" stroke="#dce4ee" />
                  <line
                    x1={position(0)}
                    x2={position(0)}
                    y1="3"
                    y2="25"
                    stroke="#a3afbf"
                    strokeDasharray="3 3"
                  />
                  <circle
                    cx={position(value)}
                    cy="14"
                    r="5"
                    fill="#2563eb"
                    stroke="#fff"
                    strokeWidth="1.5"
                    tabIndex={0}
                    role="img"
                    aria-label={description}
                  >
                    <title>{description}</title>
                  </circle>
                </svg>
                <strong>{format(value)}</strong>
              </div>
            );
          })}
          {pageCount > 1 && (
            <div className="research-evidence-pagination">
              <button
                type="button"
                disabled={currentPage === 0}
                onClick={() => setPage(currentPage - 1)}
              >
                上一页样本
              </button>
              <span>
                第 {currentPage + 1}/{pageCount} 页 · 全部 {samples.length}{" "}
                个样本
              </span>
              <button
                type="button"
                disabled={currentPage + 1 >= pageCount}
                onClick={() => setPage(currentPage + 1)}
              >
                下一页样本
              </button>
            </div>
          )}
        </div>
      ) : (
        <p className="research-evidence-gaps">
          没有满足当前窗口资格的历史样本，无法绘制实际点。
        </p>
      )}
      <p className="research-evidence-risk">{methods?.drawdown_rule}</p>
      <details
        className="research-evidence-details"
        open={detailsOpen}
        onToggle={(event) => setDetailsOpen(event.currentTarget.open)}
      >
        <summary>稳健性：单样本影响、早晚历史比较与重叠窗口</summary>
        <Sensitivity value={robust} />
        {selected.paired_difference_robustness && (
          <details>
            <summary>
              相对基准收益差的敏感性（配对 N=
              {selected.paired_difference_robustness.n}）
            </summary>
            <Sensitivity value={selected.paired_difference_robustness} paired />
          </details>
        )}
        {(robust.overlap_pair_count ?? robust.overlapping_pairs?.length ?? 0) >
          0 && (
          <div className="research-evidence-gaps">
            <p>
              重叠价格窗口共{" "}
              {robust.overlap_pair_count ?? robust.overlapping_pairs?.length}{" "}
              对；保留所有样本并提示相关性，不能当作独立重复试验。
              {robust.overlap_details_truncated
                ? "以下列出前200对；完整事件日期见逐样本明细。"
                : ""}
            </p>
            <ul>
              {robust.overlapping_pairs?.map((pair, index) => (
                <li key={index}>
                  {pair.first_key} / {pair.second_key}：{pair.start_date}—
                  {pair.end_date}
                </li>
              ))}
            </ul>
          </div>
        )}
        {(robust.warnings ?? []).map((warning) => (
          <p key={warning} className="research-evidence-note">
            {warning}
          </p>
        ))}
      </details>
      <details
        className="research-evidence-details"
        open={methodsOpen}
        onToggle={(event) => setMethodsOpen(event.currentTarget.open)}
      >
        <summary>方法、样本单位与解释限制</summary>
        <p>{methods?.price_basis}</p>
        <p>{methods?.path_n_rule}</p>
        <p>{methods?.benchmark_rule}</p>
        <p>{methods?.exploration_scope}</p>
        <p>{methods?.interpretation}</p>
        <p>{proportion.assumptions}</p>
        <p>{proportion.interpretation}</p>
        <p>该研究只描述事件附近的价格变化，不能把窗口内全部涨跌归因于事件。</p>
        {meta.inclusion_rule != null && (
          <p>保存的事件纳入范围：{display(meta.inclusion_rule)}</p>
        )}
        {meta.keyword_scope_policy && <p>{display(meta.keyword_scope_policy)}</p>}
      </details>
    </section>
  );
}
