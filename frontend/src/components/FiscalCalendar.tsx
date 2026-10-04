import type { components } from "../generated/api";
import { display, type Fact } from "../api";
import { useReadingState } from "../researchStorage";
import { Button } from "./ui";
import { Link, useLocation } from "react-router-dom";
import { facts } from "../api";
import { sourceContext } from "../researchStorage";

type Calendar = components["schemas"]["FiscalQuarterCalendar"];
const fieldStatus: Record<string, string> = {
  confirmed: "已核对",
  missing: "来源缺失",
  unverified: "待核对",
  unsupported: "来源未对应此字段",
  conflicting: "来源冲突",
};
const reasonLabel: Record<string, string> = {
  missing_event: "尚缺该财季发布资料",
  user_excluded: "已排除",
  unassigned_year: "财年归属待核对",
  not_historical_year: "当前/非历史财年，另列观察",
  outside_requested_years: "不在所选历史年份内",
  unverified_or_nonstandard_fiscal_period: "财季归属待核对或不是常规季度",
  duplicate_fiscal_period: "同一财季有多条主发布，需先核对关系",
  event_not_confirmed_occurred: "尚未确认实际发生",
  future_announcement: "晚于研究截止日",
  unverified_event_date: "已有资料，日期待核对；不计入有效样本",
  unsupported_event_date: "已有资料，日期来源尚未支持；不计入有效样本",
};

export function FiscalReviewLink({ data, eventKey }: { data: Fact; eventKey?: string }) {
  const location = useLocation();
  const metadata = facts(data.metadata);
  const setId = metadata.event_set_id;
  const query = new URLSearchParams(
    location.pathname === "/analysis/events" ? location.search : "",
  );
  if (setId) {
    query.set("set", String(setId));
    query.set("version", String(metadata.event_version));
    query.set("review_event", eventKey ?? "");
  }
  const symbol = metadata.symbol ?? facts(metadata.params).tickers?.[0];
  return (
    <Link className="text-link" state={sourceContext(location)}
      to={setId ? `/analysis/events?${query}` : `/data?events=${encodeURIComponent(symbol ?? "")}`}>
      查看资料并核对 →
    </Link>
  );
}

export function useFiscalCalendar(data: Fact, readingKey: string) {
  const [opened, setOpened] = useReadingState(readingKey + ":calendar", "");
  const calendars = (data.fiscal_coverage?.quarter_calendar ??
    []) as Calendar[];
  return {
    cells: (quarter: string) => (
      <FiscalCalendarCells
        calendar={calendars.find((item) => item.quarter === quarter)}
        open={opened === quarter}
        onToggle={() => setOpened(opened === quarter ? "" : quarter)}
      />
    ),
    details: opened ? (
      <FiscalCalendarDetails
        calendar={calendars.find((item) => item.quarter === opened)}
        data={data}
      />
    ) : null,
  };
}

function FiscalCalendarCells({
  calendar,
  open,
  onToggle,
}: {
  calendar?: Calendar;
  open: boolean;
  onToggle: () => void;
}) {
  if (!calendar)
    return (
      <>
        <td colSpan={2}>
          <small>此保留版本未包含月份摘要；重新应用条件可生成新版。</small>
        </td>
      </>
    );
  return (
    <>
      <td>
        {calendar.period_months ?? "覆盖月份待核对"}
        <small>已核对起止 {calendar.period_n} 年</small>
        {(calendar.period_ranges?.length ?? 0) > 1 && (
          <small>财期随年份变化，详见历年日期</small>
        )}
      </td>
      <td>
        {calendar.announcement_months ?? "公布月份待核对"}
        <small>
          {calendar.announcement_n
            ? `${calendar.announcement_n} 年已核对${calendar.announcement_n > 1 ? ` · 常见 ${calendar.common_announcement_months?.map((month) => `${month}月`).join("、")}` : " · 仅1年，不代表稳定规律"}`
            : "尚无符合常规财期资格的历史日期"}
        </small>
        <Button variant="ghost" aria-expanded={open} onClick={onToggle}>
          {open ? "收起历年日期" : "展开历年日期"} · {calendar.quarter}
        </Button>
      </td>
    </>
  );
}

function FiscalCalendarDetails({ calendar, data }: { calendar?: Calendar; data: Fact }) {
  if (!calendar) return null;
  return (
    <section
      className="fiscal-calendar-details"
      aria-label={`${calendar.quarter} 历年财期与公告日期`}
    >
      <h4>{calendar.quarter} · 历年财期与公告日期</h4>
      <p className="muted">
        月份取本次研究内已核对的历史来源，与收益有效 N
        分开；缺行情不抹掉已知日期。
        当前财年、未核对、重复或已排除资料另作说明，不加入常见月份。
        {calendar.announcement_counts?.length
          ? ` 公告频次：${calendar.announcement_counts.map((item) => `${item.month}月 ${item.n} 次`).join("；")}。`
          : ""}
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>财年 / 财季</th>
              <th>财季实际覆盖日期</th>
              <th>财报公布日期</th>
              <th>资料资格 / 来源</th>
            </tr>
          </thead>
          <tbody>
            {calendar.entries?.map((entry, index) => (
              <tr key={entry.key ?? `missing:${entry.fiscal_year}:${index}`}>
                <td>
                  FY{display(entry.fiscal_year)} {entry.quarter}
                  <small>
                    {entry.group === "historical"
                      ? "历史"
                      : entry.group === "current"
                        ? "当前财年，单独观察"
                        : "待核对观察"}
                  </small>
                </td>
                <td>
                  {display(entry.period_start)} → {display(entry.period_end)}
                  <small>
                    起点：{fieldStatus[entry.period_start_status ?? "missing"]}
                    ；期末：{fieldStatus[entry.period_end_status ?? "missing"]}
                  </small>
                </td>
                <td>
                  {display(entry.announcement_date)}
                  <small>
                    {fieldStatus[entry.announcement_status ?? "missing"]} ·
                    原公告日，不是顺延后的交易日
                  </small>
                </td>
                <td>
                  {entry.reasons?.map((reason) => (
                    <small key={reason}>
                      {reasonLabel[reason] ?? "资料资格待核对"}
                    </small>
                  ))}
                  {entry.review_note && <small>{entry.review_note}</small>}
                  {entry.key && (
                    <FiscalReviewLink data={data} eventKey={entry.key} />
                  )}
                  <details>
                    <summary>查看来源（{entry.sources?.length ?? 0}）</summary>
                    {entry.sources?.map((source, i) => (
                      <p key={i}>
                        {typeof source.url === "string" &&
                        /^https?:\/\//.test(source.url) ? (
                          <a
                            className="text-link"
                            href={source.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {display(source.title ?? "公告来源")}
                          </a>
                        ) : (
                          "来源地址待核对"
                        )}
                        {source.evidence_note ? (
                          <small>{display(source.evidence_note)}</small>
                        ) : null}
                      </p>
                    ))}
                    {!entry.sources?.length && (
                      <p>
                        暂无对应来源；请通过财报资料核对或事件来源管理补充。
                      </p>
                    )}
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
