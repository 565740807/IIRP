import { Link, useLocation } from "react-router-dom";
import { display, facts, formatNumber, rows, type Fact } from "../api";
import { sourceContext } from "../researchStorage";

// Transaction table and summaries of the company/person/transaction pages,
// which move to TanStack Table in S5b.
/** Reported transaction date later than its SEC acceptance date: kept, flagged, not sorted. */
export function DateAnomaly({ row }: { row: Fact }) {
  const anomaly = facts(row.date_anomaly);
  if (!anomaly.label) return null;
  return (
    <small
      className="date-anomaly"
      title={`申报的交易日 ${display(anomaly.transaction_date)} 晚于 SEC 接受日 ${display(anomaly.accepted_date)}；原值保留，不参与按实际交易日排序和日期范围。`}
    >
      {String(anomaly.label)}
    </small>
  );
}
export function TransactionTable({
  items,
  showCompany = false,
  hideOwner = false,
  focusOwnerId,
  hideDetailLink = false,
}: {
  items: Fact[];
  showCompany?: boolean;
  hideOwner?: boolean;
  focusOwnerId?: string;
  hideDetailLink?: boolean;
}) {
  const location = useLocation();
  const from = sourceContext(location);
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {showCompany && <th>公司 / ticker</th>}
            {!hideOwner && (
              <th>{focusOwnerId ? "申报时身份" : "主体 / 申报时身份"}</th>
            )}
            <th>行为 / 代码</th>
            <th>股数</th>
            <th>申报单价</th>
            <th>申报金额</th>
            <th>实际交易日</th>
            {!hideDetailLink && <th>查看</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((r, i) => (
            <tr key={r.id ?? i}>
              {showCompany && (
                <td>
                  <Link
                    state={from}
                    className="text-link"
                    to={"/companies/" + r.issuer_id}
                  >
                    {display(r.issuer_name ?? r.ticker ?? r.issuer_id)}
                  </Link>
                  {r.ticker && <small>{r.ticker}</small>}
                </td>
              )}
              {!hideOwner && (
                <td>
                  {focusOwnerId ? (
                    <span>
                      {rows(r.owners)
                        .filter((owner) => owner.id === focusOwnerId)
                        .map((owner) =>
                          [
                            (owner.roles ?? []).join("、"),
                            owner.entity_type === "institution" ? "机构" : "",
                          ]
                            .filter(Boolean)
                            .join(" · "),
                        )
                        .filter(Boolean)
                        .join("、") || "申报未注明职务"}
                    </span>
                  ) : rows(r.owners).length ? (
                    rows(r.owners).map((o) => (
                      <div key={o.id}>
                        <Link
                          state={from}
                          className="text-link owner-link"
                          to={`/people/${o.id}`}
                        >
                          {display(o.name)}
                        </Link>
                        <small>
                          {(o.roles ?? []).join("、") || "申报未注明职务"}
                          {o.entity_type === "institution" ? " · 机构" : ""}
                        </small>
                      </div>
                    ))
                  ) : r.owner_id ? (
                    <Link
                      state={from}
                      className="text-link"
                      to={`/people/${r.owner_id}`}
                    >
                      {display(r.owner ?? r.owner_name)}
                    </Link>
                  ) : rows(r.owners).length ? (
                    rows(r.owners).map((o) => (
                      <Link
                        key={o.id}
                        className="text-link owner-link"
                        to={`/people/${o.id}`}
                      >
                        {display(o.name)}
                      </Link>
                    ))
                  ) : (
                    display(r.owner ?? r.owner_name)
                  )}
                </td>
              )}
              <td
                className={
                  r.code === "P"
                    ? "trade-buy"
                    : r.code === "S"
                      ? "trade-sell"
                      : "trade-derivative"
                }
              >
                {display(r.action ?? r.kind)} · {display(r.code)}
              </td>
              <td>{r.shares == null ? "—" : formatNumber(r.shares)}</td>
              <td>
                {r.price == null
                  ? "源申报单价未知"
                  : `${r.currency ? `${r.currency} ` : ""}${formatNumber(r.price)}`}
                {r.price != null && !r.currency && (
                  <small className="muted">（币种未标注）</small>
                )}
              </td>
              <td>
                {r.amount == null
                  ? r.summary_exclusion_reason
                    ? "未计入确认汇总"
                    : "源申报金额未知"
                  : `${r.currency ? `${r.currency} ` : ""}${formatNumber(r.amount)}`}
                {r.amount != null && !r.currency && (
                  <small className="muted">（币种未标注）</small>
                )}
              </td>
              <td>
                {display(r.transaction_date)}
                <DateAnomaly row={r} />
                {(r.form?.includes("/A") || r.is_amendment_update) && (
                  <small>历史交易修订</small>
                )}
                {r.summary_exclusion_reason && (
                  <small>{r.summary_exclusion_reason}</small>
                )}
              </td>
              {!hideDetailLink && (
                <td>
                  <Link
                    state={from}
                    className="text-link"
                    to={`/transactions/${r.id}`}
                  >
                    交易与价格 →
                  </Link>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function ownerCount(summary: Fact) {
  return (
    [
      summary.person_count ? `${summary.person_count} 人` : "",
      summary.institution_count ? `${summary.institution_count} 家机构` : "",
      summary.unknown_owner_count
        ? `${summary.unknown_owner_count} 个类型未注明主体`
        : "",
    ]
      .filter(Boolean)
      .join("、") || `${summary.owner_count ?? 0} 个主体`
  );
}
export function TradeSummary({
  summary,
  primary = false,
}: {
  summary: Fact[];
  primary?: boolean;
}) {
  const trades = primary
    ? summary.filter((s) => s.table === "I" && ["P", "S"].includes(s.code))
    : summary;
  return (
    <div className="trade-summary">
      {trades.map((s, i) => (
        <p key={i}>
          <strong>
            {s.table === "I" && s.code === "P"
              ? "买入"
              : s.table === "I" && s.code === "S"
                ? "卖出"
                : display(s.kind)}
          </strong>{" "}
          · {ownerCount(s)} ·{" "}
          {s.shares == null
            ? s.reported_value_review_rows
              ? "源申报有数值，待确认后汇总"
              : "源申报股数未知"
            : `${formatNumber(s.shares)} 股`}{" "}
          ·{" "}
          {s.known_amount == null
            ? s.reported_value_review_rows
              ? "金额未计入"
              : "金额未知"
            : `${s.currency || "币种未注明"} ${formatNumber(s.known_amount)}`}
          {(!primary || trades.length > 1 || !!s.review_rows || !!s.missing_price_rows) && (
            <small>
              {[
                !primary || trades.length > 1 ? display(s.security_title) : "",
                s.review_rows ? `${s.review_rows} 条待核对` : "",
                s.missing_price_rows
                  ? `${s.missing_price_rows} 条缺价，金额为已知部分`
                  : "",
              ].filter(Boolean).join(" · ")}
            </small>
          )}
        </p>
      ))}
      {primary && summary.length > trades.length && (
        <small>
          {trades.length ? "另有" : "本范围无主动买卖；"}
          授予、行权等其他行为，展开查看。
        </small>
      )}
    </div>
  );
}
