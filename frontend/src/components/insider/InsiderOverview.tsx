import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LoaderCircle } from "lucide-react";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import {
  directionClass,
  formatDay,
  formatDayRange,
  formatEt,
  formatMoney,
  formatSignedPercent,
} from "@/lib/format";
import { readableCase } from "@/lib/insider";
import {
  INDICES,
  useOverviewChoice,
  useStockQuotes,
  type CompanyOverview,
  type OverviewTrade,
  type IndexFilter as Index,
} from "@/lib/insiderOverview";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { sourceContext } from "@/lib/navigation";
import { useInsiderN, useTradeWindows } from "@/lib/windows";
import { cn } from "@/lib/utils";
import { IndexFilter, IndexStatus } from "./IndexFilter";
import { CurrentPrice, CurrentQuoteStatus, MismatchRatio, TradePrice } from "./InsiderPrice";

type Overview = Schemas["InsiderOverview"];
type Role = "all" | "executive" | "director" | "ten_percent";
type CompanySort = Overview["company_sort"];

const ROLES = ["all", "executive", "director", "ten_percent"] as const;
const COMPANY_SORTS = ["net_buy", "net_sell", "buy", "sell", "people"] as const;
// While quotes asked for are still missing, re-read soon so rankings settle.
const QUOTE_FOLLOW_UP_MS = 3000;
const QUOTE_FOLLOW_UP_WINDOW_MS = 60_000;

const selectStyle = "h-8 rounded-md border bg-card px-2 text-xs outline-none focus:border-ring";
const headerStyle = "whitespace-nowrap px-3 py-2 text-right font-medium text-muted-foreground";
const cellStyle = "whitespace-nowrap px-3 py-2 text-right tabular-nums";
const rowStyle = "border-t hover:bg-accent/40";

type CompanyRef = { issuer_id: string; name: string; ticker: string | null };

function CompanyLink({ row }: { row: CompanyRef }) {
  const location = useLocation();
  const name = readableCase(row.name);
  return <Link
    to={`/companies/${row.issuer_id}`}
    state={sourceContext(location)}
    className="flex min-w-0 items-baseline gap-2 hover:underline"
    title={name}
  >
    <span className="shrink-0 rounded bg-secondary px-1 text-[11px] font-semibold">{row.ticker ?? "—"}</span>
    <span className="truncate font-medium">{name}</span>
  </Link>;
}

type AmountProps = { amount?: string | null; estimated?: boolean; direction?: "buy" | "sell" };

function Amount({ amount, estimated, direction }: AmountProps) {
  const { t } = useTranslation();
  // Direction supplies a display sign; totals and prices are computed by the backend.
  const sign = direction === "buy" ? "+" : "-";
  const display = amount != null && direction ? `${sign}${amount.replace(/^[+-]/, "")}` : amount;
  const tone = direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : directionClass(amount);
  return <span className={tone}>
    {estimated && <span className="mr-1 text-[10px] text-muted-foreground">{t("ui.overview.estimated")}</span>}
    {formatMoney(display, { signed: true })}
  </span>;
}

/** Markers after an amount: trades without a price, and trades left out for a suspect price. */
function AmountNotes({ missing, mismatched }: { missing: number; mismatched: number }) {
  const { t } = useTranslation();
  return <>
    {missing > 0 && <span title={t("ui.overview.partial_amount")} className="ml-0.5 text-warn">*</span>}
    {mismatched > 0 && <span
      title={t("ui.overview.mismatch_excluded", { count: mismatched })}
      className="ml-0.5 text-warn"
    >†</span>}
  </>;
}

type BlockProps = {
  title: string;
  section: { total: number; items: unknown[]; price_mismatch_rows?: number };
  children: React.ReactNode;
  actions?: React.ReactNode;
  footer?: React.ReactNode;
};

function Block({ title, section, children, actions, footer }: BlockProps) {
  const { t } = useTranslation();
  const shown = section.items.length;
  const mismatched = section.price_mismatch_rows ?? 0;
  return <section className="min-w-0 overflow-hidden rounded-lg border bg-card">
    <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b bg-secondary/30 px-3 py-2.5">
      <h2 className="text-sm font-semibold">{title}</h2>
      {shown > 0 && <span className="text-[11px] text-muted-foreground tabular-nums">
        {t("ui.overview.shown_of", { total: section.total, shown })}
      </span>}
      {mismatched > 0 && <span className="text-[11px] text-warn">
        † {t("ui.overview.mismatch_excluded", { count: mismatched })}
      </span>}
      {actions && <div className="ml-auto">{actions}</div>}
    </header>
    {shown === 0
      ? <p className="px-3 py-5 text-xs text-muted-foreground">{t("ui.overview.empty")}</p>
      : <div className="overflow-x-auto">{children}</div>}
    {footer}
  </section>;
}

function HeaderRow({ keys, left }: { keys: readonly string[]; left: readonly string[] }) {
  const { t } = useTranslation();
  return <thead><tr>{keys.map((key) => <th
    key={key}
    className={cn(headerStyle, left.includes(key) && "text-left")}
  >{t(`ui.overview.column.${key}`)}</th>)}</tr></thead>;
}

function Columns({ widths }: { widths: readonly string[] }) {
  return <colgroup>{widths.map((width, index) => <col key={index} className={width}/>)}</colgroup>;
}

const CLUSTER_COLUMNS = ["company", "people", "amount", "average_price", "current", "dates"] as const;
const CLUSTER_WIDTHS = ["w-[27%]", "w-[8%]", "w-[14%]", "w-[13%]", "w-[24%]", "w-[14%]"];

function ClusterRow({ row, sale }: { row: CompanyOverview; sale?: boolean }) {
  const price = sale ? row.sell_price : row.buy_price;
  return <tr className={rowStyle}>
    <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td>
    <td className={cellStyle}>{sale ? row.sell_people : row.buy_people}</td>
    <td className={cellStyle}>
      <Amount
        amount={sale ? row.sell_amount : row.buy_amount}
        estimated={row.amount_estimated}
        direction={sale ? "sell" : "buy"}
      />
      <AmountNotes missing={row.missing_price_rows} mismatched={row.price_mismatch_rows}/>
    </td>
    <td className={cellStyle}><TradePrice price={price} average/></td>
    <td className={cellStyle}><CurrentPrice price={price}/></td>
    <td className={cn(cellStyle, "text-muted-foreground")}>{formatDayRange([row.start_date, row.end_date])}</td>
  </tr>;
}

function ClusterTable({ rows, sale }: { rows: CompanyOverview[]; sale?: boolean }) {
  return <table className="w-full table-fixed text-xs">
    <Columns widths={CLUSTER_WIDTHS}/>
    <HeaderRow keys={CLUSTER_COLUMNS} left={["company"]}/>
    <tbody>{rows.map((row) => <ClusterRow key={row.issuer_id} row={row} sale={sale}/>)}</tbody>
  </table>;
}

const TRADE_WIDTHS = ["w-[23%]", "w-[18%]", "w-[12%]", "w-[12%]", "w-[25%]", "w-[10%]"];

function Owners({ owners }: { owners: OverviewTrade["owners"] }) {
  const location = useLocation();
  return <>{owners.map((owner, index) => {
    const name = readableCase(owner.name);
    return <span key={owner.id}>
      {index > 0 && " · "}
      <Link to={`/people/${owner.id}`} state={sourceContext(location)} className="hover:underline" title={name}>
        {name}
      </Link>
    </span>;
  })}</>;
}

type TradeRowProps = { row: OverviewTrade; sale?: boolean; holdings?: boolean };

function TradeRow({ row, sale, holdings }: TradeRowProps) {
  const location = useLocation();
  return <tr className={rowStyle}>
    <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td>
    <td className="truncate px-3 py-2"><Owners owners={row.owners}/></td>
    <td className={cellStyle}>
      <Amount amount={row.amount} estimated={row.amount_estimated} direction={sale ? "sell" : "buy"}/>
    </td>
    <td className={cellStyle}>{holdings
      ? <span className="text-up">{formatSignedPercent(row.holding_change)}</span>
      : <TradePrice price={row.price}/>}</td>
    <td className={cellStyle}><CurrentPrice price={row.price}/></td>
    <td className={cellStyle}>
      <Link
        to={`/transactions/${row.id}`}
        state={sourceContext(location)}
        className="text-muted-foreground hover:underline"
      >{formatDay(row.date)}</Link>
    </td>
  </tr>;
}

function TradeTable({ rows, sale, holdings }: { rows: OverviewTrade[]; sale?: boolean; holdings?: boolean }) {
  const keys = ["company", "insider", "amount", holdings ? "holding_change" : "trade_price", "current", "date"];
  return <table className="w-full table-fixed text-xs">
    <Columns widths={TRADE_WIDTHS}/>
    <HeaderRow keys={keys} left={["company", "insider"]}/>
    <tbody>{rows.map((row) => <TradeRow key={row.id} row={row} sale={sale} holdings={holdings}/>)}</tbody>
  </table>;
}

const COMPANY_COLUMNS = [
  "company", "buy_people", "buy_amount", "sell_people", "sell_amount", "net_amount", "current",
] as const;
const COMPANY_WIDTHS = ["w-[28%]", "w-[8%]", "w-[13%]", "w-[8%]", "w-[13%]", "w-[14%]", "w-[16%]"];

function CompanyRow({ row }: { row: CompanyOverview }) {
  const current = row.buy_price.current_price != null ? row.buy_price : row.sell_price;
  return <tr className={rowStyle}>
    <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td>
    <td className={cellStyle}>{row.buy_people}</td>
    <td className={cellStyle}>
      <Amount amount={row.buy_amount} estimated={row.buy_price.estimated} direction="buy"/>
    </td>
    <td className={cellStyle}>{row.sell_people}</td>
    <td className={cellStyle}>
      <Amount amount={row.sell_amount} estimated={row.sell_price.estimated} direction="sell"/>
    </td>
    <td className={cellStyle}>
      <Amount amount={row.net_amount} estimated={row.amount_estimated}/>
      <AmountNotes missing={row.missing_price_rows} mismatched={row.price_mismatch_rows}/>
    </td>
    <td className={cellStyle}><CurrentPrice price={current}/></td>
  </tr>;
}

function CompaniesTable({ rows }: { rows: CompanyOverview[] }) {
  return <table className="w-full table-fixed text-xs">
    <Columns widths={COMPANY_WIDTHS}/>
    <HeaderRow keys={COMPANY_COLUMNS} left={["company"]}/>
    <tbody>{rows.map((row) => <CompanyRow key={row.issuer_id} row={row}/>)}</tbody>
  </table>;
}

type ChoiceProps = {
  label: string;
  value: string;
  choose: (value: string) => void;
  options: readonly (string | number)[];
  text: (option: string | number) => string;
  className?: string;
};

function Choice({ label, value, choose, options, text, className }: ChoiceProps) {
  return <label className={cn("flex items-center gap-1.5 text-xs text-muted-foreground", className)}>
    {label}
    <select className={selectStyle} value={value} onChange={(event) => choose(event.target.value)}>
      {options.map((option) => <option key={option} value={option}>{text(option)}</option>)}
    </select>
  </label>;
}

function Toggle({ label, checked, set }: { label: string; checked: boolean; set: (value: string) => void }) {
  return <label className="flex items-center gap-1.5 text-xs">
    <input type="checkbox" checked={checked} onChange={(event) => set(String(event.target.checked))}/>
    {label}
  </label>;
}

function useFilters() {
  const [index, setIndex] = useOverviewChoice("index", INDICES, "all");
  const [days, setDays] = useOverviewChoice("days", ["7", "30", "90"], "30");
  const [role, setRole] = useOverviewChoice("role", ROLES, "all");
  const [minimum, setMinimum] = useOverviewChoice("min_amount", null, "0");
  const [exclude, setExclude] = useOverviewChoice("exclude_plans", ["true", "false"], "false");
  const [clusterDays, setClusterDays] = useOverviewChoice("cluster_days", ["7", "14", "30"], "7");
  const [clusterPeople, setClusterPeople] = useOverviewChoice("cluster_people", ["2", "3"], "2");
  const [excludeCluster, setExcludeCluster] = useOverviewChoice(
    "exclude_cluster_plans", ["true", "false"], "true");
  const [companySort, setCompanySort] = useOverviewChoice("company_sort", COMPANY_SORTS, "net_buy");
  const values = { index, days, role, minimum, exclude, clusterDays, clusterPeople, excludeCluster, companySort };
  const setters = {
    setIndex, setDays, setRole, setMinimum, setExclude,
    setClusterDays, setClusterPeople, setExcludeCluster, setCompanySort,
  };
  const query = {
    index: index as Index,
    days: Number(days) as 7 | 30 | 90,
    role: role as Role,
    min_amount: Number(minimum),
    exclude_plans: exclude === "true",
    cluster_days: Number(clusterDays) as 7 | 14 | 30,
    cluster_people: Number(clusterPeople) as 2 | 3,
    exclude_cluster_plans: excludeCluster === "true",
    company_sort: companySort as CompanySort,
  };
  return { values, setters, query };
}

type Filters = ReturnType<typeof useFilters>;

/** Write the effective defaults into the URL once, so a shared link reproduces the view. */
function useFiltersInUrl(query: Filters["query"]) {
  const [params, setParams] = useSearchParams();
  const signature = JSON.stringify(query);
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    let changed = false;
    for (const [name, value] of Object.entries(JSON.parse(signature) as Record<string, unknown>)) {
      if (next.has(name)) continue;
      next.set(name, String(value));
      changed = true;
    }
    if (changed) setParams(next, { replace: true });
  }, [params, setParams, signature]);
}

function FilterBar({ values, setters }: Pick<Filters, "values" | "setters">) {
  const { t } = useTranslation();
  const days = (count: string | number) => t("ui.overview.days", { count: Number(count) });
  return <div className="space-y-2 rounded-lg border bg-card p-3">
    <div className="flex flex-wrap items-center gap-3">
      <IndexFilter value={values.index as Index} choose={setters.setIndex}/>
      <Choice label={t("ui.overview.time_label")} value={values.days} choose={setters.setDays}
        options={[7, 30, 90]} text={days}/>
      <Choice label={t("ui.overview.role_label")} value={values.role} choose={setters.setRole}
        options={ROLES} text={(item) => t(`ui.overview.role.${item}`)}/>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {t("ui.overview.minimum")}
        <input
          type="number" min="0" step="1000"
          className={cn(selectStyle, "w-24 tabular-nums")}
          value={values.minimum}
          onChange={(event) => setters.setMinimum(event.target.value || "0")}
        />
      </label>
      <Toggle label={t("ui.overview.exclude_plans")} checked={values.exclude === "true"} set={setters.setExclude}/>
    </div>
    <IndexStatus value={values.index as Index}/>
    <div className="flex flex-wrap items-center gap-3 border-t pt-2 text-xs">
      <span className="font-medium">{t("ui.overview.cluster_label")}</span>
      <Choice label={t("ui.overview.cluster_people")} value={values.clusterPeople}
        choose={setters.setClusterPeople} options={[2, 3]}
        text={(count) => t("ui.overview.at_least_people", { count: Number(count) })}/>
      <Choice label={t("ui.overview.cluster_window")} value={values.clusterDays}
        choose={setters.setClusterDays} options={[7, 14, 30]} text={days}/>
      <Toggle label={t("ui.overview.exclude_cluster_plans")} checked={values.excludeCluster === "true"}
        set={setters.setExcludeCluster}/>
    </div>
  </div>;
}

/**
 * The overview re-reads at the SEC polling pace. Right after a quote request,
 * while quotes it asked for are still missing, it re-reads a little sooner.
 */
function useOverview(query: Filters["query"], companyLimit: number | undefined) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const [askedAt, setAskedAt] = useState<number | null>(null);
  const params = { ...query, ...(companyLimit ? { company_limit: companyLimit } : {}) };
  const result = useQuery({
    queryKey: ["insider-overview", params],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/overview", { params: { query: params }, signal })),
    enabled: visible,
    placeholderData: (previous) => previous,
    refetchInterval: (state) => {
      if (!visible) return false;
      const pending = (state.state.data?.quotes_pending ?? 0) > 0;
      const recent = askedAt != null && Date.now() - askedAt < QUOTE_FOLLOW_UP_WINDOW_MS;
      return pending && recent ? QUOTE_FOLLOW_UP_MS : intervals.overview_poll_seconds * 1000;
    },
  });
  const quotes = useStockQuotes(result.data?.quote_symbols ?? []);
  useEffect(() => { if (quotes.askedAt) setAskedAt(quotes.askedAt); }, [quotes.askedAt]);
  return { query: result, quoteError: quotes.error };
}

function CompanySortChoice({ value, choose }: { value: string; choose: (value: string) => void }) {
  const { t } = useTranslation();
  return <Choice label={t("ui.overview.company_sort_label")} value={value} choose={choose}
    options={COMPANY_SORTS} text={(item) => t(`ui.overview.company_sort.${item}`)}/>;
}

function ShowMore({ section, more }: { section: Overview["companies"]; more: () => void }) {
  const { t } = useTranslation();
  if (section.items.length >= section.total) return null;
  return <div className="border-t px-3 py-2 text-center">
    <button type="button" className="text-xs font-medium hover:underline" onClick={more}>
      {t("ui.overview.show_more")}
    </button>
  </div>;
}

type SectionValues = Filters["values"] & { chooseSort: (value: string) => void };

function Sections({ data, values, more }: { data: Overview; values: SectionValues; more: () => void }) {
  const { t } = useTranslation();
  const people = values.clusterPeople;
  const plansExcluded = values.excludeCluster === "true" || values.exclude === "true";
  const plans = t(plansExcluded ? "ui.overview.plans_excluded" : "ui.overview.plans_included");
  return <>
    <Block title={t("ui.overview.cluster_buys", { days: values.clusterDays, people })} section={data.cluster_buys}>
      <ClusterTable rows={data.cluster_buys.items}/>
    </Block>
    <Block
      title={t("ui.overview.cluster_sales", { days: values.clusterDays, people, plans })}
      section={data.cluster_sales}
    >
      <ClusterTable rows={data.cluster_sales.items} sale/>
    </Block>
    <Block title={t("ui.overview.large_buys")} section={data.large_buys}>
      <TradeTable rows={data.large_buys.items}/>
    </Block>
    <Block title={t("ui.overview.large_sales")} section={data.large_sales}>
      <TradeTable rows={data.large_sales.items} sale/>
    </Block>
    <Block title={t("ui.overview.executive_buys")} section={data.executive_buys}>
      <TradeTable rows={data.executive_buys.items}/>
    </Block>
    <Block title={t("ui.overview.holding_increases")} section={data.holding_increases}>
      <TradeTable rows={data.holding_increases.items} holdings/>
    </Block>
    <Block
      title={t("ui.overview.companies")}
      section={data.companies}
      actions={<CompanySortChoice value={values.companySort} choose={values.chooseSort}/>}
      footer={<ShowMore section={data.companies} more={more}/>}
    >
      <CompaniesTable rows={data.companies.items}/>
    </Block>
  </>;
}

function quotePrices(data: Overview) {
  return [
    ...data.cluster_buys.items.map((row) => row.buy_price),
    ...data.cluster_sales.items.map((row) => row.sell_price),
    ...data.companies.items.flatMap((row) => [row.buy_price, row.sell_price]),
  ];
}

export function InsiderOverview() {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const { values, setters, query } = useFilters();
  useFiltersInUrl(query);
  // "Show more" grows the company list; any filter or order change starts again.
  const [companyLimit, setCompanyLimit] = useState<number | undefined>(undefined);
  const signature = JSON.stringify(query);
  useEffect(() => setCompanyLimit(undefined), [signature]);
  const { query: overview, quoteError } = useOverview(query, companyLimit);
  const data = overview.data;
  const { n } = useInsiderN();
  const priceKeys = useMemo(() => data?.price_keys ?? [], [data]);
  useTradeWindows(priceKeys, n, visible, true);
  const more = () => {
    if (data) setCompanyLimit(data.companies.items.length + data.company_page_rows);
  };
  const sectionValues = { ...values, chooseSort: setters.setCompanySort };
  return <div className="space-y-3">
    <FilterBar values={values} setters={setters}/>
    <p className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
      {t("ui.overview.scope_rule")}
      {data && <span className="ml-auto tabular-nums">{t("ui.feed.as_of", { time: formatEt(data.as_of) })}</span>}
      {overview.isFetching && <LoaderCircle className="size-3 motion-safe:animate-spin"/>}
    </p>
    {data && <div className="flex flex-wrap gap-3"><CurrentQuoteStatus prices={quotePrices(data)}/></div>}
    {overview.error && <p className="text-sm text-destructive">{overview.error.message}</p>}
    {quoteError && <p className="text-xs text-warn">{quoteError.message}</p>}
    {!data && overview.isPending
      ? <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 motion-safe:animate-spin"/>{t("ui.overview.loading")}
      </p>
      : data && <MismatchRatio.Provider value={data.price_mismatch_ratio}>
        <Sections data={data} values={sectionValues} more={more}/>
      </MismatchRatio.Provider>}
  </div>;
}
