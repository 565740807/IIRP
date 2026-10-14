import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LoaderCircle, SlidersHorizontal } from "lucide-react";
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
  SECTOR_FORM,
  sectorChoice,
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
import { choiceFrom, withChoice } from "@/lib/urlChoice";
import { InfoTip } from "@/components/ui/info";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ErrorState, TableSkeleton } from "@/components/ui/states";
import { TabBar } from "@/components/ui/tab-bar";
import { IndexFilter, IndexStatus } from "./IndexFilter";
import { IndexMarks } from "./IndexMarks";
import { TickerBatch, TickerMarks } from "@/lib/tickerMarks";
import { WatchMark } from "@/components/watchlist/Watch";
import { useWatchFilter, WatchFilter } from "@/components/watchlist/WatchFilter";
import { WATCH_DEPENDENT } from "@/lib/watchlist";
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

type CompanyRef = { issuer_id: string; name: string; ticker: string | null; indexes?: readonly string[] };

function CompanyLink({ row }: { row: CompanyRef }) {
  const location = useLocation();
  const name = readableCase(row.name);
  return <Link
    to={`/companies/${row.issuer_id}`}
    state={sourceContext(location)}
    className="flex min-w-0 items-baseline gap-2 hover:underline"
    title={name}
  >
    <span className="relative shrink-0 rounded bg-secondary px-1 text-xs font-semibold">
      {row.ticker ?? "—"}
      <WatchMark ticker={row.ticker} corner/>
    </span>
    <span className="truncate font-medium">{name}</span>
    <IndexMarks indexes={row.indexes} />
  </Link>;
}

/** The company link, then the marks beside its code (outside the link: a mark has its own target). */
function Company({ row }: { row: CompanyRef }) {
  return <span className="flex min-w-0 items-baseline gap-1.5">
    <CompanyLink row={row}/>
    <TickerMarks ticker={row.ticker} compact className="self-center"/>
  </span>;
}

type AmountProps = { amount?: string | null; estimated?: boolean; direction?: "buy" | "sell" };

function Amount({ amount, estimated, direction }: AmountProps) {
  const { t } = useTranslation();
  // Direction supplies a display sign; totals and prices are computed by the backend.
  const sign = direction === "buy" ? "+" : "-";
  const display = amount != null && direction ? `${sign}${amount.replace(/^[+-]/, "")}` : amount;
  const tone = direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : directionClass(amount);
  return <span className={tone}>
    {estimated && <span className="mr-1 text-xs text-muted-foreground">{t("ui.overview.estimated")}</span>}
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

/** Whether the overview shows only the watched companies (empty lists then say so). */
const WatchScope = createContext(false);

function Block({ title, section, children, actions, footer }: BlockProps) {
  const { t } = useTranslation();
  const watched = useContext(WatchScope);
  const shown = section.items.length;
  const mismatched = section.price_mismatch_rows ?? 0;
  return <section className="min-w-0 overflow-hidden rounded-lg border bg-card">
    <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b bg-secondary/30 px-3 py-2.5">
      <h2 className="text-sm font-semibold">{title}</h2>
      {shown > 0 && <span className="text-xs text-muted-foreground tabular-nums">
        {t("ui.overview.shown_of", { total: section.total, shown })}
      </span>}
      {mismatched > 0 && <span className="text-xs text-warn">
        † {t("ui.overview.mismatch_excluded", { count: mismatched })}
      </span>}
      {actions && <div className="ml-auto">{actions}</div>}
    </header>
    {shown === 0
      ? <p className="px-3 py-5 text-xs text-muted-foreground">{t(watched ? "ui.watchlist.overview_empty" : "ui.overview.empty")}</p>
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
    <td className="overflow-hidden px-3 py-2"><Company row={row}/></td>
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
    <td className={cellStyle}><CurrentPrice price={price} narrow/></td>
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
    <td className="overflow-hidden px-3 py-2"><Company row={row}/></td>
    <td className="truncate px-3 py-2"><Owners owners={row.owners}/></td>
    <td className={cellStyle}>
      <Amount amount={row.amount} estimated={row.amount_estimated} direction={sale ? "sell" : "buy"}/>
    </td>
    <td className={cellStyle}>{holdings
      ? <span className="text-up">{formatSignedPercent(row.holding_change)}</span>
      : <TradePrice price={row.price}/>}</td>
    <td className={cellStyle}><CurrentPrice price={row.price} narrow/></td>
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
    <td className="overflow-hidden px-3 py-2"><Company row={row}/></td>
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
    <td className={cellStyle}><CurrentPrice price={current} narrow/></td>
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
  const [sector, setSector] = useOverviewChoice("sector", SECTOR_FORM, "all");
  const [watchlist, setWatchlist] = useOverviewChoice("watchlist", ["true", "false"], "false");
  const values = { index, days, role, minimum, exclude, clusterDays, clusterPeople, excludeCluster, companySort, sector, watchlist };
  const setters = {
    setIndex, setDays, setRole, setMinimum, setExclude,
    setClusterDays, setClusterPeople, setExcludeCluster, setCompanySort, setSector, setWatchlist,
  };
  // "Watchlist only" applies while the watchlist has codes; an empty one leaves every company.
  const watch = useWatchFilter(watchlist === "true").active;
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
    sector,
    watchlist: watch,
  };
  return { values, setters, query };
}

type Filters = ReturnType<typeof useFilters>;

/** Write the effective defaults into the URL once, so a shared link reproduces the view. */
function useFiltersInUrl(query: Filters["query"]) {
  const pathname = useRef(useLocation().pathname).current;
  const [params, setParams] = useSearchParams();
  const signature = JSON.stringify(query);
  useEffect(() => {
    // A lazy destination keeps this page mounted during the route transition.
    // Its late default normalization must not navigate back to this route.
    if (window.location.pathname !== pathname) return;
    const next = new URLSearchParams(window.location.search);
    let changed = false;
    for (const [name, value] of Object.entries(JSON.parse(signature) as Record<string, unknown>)) {
      if (next.has(name)) continue;
      next.set(name, String(value));
      changed = true;
    }
    if (changed) setParams(next, { replace: true });
  }, [params, setParams, signature, pathname]);
}

/** Number of secondary filters that differ from their defaults, shown on the "More" button. */
function changedSecondary(values: Filters["values"]) {
  return [values.minimum !== "0", values.exclude !== "false", values.clusterPeople !== "2",
    values.clusterDays !== "7", values.excludeCluster !== "true"].filter(Boolean).length;
}

/** Every sector the server offers, then companies without a saved sector. */
function SectorChoice({ value, choose, sectors }: { value: string; choose: (value: string) => void; sectors: readonly string[] }) {
  const { t } = useTranslation();
  const options = ["all", ...new Set([...sectors, ...(value !== "all" && value !== "unclassified" ? [value] : [])]), "unclassified"];
  return <span className="flex items-center gap-1">
    <Choice label={t("ui.overview.sector_label")} value={value} choose={choose} options={options}
      text={(item) => item === "all" || item === "unclassified" ? t(`ui.overview.sector.${item}`) : t(`ui.sectors.name.${item}`)}/>
    <InfoTip>{t("ui.overview.sector_rule")}</InfoTip>
  </span>;
}

/** One row: index, sector, period and role; the minimum amount, plan exclusions and the cluster rule under "More". */
function FilterBar({ values, setters, status, sectors }: Pick<Filters, "values" | "setters"> & { status?: React.ReactNode; sectors: readonly string[] }) {
  const { t } = useTranslation();
  const days = (count: string | number) => t("ui.overview.days", { count: Number(count) });
  const changed = changedSecondary(values);
  return <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
    <IndexFilter value={values.index as Index} choose={setters.setIndex}/>
    <WatchFilter chosen={values.watchlist === "true"} choose={(on) => setters.setWatchlist(String(on))}/>
    <SectorChoice value={values.sector} choose={setters.setSector} sectors={sectors}/>
    <Choice label={t("ui.overview.time_label")} value={values.days} choose={setters.setDays}
      options={[7, 30, 90]} text={days}/>
    <Choice label={t("ui.overview.role_label")} value={values.role} choose={setters.setRole}
      options={ROLES} text={(item) => t(`ui.overview.role.${item}`)}/>
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" data-more-filters className="inline-flex h-8 items-center gap-1 rounded-md border bg-card px-2.5 text-xs hover:bg-accent">
          <SlidersHorizontal className="size-3.5"/>
          {t("ui.overview.more_filters")}
          {changed > 0 && <span className="rounded-full bg-primary px-1.5 text-primary-foreground tabular-nums">{changed}</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 space-y-3">
        <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          {t("ui.overview.minimum")}
          <input
            type="number" min="0" step="1000"
            className={cn(selectStyle, "w-28 tabular-nums")}
            value={values.minimum}
            onChange={(event) => setters.setMinimum(event.target.value || "0")}
          />
        </label>
        <Toggle label={t("ui.overview.exclude_plans")} checked={values.exclude === "true"} set={setters.setExclude}/>
        <div className="space-y-2 border-t pt-2.5 text-xs">
          <p className="font-medium">{t("ui.overview.cluster_label")}</p>
          <Choice label={t("ui.overview.cluster_people")} value={values.clusterPeople} className="justify-between"
            choose={setters.setClusterPeople} options={[2, 3]}
            text={(count) => t("ui.overview.at_least_people", { count: Number(count) })}/>
          <Choice label={t("ui.overview.cluster_window")} value={values.clusterDays} className="justify-between"
            choose={setters.setClusterDays} options={[7, 14, 30]} text={days}/>
          <Toggle label={t("ui.overview.exclude_cluster_plans")} checked={values.excludeCluster === "true"}
            set={setters.setExcludeCluster}/>
        </div>
      </PopoverContent>
    </Popover>
    <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">{status}</span>
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
    // A watchlist-only overview is read again whenever the watchlist changes.
    queryKey: ["insider-overview", params, ...(query.watchlist ? [WATCH_DEPENDENT] : [])],
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
const TABS = ["cluster_buys", "cluster_sales", "large_buys", "large_sales", "executive_buys", "holding_increases", "companies"] as const;
type Tab = (typeof TABS)[number];

/** One tab at a time: its table under a one-line rule; the tab is kept in the link (?tab=). */
function Sections({ data, values, more }: { data: Overview; values: SectionValues; more: () => void }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = choiceFrom(params, "tab", TABS, "cluster_buys");
  const choose = (next: Tab) => setParams(withChoice(new URLSearchParams(window.location.search), "tab", next, "cluster_buys"), { replace: true });
  const people = values.clusterPeople;
  const plansExcluded = values.excludeCluster === "true" || values.exclude === "true";
  const plans = t(plansExcluded ? "ui.overview.plans_excluded" : "ui.overview.plans_included");
  const title: Record<Tab, string> = {
    cluster_buys: t("ui.overview.cluster_buys", { days: values.clusterDays, people }),
    cluster_sales: t("ui.overview.cluster_sales", { days: values.clusterDays, people, plans }),
    large_buys: t("ui.overview.large_buys"),
    large_sales: t("ui.overview.large_sales"),
    executive_buys: t("ui.overview.executive_buys"),
    holding_increases: t("ui.overview.holding_increases"),
    companies: t("ui.overview.companies"),
  };
  const tickers = (data[tab].items as readonly CompanyRef[]).map((row) => row.ticker);
  return <TickerBatch tickers={tickers}><div className="space-y-2">
    <TabBar
      label={t("ui.overview.tabs_label")}
      value={tab}
      onChange={choose}
      items={TABS.map((id) => ({ id, label: t(`ui.overview.tab.${id}`), count: data[id].total }))}
    />
    {tab === "cluster_buys" && <Block title={title.cluster_buys} section={data.cluster_buys}>
      <ClusterTable rows={data.cluster_buys.items}/>
    </Block>}
    {tab === "cluster_sales" && <Block title={title.cluster_sales} section={data.cluster_sales}>
      <ClusterTable rows={data.cluster_sales.items} sale/>
    </Block>}
    {tab === "large_buys" && <Block title={title.large_buys} section={data.large_buys}>
      <TradeTable rows={data.large_buys.items}/>
    </Block>}
    {tab === "large_sales" && <Block title={title.large_sales} section={data.large_sales}>
      <TradeTable rows={data.large_sales.items} sale/>
    </Block>}
    {tab === "executive_buys" && <Block title={title.executive_buys} section={data.executive_buys}>
      <TradeTable rows={data.executive_buys.items}/>
    </Block>}
    {tab === "holding_increases" && <Block title={title.holding_increases} section={data.holding_increases}>
      <TradeTable rows={data.holding_increases.items} holdings/>
    </Block>}
    {tab === "companies" && <Block
      title={title.companies}
      section={data.companies}
      actions={<CompanySortChoice value={values.companySort} choose={values.chooseSort}/>}
      footer={<ShowMore section={data.companies} more={more}/>}
    >
      <CompaniesTable rows={data.companies.items}/>
    </Block>}
  </div></TickerBatch>;
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
  // Once the server has named its sectors, a remembered or linked unknown one falls back to all.
  const sectors = data?.sectors ?? null;
  useEffect(() => {
    if (sectors && sectorChoice(values.sector, sectors) !== values.sector) setters.setSector("all");
  }, [sectors, values.sector, setters]);
  return <div className="space-y-2.5">
    <FilterBar values={values} setters={setters} sectors={sectors ?? []} status={<>
      {data && <span className="tabular-nums">{t("ui.feed.as_of", { time: formatEt(data.as_of) })}</span>}
      {overview.isFetching && <LoaderCircle className="size-3 motion-safe:animate-spin"/>}
      <InfoTip side="left">{t("ui.overview.scope_rule")}</InfoTip>
    </>}/>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <IndexStatus value={values.index as Index}/>
      {data && <CurrentQuoteStatus prices={quotePrices(data)}/>}
    </div>
    {overview.error && <ErrorState error={overview.error} retry={() => void overview.refetch()}/>}
    {quoteError && <p className="text-xs text-warn">{quoteError.message}</p>}
    {!data && overview.isPending
      ? <TableSkeleton rows={10} columns={6}/>
      : data && <MismatchRatio.Provider value={data.price_mismatch_ratio}>
        <WatchScope.Provider value={data.watchlist}>
          <Sections data={data} values={sectionValues} more={more}/>
        </WatchScope.Provider>
      </MismatchRatio.Provider>}
  </div>;
}
