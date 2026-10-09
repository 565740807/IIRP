import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LoaderCircle } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { directionClass, formatDay, formatDayRange, formatEt, formatMoney, formatSignedPercent } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { INDICES, useOverviewChoice, useStockQuotes, useVisibleStocks, type CompanyOverview, type OverviewTrade, type IndexFilter as Index } from "@/lib/insiderOverview";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { sourceContext } from "@/lib/navigation";
import { useInsiderN, useTradeWindows } from "@/lib/windows";
import { cn } from "@/lib/utils";
import { IndexFilter, IndexStatus } from "./IndexFilter";
import { CurrentPrice, CurrentQuoteStatus, TradePrice } from "./InsiderPrice";

const selectStyle = "h-8 rounded-md border bg-card px-2 text-xs outline-none focus:border-ring";
const headerStyle = "whitespace-nowrap px-3 py-2 text-right font-medium text-muted-foreground";
const cellStyle = "whitespace-nowrap px-3 py-2 text-right tabular-nums";

function CompanyLink({ row }: { row: { issuer_id: string; name: string; ticker: string | null } }) {
  const location = useLocation();
  return <Link to={`/companies/${row.issuer_id}`} state={sourceContext(location)} className="flex min-w-0 items-baseline gap-2 hover:underline" title={readableCase(row.name)}>
    <span className="shrink-0 rounded bg-secondary px-1 text-[11px] font-semibold">{row.ticker ?? "—"}</span>
    <span className="truncate font-medium">{readableCase(row.name)}</span>
  </Link>;
}

function Amount({ amount, estimated, direction }: { amount?: string | null; estimated?: boolean; direction?: "buy" | "sell" }) {
  const { t } = useTranslation();
  // Direction supplies a display sign; totals and prices are computed by the backend.
  const display = amount != null && direction ? `${direction === "buy" ? "+" : "-"}${amount.replace(/^[+-]/, "")}` : amount;
  return <span className={cn(direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : directionClass(amount))}>
    {estimated && <span className="mr-1 text-[10px] text-muted-foreground">{t("ui.overview.estimated")}</span>}{formatMoney(display, { signed: true })}
  </span>;
}

function Block({ title, children, empty }: { title: string; children: React.ReactNode; empty: boolean }) {
  const { t } = useTranslation();
  return <section className="min-w-0 overflow-hidden rounded-lg border bg-card">
    <h2 className="border-b bg-secondary/30 px-3 py-2.5 text-sm font-semibold">{title}</h2>
    {empty ? <p className="px-3 py-5 text-xs text-muted-foreground">{t("ui.overview.empty")}</p> : <div className="overflow-x-auto">{children}</div>}
  </section>;
}

function ClusterTable({ rows, sale }: { rows: CompanyOverview[]; sale?: boolean }) {
  const { t } = useTranslation();
  return <table className="w-full table-fixed text-xs">
    <colgroup><col className="w-[27%]"/><col className="w-[8%]"/><col className="w-[14%]"/><col className="w-[13%]"/><col className="w-[24%]"/><col className="w-[14%]"/></colgroup>
    <thead><tr>{["company", "people", "amount", "average_price", "current", "dates"].map((key) => <th key={key} className={cn(headerStyle, key === "company" && "text-left")}>{t(`ui.overview.column.${key}`)}</th>)}</tr></thead>
    <tbody>{rows.map((row) => <tr key={`${row.issuer_id}:${row.ticker}`} data-quote-symbol={row.ticker ?? ""} className="border-t hover:bg-accent/40">
      <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td>
      <td className={cellStyle}>{sale ? row.sell_people : row.buy_people}</td>
      <td className={cellStyle}><Amount amount={sale ? row.sell_amount : row.buy_amount} estimated={row.amount_estimated} direction={sale ? "sell" : "buy"}/>{row.missing_price_rows > 0 && <span title={t("ui.overview.partial_amount")} className="ml-0.5 text-warn">*</span>}</td>
      <td className={cellStyle}><TradePrice price={sale ? row.sell_price : row.buy_price} average/></td>
      <td className={cellStyle}><CurrentPrice price={sale ? row.sell_price : row.buy_price}/></td>
      <td className={cn(cellStyle, "text-muted-foreground")}>{formatDayRange([row.start_date, row.end_date])}</td>
    </tr>)}</tbody>
  </table>;
}

function TradeRows({ rows, sale, holdings }: { rows: OverviewTrade[]; sale?: boolean; holdings?: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  return <table className="w-full table-fixed text-xs">
    <colgroup><col className="w-[23%]"/><col className="w-[18%]"/><col className="w-[12%]"/><col className="w-[12%]"/><col className="w-[25%]"/><col className="w-[10%]"/></colgroup>
    <thead><tr>{["company", "insider", "amount", holdings ? "holding_change" : "trade_price", "current", "date"].map((key) => <th key={key} className={cn(headerStyle, ["company", "insider"].includes(key) && "text-left")}>{t(`ui.overview.column.${key}`)}</th>)}</tr></thead>
    <tbody>{rows.map((row) => <tr key={row.id} data-quote-symbol={row.ticker ?? ""} className="border-t hover:bg-accent/40">
      <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td>
      <td className="truncate px-3 py-2">{row.owners.map((owner, index) => <span key={owner.id}>{index > 0 && " · "}<Link to={`/people/${owner.id}`} state={sourceContext(location)} className="hover:underline" title={readableCase(owner.name)}>{readableCase(owner.name)}</Link></span>)}</td>
      <td className={cellStyle}><Amount amount={row.amount} estimated={row.amount_estimated} direction={sale ? "sell" : "buy"}/></td>
      <td className={cellStyle}>{holdings ? <span className="text-up">{formatSignedPercent(row.holding_change)}</span> : <TradePrice price={row.price}/>}</td>
      <td className={cellStyle}><CurrentPrice price={row.price}/></td>
      <td className={cellStyle}><Link to={`/transactions/${row.id}`} state={sourceContext(location)} className="text-muted-foreground hover:underline">{formatDay(row.date)}</Link></td>
    </tr>)}</tbody>
  </table>;
}

function CompaniesTable({ rows }: { rows: CompanyOverview[] }) {
  const { t } = useTranslation();
  return <table className="w-full table-fixed text-xs">
    <colgroup><col className="w-[28%]"/><col className="w-[8%]"/><col className="w-[13%]"/><col className="w-[8%]"/><col className="w-[13%]"/><col className="w-[14%]"/><col className="w-[16%]"/></colgroup>
    <thead><tr>{["company", "buy_people", "buy_amount", "sell_people", "sell_amount", "net_amount", "current"].map((key) => <th key={key} className={cn(headerStyle, key === "company" && "text-left")}>{t(`ui.overview.column.${key}`)}</th>)}</tr></thead>
    <tbody>{rows.map((row) => <tr key={`${row.issuer_id}:${row.ticker}`} data-quote-symbol={row.ticker ?? ""} className="border-t hover:bg-accent/40">
      <td className="overflow-hidden px-3 py-2"><CompanyLink row={row}/></td><td className={cellStyle}>{row.buy_people}</td>
      <td className={cellStyle}><Amount amount={row.buy_amount} estimated={row.amount_estimated} direction="buy"/></td><td className={cellStyle}>{row.sell_people}</td>
      <td className={cellStyle}><Amount amount={row.sell_amount} estimated={row.amount_estimated} direction="sell"/></td>
      <td className={cellStyle}><Amount amount={row.net_amount} estimated={row.amount_estimated}/>{row.missing_price_rows > 0 && <span className="ml-0.5 text-warn" title={t("ui.overview.partial_amount")}>*</span>}</td>
      <td className={cellStyle}><CurrentPrice price={row.buy_price.current_price != null ? row.buy_price : row.sell_price}/></td>
    </tr>)}</tbody>
  </table>;
}

export function InsiderOverview() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [index, setIndex] = useOverviewChoice("index", INDICES, "all");
  const [days, setDays] = useOverviewChoice("days", ["7", "30", "90"], "30");
  const [role, setRole] = useOverviewChoice("role", ["all", "executive", "director", "ten_percent"], "all");
  const [minimum, setMinimum] = useOverviewChoice("min_amount", null, "0");
  const [exclude, setExclude] = useOverviewChoice("exclude_plans", ["true", "false"], "false");
  const [clusterDays, setClusterDays] = useOverviewChoice("cluster_days", ["7", "14", "30"], "7");
  const [clusterPeople, setClusterPeople] = useOverviewChoice("cluster_people", ["2", "3"], "2");
  const [excludeCluster, setExcludeCluster] = useOverviewChoice("exclude_cluster_plans", ["true", "false"], "true");
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const filters = { index: index as Index, days: Number(days) as 7 | 30 | 90, role: role as "all" | "executive" | "director" | "ten_percent", min_amount: Number(minimum), exclude_plans: exclude === "true", cluster_days: Number(clusterDays) as 7 | 14 | 30, cluster_people: Number(clusterPeople) as 2 | 3, exclude_cluster_plans: excludeCluster === "true" };
  useEffect(() => {
    const next = new URLSearchParams(params);
    let changed = false;
    for (const [name, value] of Object.entries(filters)) {
      if (next.has(name)) continue;
      next.set(name, String(value));
      changed = true;
    }
    if (changed) setParams(next, { replace: true });
  }, [params, setParams, index, days, role, minimum, exclude, clusterDays, clusterPeople, excludeCluster]);
  const query = useQuery({ queryKey: ["insider-overview", filters], queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/overview", { params: { query: filters }, signal })), enabled: visible, refetchInterval: visible ? intervals.home_poll_seconds * 1000 : false, placeholderData: (previous) => previous });
  const data = query.data;
  const container = useRef<HTMLDivElement>(null);
  const stocks = useVisibleStocks(container, data);
  const quoteError = useStockQuotes(stocks.symbols);
  const { n } = useInsiderN();
  const priceKeys = useMemo(() => data?.price_keys ?? [], [data]);
  useTradeWindows(priceKeys, n, visible, true);
  return <div className="space-y-3" ref={container}>
    <div className="space-y-2 rounded-lg border bg-card p-3">
      <div className="flex flex-wrap items-center gap-3">
        <IndexFilter value={index as Index} choose={setIndex}/>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">{t("ui.overview.time_label")}<select className={selectStyle} value={days} onChange={(event) => setDays(event.target.value)}>{[7, 30, 90].map((day) => <option key={day} value={day}>{t("ui.overview.days", { count: day })}</option>)}</select></label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">{t("ui.overview.role_label")}<select className={selectStyle} value={role} onChange={(event) => setRole(event.target.value)}>{["all", "executive", "director", "ten_percent"].map((item) => <option key={item} value={item}>{t(`ui.overview.role.${item}`)}</option>)}</select></label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">{t("ui.overview.minimum")}<input type="number" min="0" step="1000" className={cn(selectStyle, "w-24 tabular-nums")} value={minimum} onChange={(event) => setMinimum(event.target.value || "0")}/></label>
        <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={exclude === "true"} onChange={(event) => setExclude(String(event.target.checked))}/>{t("ui.overview.exclude_plans")}</label>
      </div>
      <IndexStatus value={index as Index}/>
      <div className="flex flex-wrap items-center gap-3 border-t pt-2 text-xs">
        <span className="font-medium">{t("ui.overview.cluster_label")}</span>
        <label className="flex items-center gap-1.5 text-muted-foreground">{t("ui.overview.cluster_people")}<select className={selectStyle} value={clusterPeople} onChange={(event) => setClusterPeople(event.target.value)}>{[2, 3].map((count) => <option key={count} value={count}>{t("ui.overview.at_least_people", { count })}</option>)}</select></label>
        <label className="flex items-center gap-1.5 text-muted-foreground">{t("ui.overview.cluster_window")}<select className={selectStyle} value={clusterDays} onChange={(event) => setClusterDays(event.target.value)}>{[7, 14, 30].map((count) => <option key={count} value={count}>{t("ui.overview.days", { count })}</option>)}</select></label>
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={excludeCluster === "true"} onChange={(event) => setExcludeCluster(String(event.target.checked))}/>{t("ui.overview.exclude_cluster_plans")}</label>
      </div>
    </div>
    <p className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">{t("ui.overview.scope_rule")}{data && <span className="ml-auto tabular-nums">{t("ui.feed.as_of", { time: formatEt(data.as_of) })}</span>}{query.isFetching && <LoaderCircle className="size-3 motion-safe:animate-spin"/>}</p>
    {data && <div className="flex flex-wrap gap-3"><CurrentQuoteStatus prices={[...data.cluster_buys.map((row) => row.buy_price), ...data.cluster_sales.map((row) => row.sell_price), ...data.companies.flatMap((row) => [row.buy_price, row.sell_price])]}/></div>}
    {query.error && <p className="text-sm text-destructive">{query.error.message}</p>}
    {quoteError && <p className="text-xs text-warn">{quoteError.message}</p>}
    {!data && query.isPending ? <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><LoaderCircle className="size-4 motion-safe:animate-spin"/>{t("ui.overview.loading")}</p> : data && <>
      <Block title={t("ui.overview.cluster_buys", { days: clusterDays, people: clusterPeople })} empty={!data.cluster_buys.length}><ClusterTable rows={data.cluster_buys}/></Block>
      <Block title={t("ui.overview.cluster_sales", { days: clusterDays, people: clusterPeople, plans: excludeCluster === "true" || exclude === "true" ? t("ui.overview.plans_excluded") : t("ui.overview.plans_included") })} empty={!data.cluster_sales.length}><ClusterTable rows={data.cluster_sales} sale/></Block>
      <Block title={t("ui.overview.large_buys")} empty={!data.large_buys.length}><TradeRows rows={data.large_buys}/></Block>
      <Block title={t("ui.overview.large_sales")} empty={!data.large_sales.length}><TradeRows rows={data.large_sales} sale/></Block>
      <Block title={t("ui.overview.executive_buys")} empty={!data.executive_buys.length}><TradeRows rows={data.executive_buys}/></Block>
      <Block title={t("ui.overview.holding_increases")} empty={!data.holding_increases.length}><TradeRows rows={data.holding_increases} holdings/></Block>
      <Block title={t("ui.overview.companies")} empty={!data.companies.length}><CompaniesTable rows={data.companies}/></Block>
    </>}
  </div>;
}
