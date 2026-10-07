import { memo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown } from "lucide-react";
import { tm } from "@/i18n";
import { codeKey, primarySummary, readableCase, rolesText, side } from "@/lib/insider";
import { formatCompact, formatDay, formatEt, formatLocal, formatMoney } from "@/lib/format";
import type { FeedGroup } from "@/lib/feedStream";
import { sourceContext } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type TraderGroup = FeedGroup["trader_groups"][number];
type Summary = TraderGroup["summary"][number];
const SHOWN = 3;

function Amount({ summary }: { summary: Summary }) {
  const { t } = useTranslation();
  const direction = side(summary);
  if (summary.known_amount == null || Number(summary.known_amount) === 0) return null;
  const sign = direction === "buy" ? 1 : direction === "sell" ? -1 : 0;
  return (
    <span className={cn("tabular-nums", direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : "")}>
      {formatMoney(sign ? sign * Number(summary.known_amount) : summary.known_amount, { signed: sign !== 0, currency: summary.currency })}
      {summary.missing_price_rows > 0 && (
        <span className="text-muted-foreground"> {t("ui.feed.partial_amount")}</span>
      )}
    </span>
  );
}

function Action({ summary }: { summary: Summary }) {
  const { t } = useTranslation();
  const direction = side(summary);
  const label = t(codeKey(summary), { defaultValue: tm(summary.kind) });
  return (
    <span
      className={cn(
        "font-medium",
        direction === "buy" && "text-up",
        direction === "sell" && "text-down",
      )}
    >
      {summary.table === "II" ? t("ui.feed.derivative", { action: label }) : label}
    </span>
  );
}

/** One owner's line: who, role, what, how many, how much, when traded. */
function TraderLine({ trader, anomalies }: { trader: TraderGroup; anomalies: Map<string, string> }) {
  const { t } = useTranslation();
  const location = useLocation();
  const main = primarySummary(trader.summary);
  const others = trader.summary.filter((summary) => summary !== main);
  const anomaly = trader.transaction_date ? anomalies.get(trader.transaction_date) : undefined;
  const [first, ...rest] = trader.owners;
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 leading-6">
      {first ? (
        <Link to={`/people/${first.id}`} state={sourceContext(location)} className="font-medium hover:underline">
          {readableCase(first.name)}
        </Link>
      ) : (
        <span className="text-muted-foreground">{t("ui.feed.owner_unknown")}</span>
      )}
      {rest.length > 0 && (
        <Tooltip>
          <TooltipTrigger className="text-muted-foreground">{t("ui.feed.more_owners", { count: rest.length })}</TooltipTrigger>
          <TooltipContent>{rest.map((owner) => readableCase(owner.name)).join(", ")}</TooltipContent>
        </Tooltip>
      )}
      {first && first.roles.length > 0 && <span className="text-muted-foreground">{t("ui.feed.roles", { roles: rolesText(first) })}</span>}
      {main && (
        <>
          <span className="text-muted-foreground" aria-hidden>—</span>
          <Action summary={main} />
          {main.shares != null && (
            <span className="tabular-nums">{t("ui.feed.shares", { shares: formatCompact(main.shares) })}</span>
          )}
          <Amount summary={main} />
        </>
      )}
      {trader.transaction_date && (
        <span className="text-muted-foreground tabular-nums">· {t("ui.feed.traded", { day: formatDay(trader.transaction_date) })}</span>
      )}
      {anomaly && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" className="border-warn/40 text-warn">{t("ui.feed.date_anomaly")}</Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{anomaly}</TooltipContent>
        </Tooltip>
      )}
      {others.length > 0 && (
        <span className="w-full pl-0 text-xs text-muted-foreground">
          {t("ui.feed.also")}{" "}
          {others.map((summary, index) => (
            <span key={index}>
              {index > 0 && " · "}
              {t(codeKey(summary), { defaultValue: tm(summary.kind) })}
              {summary.shares != null && ` ${t("ui.feed.shares", { shares: formatCompact(summary.shares) })}`}
            </span>
          ))}
        </span>
      )}
    </li>
  );
}

function accent(group: FeedGroup) {
  const sides = new Set(group.summary.map(side).filter(Boolean));
  if (sides.has("buy") && !sides.has("sell")) return "border-l-up";
  if (sides.has("sell") && !sides.has("buy")) return "border-l-down";
  return "border-l-neutral-mark";
}

/** A company's filings accepted on one US Eastern day, one line per owner and action. */
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
      className={cn("rounded-lg border border-l-[3px] bg-card px-4 py-3 shadow-xs", accent(group))}
    >
      <header className="flex items-baseline gap-2">
        <Link
          to={`/companies/${group.issuer_id}`}
          state={sourceContext(location)}
          className="flex min-w-0 items-baseline gap-2 hover:underline"
        >
          <span className="shrink-0 rounded bg-secondary px-1.5 py-px text-xs font-semibold tracking-wide tabular-nums">
            {group.ticker ?? group.issuer_ticker_raw ?? "—"}
          </span>
          <span className="truncate font-semibold">{company}</span>
        </Link>
        {group.amendment_count > 0 && <Badge variant="outline">{t("ui.feed.amendment")}</Badge>}
        <AnimatePresence>
          {mark && (
            <motion.span initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
              <Badge className="bg-primary text-primary-foreground">{t(`ui.feed.mark.${mark}`)}</Badge>
            </motion.span>
          )}
        </AnimatePresence>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="ml-auto shrink-0 cursor-default text-xs text-muted-foreground tabular-nums">
              {t("ui.feed.filed", { time: formatEt(group.accepted_at) })}
            </span>
          </TooltipTrigger>
          <TooltipContent>{t("ui.time.local", { time: formatLocal(group.accepted_at) })}</TooltipContent>
        </Tooltip>
      </header>
      <ul className="mt-1.5 space-y-0.5">
        {traders.slice(0, SHOWN).map((trader) => (
          <TraderLine key={trader.id} trader={trader} anomalies={anomalies} />
        ))}
      </ul>
      <AnimatePresence initial={false}>
        {expanded && hidden > 0 && (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            className="space-y-0.5 overflow-hidden"
          >
            {traders.slice(SHOWN).map((trader) => (
              <TraderLine key={trader.id} trader={trader} anomalies={anomalies} />
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
      {(hidden > 0 || group.next_trader_cursor) && (
        <footer className="mt-1 flex items-center gap-3 text-xs">
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
        </footer>
      )}
    </article>
  );
});
