import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CalendarRange, ChevronLeft, ChevronRight, LoaderCircle, X } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { formatDay, formatNumber } from "@/lib/format";
import { NOTABLE_DEPENDENT, personName, useNotablePeople, type NotablePerson } from "@/lib/notable";
import { openSettings } from "@/lib/settings";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Explain } from "@/components/ui/info";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/ui/states";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { NControl } from "@/components/insider/NControl";
import { AssetChoice } from "@/components/notable/AssetChoice";
import { NotableTable } from "@/components/notable/NotableTable";

const PAGE = 5;
const SCOPES = ["watched", "all"] as const;
const SOURCES = ["sec", "house", "oge"] as const;
const SIDES = ["buy", "sell"] as const;
const CATEGORY_ORDER = ["investor", "investment_firm", "founder_ceo", "house", "president", "cabinet"] as const;
const selectStyle = "h-8 rounded-md border bg-card px-2 text-xs outline-none focus:border-ring";

type Filters = {
  scope: (typeof SCOPES)[number];
  assets: "equities" | "all";
  person: string | null;
  source: (typeof SOURCES)[number] | null;
  side: (typeof SIDES)[number] | null;
  ticker: string | null;
  from: string | null;
  to: string | null;
};

const DAY = /^\d{4}-\d{2}-\d{2}$/;

function pick<T extends string>(value: string | null, options: readonly T[]): T | null {
  return value != null && (options as readonly string[]).includes(value) ? (value as T) : null;
}

/** Every filter lives in the link, so a filtered view can be shared or bookmarked. */
function useFilters(): [Filters, (change: Partial<Filters>) => void] {
  const [params, setParams] = useSearchParams();
  const filters: Filters = {
    scope: pick(params.get("scope"), SCOPES) ?? "watched",
    assets: params.get("assets") === "all" ? "all" : "equities",
    person: params.get("person") || null,
    source: pick(params.get("source"), SOURCES),
    side: pick(params.get("side"), SIDES),
    ticker: params.get("ticker")?.trim().toUpperCase() || null,
    from: DAY.test(params.get("from") ?? "") ? params.get("from") : null,
    to: DAY.test(params.get("to") ?? "") ? params.get("to") : null,
  };
  // Built from the address itself: two quick changes in a row must both stay, even before a re-render.
  const change = (next: Partial<Filters>) => setParams(() => {
    const updated = new URLSearchParams(window.location.search);
    for (const [name, value] of Object.entries(next)) {
      if (value == null || value === "" || (name === "scope" && value === "watched")) updated.delete(name);
      else updated.set(name, String(value));
    }
    return updated;
  }, { replace: true });
  return [filters, change];
}

function PersonChoice({ value, choose, people }: { value: string | null; choose: (key: string | null) => void; people: NotablePerson[] }) {
  const { t } = useTranslation();
  const groups = useMemo(() => {
    const byCategory = new Map<string, NotablePerson[]>();
    for (const person of people) {
      const group = person.category ?? person.source;
      byCategory.set(group, [...(byCategory.get(group) ?? []), person]);
    }
    const order = [...CATEGORY_ORDER, "sec", "house", "oge"];
    return [...byCategory.entries()].sort(([a], [b]) => order.indexOf(a) - order.indexOf(b));
  }, [people]);
  const known = !value || people.some((person) => person.key === value);
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
      {t("ui.notable.filter.person")}
      <select className={cn(selectStyle, "max-w-48")} value={value ?? ""} onChange={(event) => choose(event.target.value || null)} data-filter="person">
        <option value="">{t("ui.notable.filter.everyone")}</option>
        {!known && <option value={value!}>{value}</option>}
        {groups.map(([group, items]) => (
          <optgroup key={group} label={t(`ui.notable.category.${group}`)}>
            {items.map((person) => <option key={person.key} value={person.key}>{personName(person)}{person.district ? ` (${person.district})` : ""}</option>)}
          </optgroup>
        ))}
      </select>
    </label>
  );
}

function TickerChoice({ value, choose }: { value: string | null; choose: (value: string | null) => void }) {
  const { t } = useTranslation();
  const [text, setText] = useState(value ?? "");
  useEffect(() => setText(value ?? ""), [value]);
  const apply = () => choose(text.trim().toUpperCase() || null);
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
      {t("ui.notable.filter.ticker")}
      <span className="relative">
        <input value={text} onChange={(event) => setText(event.target.value)} onBlur={apply}
          onKeyDown={(event) => { if (event.key === "Enter") apply(); }}
          placeholder={t("ui.notable.filter.ticker_placeholder")} aria-label={t("ui.notable.filter.ticker")}
          className={cn(selectStyle, "w-24 pr-6 uppercase placeholder:normal-case")} data-filter="ticker" />
        {value && (
          <button type="button" aria-label={t("ui.notable.filter.clear_ticker")} onClick={() => choose(null)}
            className="absolute top-1/2 right-1.5 -translate-y-1/2 text-muted-foreground hover:text-foreground">
            <X className="size-3" />
          </button>
        )}
      </span>
    </label>
  );
}

function DateChoice({ from, to, choose }: { from: string | null; to: string | null; choose: (from: string | null, to: string | null) => void }) {
  const { t } = useTranslation();
  const label = from || to
    ? t("ui.notable.filter.dates_value", { from: from ? formatDay(from, { year: true }) : "…", to: to ? formatDay(to, { year: true }) : "…" })
    : t("ui.notable.filter.dates_any");
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" data-filter="dates" className={cn("inline-flex h-8 items-center gap-1 rounded-md border bg-card px-2.5 text-xs hover:bg-accent", (from || to) && "border-primary/60")}>
          <CalendarRange className="size-3.5" />{label}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 space-y-2.5 text-xs">
        <p className="font-medium">{t("ui.notable.filter.dates_title")}</p>
        {(["from", "to"] as const).map((edge) => (
          <label key={edge} className="flex items-center justify-between gap-2 text-muted-foreground">
            {t(`ui.notable.filter.${edge}`)}
            <input type="date" className={cn(selectStyle, "w-40")} value={(edge === "from" ? from : to) ?? ""}
              onChange={(event) => edge === "from" ? choose(event.target.value || null, to) : choose(from, event.target.value || null)} />
          </label>
        ))}
        <div className="flex flex-wrap gap-1.5 border-t pt-2">
          {[30, 90, 365].map((days) => (
            <Button key={days} size="xs" variant="outline" onClick={() => {
              const start = new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
              choose(start, null);
            }}>{t("ui.notable.filter.last_days", { count: days })}</Button>
          ))}
          {(from || to) && <Button size="xs" variant="ghost" onClick={() => choose(null, null)}>{t("ui.notable.filter.clear_dates")}</Button>}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Followed people's latest trades: SEC Form 4, House reports and OGE 278-T in one timeline. */
export function NotablePage() {
  const { t } = useTranslation();
  const [filters, change] = useFilters();
  const followed = useNotablePeople();
  const directory = useNotablePeople("all", filters.scope === "all");
  const people = (filters.scope === "all" ? directory.data?.items : followed.data?.items) ?? [];
  const [cursors, setCursors] = useState<string[]>([""]);
  const signature = JSON.stringify(filters);
  useEffect(() => setCursors([""]), [signature]);
  const cursor = cursors[cursors.length - 1];
  const query = {
    scope: filters.scope,
    assets: filters.assets,
    ...(filters.person ? { person: filters.person } : {}),
    ...(filters.source ? { source: filters.source } : {}),
    ...(filters.side ? { direction: filters.side } : {}),
    ...(filters.ticker ? { ticker: filters.ticker } : {}),
    ...(filters.from ? { published_from: filters.from } : {}),
    ...(filters.to ? { published_to: filters.to } : {}),
    limit: PAGE,
    ...(cursor ? { cursor } : {}),
  };
  const trades = useQuery({
    queryKey: ["notable-reports", query, NOTABLE_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/reports", { params: { query }, signal })),
    placeholderData: (previous) => previous,
  });
  const data = trades.data;
  const filtered = !!(filters.person || filters.source || filters.side || filters.ticker || filters.from || filters.to);
  const first = (cursors.length - 1) * PAGE;
  const nothingFollowed = filters.scope === "watched" && followed.data && followed.data.items.length === 0;

  const toolbar = (
    <>
      <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={filters.scope} aria-label={t("ui.notable.filter.scope")} data-filter="scope"
        onValueChange={(value) => value && change({ scope: value as Filters["scope"], person: null })}>
        <ToggleGroupItem value="watched" className="px-2.5" title={t("ui.notable.filter.watched_tip")}>
          {t("ui.notable.filter.watched")} {followed.data && <span className="text-muted-foreground tabular-nums">{followed.data.items.length}</span>}
        </ToggleGroupItem>
        <ToggleGroupItem value="all" className="px-2.5" title={t("ui.notable.filter.all_tip")}>{t("ui.notable.filter.all")}</ToggleGroupItem>
      </ToggleGroup>
      <AssetChoice value={filters.assets} choose={(assets) => change({ assets })} />
      <PersonChoice value={filters.person} choose={(person) => change({ person })} people={people} />
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {t("ui.notable.filter.source")}
        <select className={selectStyle} value={filters.source ?? ""} data-filter="source"
          onChange={(event) => change({ source: (event.target.value || null) as Filters["source"] })}>
          <option value="">{t("ui.notable.filter.any_source")}</option>
          {SOURCES.map((source) => <option key={source} value={source}>{t(`ui.notable.source.${source}`)}</option>)}
        </select>
      </label>
      <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={filters.side ?? "any"} aria-label={t("ui.notable.filter.side")} data-filter="side"
        onValueChange={(value) => value && change({ side: value === "any" ? null : value as Filters["side"] })}>
        <ToggleGroupItem value="any" className="px-2.5">{t("ui.notable.filter.any_side")}</ToggleGroupItem>
        <ToggleGroupItem value="buy" className="px-2.5">{t("ui.notable.side.buy")}</ToggleGroupItem>
        <ToggleGroupItem value="sell" className="px-2.5">{t("ui.notable.side.sell")}</ToggleGroupItem>
      </ToggleGroup>
      <TickerChoice value={filters.ticker} choose={(ticker) => change({ ticker })} />
      <DateChoice from={filters.from} to={filters.to} choose={(from, to) => change({ from, to })} />
      {filtered && (
        <Button size="xs" variant="ghost" onClick={() => change({ person: null, source: null, side: null, ticker: null, from: null, to: null })}>
          <X />{t("ui.notable.filter.clear")}
        </Button>
      )}
      <NControl />
      <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums" data-trade-total>
        {trades.isFetching && <LoaderCircle className="size-3.5 motion-safe:animate-spin" />}
        {data && t("ui.notable.group.total", { count: data.total, formatted: formatNumber(data.total, 0) })}
      </span>
    </>
  );

  const pager = data && data.total > PAGE ? (
    <div className="flex items-center justify-end gap-2 pt-2 text-xs text-muted-foreground" data-pager>
      <span className="tabular-nums">{t("ui.notable.group.page_range", { start: formatNumber(first + 1, 0), end: formatNumber(first + data.items.length, 0), total: formatNumber(data.total, 0) })}</span>
      <Button size="xs" variant="outline" disabled={cursors.length === 1 || trades.isFetching} onClick={() => setCursors((list) => list.slice(0, -1))} aria-label={t("ui.notable.previous")}>
        <ChevronLeft />{t("ui.notable.previous")}
      </Button>
      <Button size="xs" variant="outline" disabled={!data.next_cursor || trades.isFetching} onClick={() => setCursors((list) => [...list, data.next_cursor!])} aria-label={t("ui.notable.next")}>
        {t("ui.notable.next")}<ChevronRight />
      </Button>
    </div>
  ) : null;

  return (
    <div className="space-y-3">
      <Explain summary={t("ui.notable.disclaimer")}>
        <p className="mt-1">{t("ui.notable.disclaimer_more")}</p>
        <p className="mt-1">{t("ui.notable.window_rule")}</p>
        <p className="mt-1">{t("ui.notable.vice_president")}</p>
      </Explain>
      {trades.error && !data ? (
        <ErrorState error={trades.error} retry={() => void trades.refetch()} />
      ) : !data ? (
        <>
          <div className="flex min-h-8 flex-wrap items-center gap-x-3 gap-y-2">{toolbar}</div>
          <TableSkeleton rows={10} columns={7} />
        </>
      ) : (
        <NotableTable table="notable.trades" key={signature + cursor} groups={data.items} toolbar={toolbar} footer={pager} empty={
          nothingFollowed ? (
            <EmptyState title={t("ui.notable.empty_followed")} reason={t("ui.notable.empty_followed_reason")}
              action={<Button size="sm" variant="outline" onClick={() => openSettings("notable")}>{t("ui.notable.open_settings")}</Button>} />
          ) : (
            <EmptyState title={t("ui.notable.empty")} reason={t(filtered ? "ui.notable.empty_filtered" : "ui.notable.empty_reason")}
              action={filtered ? <Button size="sm" variant="outline" onClick={() => change({ person: null, source: null, side: null, ticker: null, from: null, to: null })}>{t("ui.notable.filter.clear")}</Button> : undefined} />
          )
        } />
      )}
    </div>
  );
}
