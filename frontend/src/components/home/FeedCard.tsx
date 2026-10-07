import { createContext, memo, useContext, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown } from "lucide-react";
import { tm } from "@/i18n";
import { codeKey, primarySummary, readableCase, rolesText, side } from "@/lib/insider";
import { formatCompact, formatDay, formatEt, formatLocal, formatMoney } from "@/lib/format";
import type { FeedGroup } from "@/lib/feedStream";
import type { Windows } from "@/lib/windows";
import { sourceContext } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WindowCell } from "@/components/insider/WindowCell";

type TraderGroup = FeedGroup["trader_groups"][number];
type Summary = TraderGroup["summary"][number];
const SHOWN = 3;

/** Before/after-n windows of the loaded cards, shared so each card reads its lines. */
export const FeedWindows = createContext<{ windows: Windows | null; n: number }>({ windows: null, n: 5 });

/**
 * Columns of a trade line; the list header uses the same template so values
 * line up: insider · action · shares · amount · traded · −n · +n.
 */
export const LINE_COLUMNS =
  "grid grid-cols-[minmax(0,1fr)_minmax(0,10.5rem)_4.25rem_5.75rem_6.5rem_3.75rem_3.75rem] items-baseline gap-x-3";
/** Company column, then the lines, then the filing time. */
export const CARD_COLUMNS = "grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_7.5rem] gap-x-3 min-[1400px]:grid-cols-[minmax(0,13rem)_minmax(0,1fr)_7.5rem] min-[1400px]:gap-x-4";

function Amount({ summary }: { summary: Summary }) {
  const { t } = useTranslation();
  const direction = side(summary);
  if (summary.known_amount == null || Number(summary.known_amount) === 0)
    return <span className="text-right text-muted-foreground">—</span>;
  const sign = direction === "buy" ? 1 : direction === "sell" ? -1 : 0;
  const text = formatMoney(sign ? sign * Number(summary.known_amount) : summary.known_amount, {
    signed: sign !== 0,
    // A filing that names no currency is shown in US dollars (Form 4 amounts are USD).
    currency: summary.currency && summary.currency !== "USD" ? summary.currency : undefined,
  });
  return (
    <span className={cn("truncate text-right tabular-nums", direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : "")}>
      {text}
      {summary.missing_price_rows > 0 && (
        <Tooltip>
          <TooltipTrigger className="text-muted-foreground">*</TooltipTrigger>
          <TooltipContent>{t("ui.feed.partial_amount")}</TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

function Action({ summary, others }: { summary: Summary; others: Summary[] }) {
  const { t } = useTranslation();
  const direction = side(summary);
  const label = t(codeKey(summary), { defaultValue: tm(summary.kind) });
  return (
    <span className="flex min-w-0 items-baseline gap-1">
      <span className={cn("truncate font-medium", direction === "buy" && "text-up", direction === "sell" && "text-down")}>
        {summary.table === "II" ? t("ui.feed.derivative", { action: label }) : label}
      </span>
      {others.length > 0 && (
        <Tooltip>
          <TooltipTrigger className="shrink-0 rounded bg-secondary px-1 text-[11px] text-muted-foreground tabular-nums">
            +{others.map((other) => other.code ?? "?").join("")}
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            {t("ui.feed.also")}{" "}
            {others
              .map((other) => `${t(codeKey(other), { defaultValue: tm(other.kind) })}${other.shares != null ? ` ${t("ui.feed.shares", { shares: formatCompact(other.shares) })}` : ""}`)
              .join(" · ")}
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

/** One owner's line: who (role), what, how many, how much, when, and the price before/after. */
function TraderLine({ trader, anomalies, group }: { trader: TraderGroup; anomalies: Map<string, string>; group: FeedGroup }) {
  const { t } = useTranslation();
  const location = useLocation();
  const { windows, n } = useContext(FeedWindows);
  const main = primarySummary(trader.summary);
  const others = trader.summary.filter((summary) => summary !== main);
  const anomaly = trader.transaction_date ? anomalies.get(trader.transaction_date) : undefined;
  const [first, ...rest] = trader.owners;
  const key = { ticker: group.ticker, date: anomaly ? null : trader.transaction_date };
  const window = windows?.get(key);
  const ticker = windows?.ticker(group.ticker);
  return (
    <li className={cn(LINE_COLUMNS, "leading-6")}>
      <span className="flex min-w-0 items-baseline gap-1.5 overflow-hidden">
        {first ? (
          <Link to={`/people/${first.id}`} state={sourceContext(location)} className="min-w-0 shrink truncate font-medium hover:underline"
            title={[readableCase(first.name), first.roles.length ? rolesText(first) : ""].filter(Boolean).join(" · ")}>
            {readableCase(first.name)}
          </Link>
        ) : (
          <span className="truncate text-muted-foreground">{t("ui.feed.owner_unknown")}</span>
        )}
        {rest.length > 0 && (
          <Tooltip>
            <TooltipTrigger className="shrink-0 text-xs text-muted-foreground tabular-nums" aria-label={t("ui.feed.more_owners", { count: rest.length })}>+{rest.length}</TooltipTrigger>
            <TooltipContent>{t("ui.feed.more_owners", { count: rest.length })}: {rest.map((owner) => readableCase(owner.name)).join(", ")}</TooltipContent>
          </Tooltip>
        )}
        {first && first.roles.length > 0 && (
          <span className="min-w-0 shrink-[3] truncate text-xs text-muted-foreground max-[1400px]:hidden">{rolesText(first)}</span>
        )}
      </span>
      {main ? <Action summary={main} others={others} /> : <span />}
      <span className="text-right tabular-nums">{main?.shares != null ? formatCompact(main.shares) : "—"}</span>
      {main ? <Amount summary={main} /> : <span />}
      <span className="flex items-baseline justify-end gap-1 whitespace-nowrap text-muted-foreground tabular-nums">
        {anomaly ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge variant="outline" className="h-5 border-warn/40 px-1 text-[11px] text-warn">{t("ui.feed.date_anomaly")}</Badge>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">{anomaly}</TooltipContent>
          </Tooltip>
        ) : (
          formatDay(trader.transaction_date)
        )}
      </span>
      <WindowCell change={window?.before} ticker={ticker} side="before" n={n} />
      <WindowCell change={window?.after} ticker={ticker} side="after" n={n} />
    </li>
  );
}

function accent(group: FeedGroup) {
  const sides = new Set(group.summary.map(side).filter(Boolean));
  if (sides.has("buy") && !sides.has("sell")) return "border-l-up";
  if (sides.has("sell") && !sides.has("buy")) return "border-l-down";
  return "border-l-neutral-mark";
}

/** A company's filings accepted on one US Eastern day, one compact line per owner and action. */
export const FeedCard = memo(function FeedCard({ group, mark }: { group: FeedGroup; mark: "new" | "updated" | null }) {
  const { t } = useTranslation();
  const location = useLocation();
  const [expanded, setExpanded] = useState(false);
  const anomalies = new Map<string, string>();
  for (const row of group.transactions) {
    if (row.date_anomaly && row.transaction_date)
      anomalies.set(row.transaction_date, t("ui.feed.date_anomaly_detail", {
        trade: formatDay(row.date_anomaly.transaction_date, { year: true }),
        accepted: formatDay(row.date_anomaly.accepted_date, { year: true }),
      }));
  }
  const traders = group.trader_groups;
  const hidden = traders.length - SHOWN;
  const company = readableCase(group.company ?? group.ticker ?? group.issuer_id);
  return (
    <article
      data-group-id={group.id}
      className={cn(CARD_COLUMNS, "rounded-md border border-l-[3px] bg-card px-3 py-1 shadow-xs", accent(group))}
    >
      <div className="flex min-w-0 items-baseline gap-1.5 leading-6">
        <Link
          to={`/companies/${group.issuer_id}`}
          state={sourceContext(location)}
          className="flex min-w-0 items-baseline gap-1.5 hover:underline"
          title={company}
        >
          <span className="shrink-0 rounded bg-secondary px-1 text-xs font-semibold tracking-wide tabular-nums">
            {group.ticker ?? group.issuer_ticker_raw ?? "—"}
          </span>
          <span className="truncate font-semibold">{company}</span>
        </Link>
        {group.amendment_count > 0 && (
          <Badge variant="outline" className="h-5 shrink-0 px-1 text-[11px]">{t("ui.feed.amendment")}</Badge>
        )}
        <AnimatePresence>
          {mark && (
            <motion.span className="shrink-0" initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
              <Badge className="h-5 bg-primary px-1.5 text-[11px] text-primary-foreground">{t(`ui.feed.mark.${mark}`)}</Badge>
            </motion.span>
          )}
        </AnimatePresence>
      </div>
      <div className="min-w-0">
        <ul>
          {traders.slice(0, SHOWN).map((trader) => (
            <TraderLine key={trader.id} trader={trader} anomalies={anomalies} group={group} />
          ))}
        </ul>
        <AnimatePresence initial={false}>
          {expanded && hidden > 0 && (
            <motion.ul
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.22, ease: "easeOut" }}
              className="overflow-hidden"
            >
              {traders.slice(SHOWN).map((trader) => (
                <TraderLine key={trader.id} trader={trader} anomalies={anomalies} group={group} />
              ))}
            </motion.ul>
          )}
        </AnimatePresence>
        {(hidden > 0 || group.next_trader_cursor) && (
          <div className="flex items-center gap-3 pb-0.5 text-xs leading-5">
            {hidden > 0 && (
              <button onClick={() => setExpanded((open) => !open)} className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground" aria-expanded={expanded}>
                <ChevronDown className={cn("size-3.5 transition-transform duration-200", expanded && "rotate-180")} />
                {expanded ? t("ui.feed.show_less") : t("ui.feed.show_more", { count: hidden })}
              </button>
            )}
            {group.next_trader_cursor && (
              <Link to={`/companies/${group.issuer_id}`} state={sourceContext(location)} className="text-muted-foreground hover:text-foreground hover:underline">
                {t("ui.feed.all_at_company")}
              </Link>
            )}
          </div>
        )}
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="cursor-default text-right text-xs leading-6 whitespace-nowrap text-muted-foreground tabular-nums">
            {formatEt(group.accepted_at)}
          </span>
        </TooltipTrigger>
        <TooltipContent>{t("ui.feed.filed_tip", { time: formatLocal(group.accepted_at) })}</TooltipContent>
      </Tooltip>
    </article>
  );
});
