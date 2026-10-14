import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ExternalLink, FileText } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { formatDay, formatEt, formatPrice } from "@/lib/format";
import { NOTABLE_DEPENDENT, amountText, personHref, personName, roleLabel } from "@/lib/notable";
import { sourceContext, sourceHref } from "@/lib/navigation";
import { useInsiderN, useTradeWindows, type WindowItem } from "@/lib/windows";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { InfoTip } from "@/components/ui/info";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { NControl } from "@/components/insider/NControl";
import { BandLegend, PriceChart, type Band } from "@/components/insider/PriceChart";
import { WindowCell } from "@/components/insider/WindowCell";
import { FollowStar } from "@/components/notable/FollowStar";
import { ReadingMarks } from "@/components/notable/NotableTable";

const DAY_MS = 86_400_000;

function shift(day: string, days: number) {
  return new Date(Date.parse(`${day}T12:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
}

function Fact({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="truncate tabular-nums">{children}</dd>
    </div>
  );
}

/** The shaded windows of the chart: before and after n from the trade, after n from publication. */
function windowBands(trade: WindowItem | undefined, published: WindowItem | undefined, t: (key: string, options?: Record<string, unknown>) => string, n: number): Band[] {
  const bands: Band[] = [];
  if (trade?.before?.start_date && trade.before.end_date) bands.push({ start: trade.before.start_date, end: trade.before.end_date, tone: "before", label: t("ui.notable.trade.band.before", { n }) });
  if (trade?.after?.start_date && trade.after.end_date) bands.push({ start: trade.after.start_date, end: trade.after.end_date, tone: "after", label: t("ui.notable.trade.band.after", { n }) });
  if (published?.after?.start_date && published.after.end_date) bands.push({ start: published.after.start_date, end: published.after.end_date, tone: "published", label: t("ui.notable.trade.band.published", { n }) });
  return bands;
}

/**
 * One House or OGE trade, laid out like the SEC trade page: who and what in one
 * sentence, the reported facts, the candles with the trade and publication days
 * and the n-session windows shaded, and the report's own row with the original.
 */
export function NotableTradePage() {
  const { t } = useTranslation();
  const location = useLocation();
  const { id = "" } = useParams();
  const { n } = useInsiderN();
  const query = useQuery({
    queryKey: ["notable-trade", id, NOTABLE_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/trades/{transaction_id}", { params: { path: { transaction_id: id } }, signal })),
    retry: false,
  });
  const detail = query.data;
  const trade = detail?.trade;
  const keys = useMemo(() => trade?.price_ticker ? [
    ...(trade.transaction_date ? [{ ticker: trade.price_ticker, date: trade.transaction_date, issuer_id: trade.issuer_id }] : []),
    ...(trade.published_basis ? [{ ticker: trade.price_ticker, date: trade.published_basis, issuer_id: trade.issuer_id }] : []),
  ] : [], [trade]);
  const windows = useTradeWindows(keys, n, !!trade, true);
  const tickerState = windows.ticker(trade?.price_ticker);
  const tradeWindow = trade?.price_ticker && trade.transaction_date ? windows.get({ ticker: trade.price_ticker, date: trade.transaction_date }) : undefined;
  const publishedWindow = trade?.price_ticker && trade.published_basis ? windows.get({ ticker: trade.price_ticker, date: trade.published_basis }) : undefined;
  const days = [trade?.transaction_date, trade?.published_basis, trade?.published, tradeWindow?.before?.start_date, publishedWindow?.after?.end_date, tradeWindow?.after?.end_date]
    .filter((day): day is string => !!day).sort();
  const span = days.length ? { start: shift(days[0], -10), end: shift(days[days.length - 1], 10) } : null;
  const bars = useQuery({
    queryKey: ["insider-bars", trade?.price_ticker, span?.start, span?.end, tickerState?.status, tickerState?.fetched_at],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/bars", { params: { query: { ticker: trade!.price_ticker!, start_date: span!.start, end_date: span!.end } }, signal })),
    enabled: !!trade?.price_ticker && !!span,
    placeholderData: (previous) => previous,
  });
  const bands = useMemo(() => windowBands(tradeWindow, publishedWindow, t, n), [tradeWindow, publishedWindow, t, n]);
  const lines = useMemo(() => [
    ...(trade?.transaction_date ? [{ date: trade.transaction_date, label: t("ui.detail.line.trade") }] : []),
    ...(trade?.published ? [{ date: trade.published, label: t("ui.notable.trade.line.published") }] : []),
  ], [trade, t]);

  const back = (
    <Link to={sourceHref(location.state, "/notable")} state={(location.state as { fromState?: unknown } | null)?.fromState} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
      <ArrowLeft className="size-3.5" />{t("ui.entity.back")}
    </Link>
  );
  if (query.error)
    return <div className="space-y-3">{back}<ErrorState error={query.error} hint={t("ui.notable.trade.missing_hint")} /></div>;
  if (!detail || !trade)
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );

  const name = personName(trade.person);
  const role = roleLabel(trade.person, t);
  const side = trade.side === "sell" && trade.partial ? t("ui.notable.side.partial_sale") : t(`ui.notable.side.${trade.side}`);
  const amount = amountText(trade, t) ?? "—";
  const asset = trade.asset_name || trade.ticker || "—";
  const report = detail.report;
  const kind = trade.asset_kind === "option"
    ? [t("ui.notable.option"), detail.option_type && t(`ui.notable.trade.option.${detail.option_type}`, { defaultValue: detail.option_type }),
      detail.strike_price && t("ui.notable.trade.strike", { price: formatPrice(detail.strike_price) }),
      detail.expiration_date && t("ui.notable.trade.expires", { day: formatDay(detail.expiration_date, { year: true }) })].filter(Boolean).join(" · ")
    : t(`ui.notable.trade.kind.${trade.asset_kind}`);
  const priceTitle = trade.price_ticker ? t("ui.notable.trade.price_title", { ticker: trade.price_ticker, n }) : t("ui.notable.trade.price_none_title");
  return (
    <div className="space-y-4">
      <div>
        {back}
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          {trade.issuer_id && trade.asset_kind !== "bond" && trade.asset_kind !== "fund" ? (
            <Link to={`/companies/${trade.issuer_id}`} state={sourceContext(location)} className="inline-flex min-w-0 items-baseline gap-1.5 text-lg font-semibold hover:underline">
              {trade.ticker && <span className="rounded bg-secondary px-1.5 text-sm tabular-nums">{trade.ticker}</span>}
              <span className="truncate" title={asset}>{asset}</span>
            </Link>
          ) : (
            <span className="inline-flex min-w-0 items-baseline gap-1.5 text-lg font-semibold">
              {trade.ticker && <span className="rounded bg-secondary px-1.5 text-sm tabular-nums">{trade.ticker}</span>}
              <span className="truncate" title={asset}>{asset}</span>
            </span>
          )}
          <span className="text-muted-foreground">·</span>
          <span className="inline-flex items-baseline gap-0.5">
            <FollowStar personKey={trade.person.key} name={name} size="xs" className="self-center" />
            <Link to={personHref(trade.person.key)} state={sourceContext(location)} className="font-medium hover:underline">{name}</Link>
            {role && <span className="ml-1 text-sm text-muted-foreground">{role}</span>}
          </span>
          <span className="inline-flex gap-1 self-center"><ReadingMarks trade={trade} /></span>
        </div>
      </div>

      <section className="rounded-lg border bg-card px-4 py-3" data-trade-facts>
        <p className={cn("text-base font-medium", trade.side === "buy" ? "text-up" : trade.side === "sell" ? "text-down" : "")}>
          {t("ui.notable.trade.sentence", {
            person: name, side, asset: trade.ticker ?? asset, amount,
            traded: trade.transaction_date ? formatDay(trade.transaction_date, { year: true }) : t("ui.notable.trade.no_date"),
            published: formatDay(trade.published, { year: true }),
          })}
        </p>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4 lg:grid-cols-6">
          <Fact label={t("ui.notable.column.side")}>
            <span className={cn(trade.side === "buy" ? "text-up" : trade.side === "sell" ? "text-down" : "text-muted-foreground")}>{side}</span>
            {trade.amendment && <Badge variant="outline" className="ml-1 h-4 px-1 text-xs" title={t("ui.notable.amended_tip")}>{t("ui.notable.amended")}</Badge>}
          </Fact>
          <Fact label={t("ui.notable.column.traded")}>{trade.transaction_date ? formatDay(trade.transaction_date, { year: true }) : "—"}</Fact>
          <Fact label={t("ui.notable.trade.published")}>{formatDay(trade.published, { year: true })}</Fact>
          <Fact label={t("ui.detail.lag")}>{trade.lag_days == null ? "—" : t("ui.detail.lag_days", { count: trade.lag_days })}</Fact>
          <Fact label={t("ui.notable.column.amount")}><span title={t("ui.notable.range_tip")}>{amount}</span></Fact>
          <Fact label={t("ui.notable.column.owner")}>{trade.owner ? t(`ui.notable.owner.${trade.owner}`, { defaultValue: trade.owner }) : "—"}</Fact>
          <Fact label={t("ui.notable.trade.kind_label")} className="col-span-2"><span title={kind}>{kind}</span></Fact>
          <Fact label={t("ui.notable.trade.code")}>
            {trade.ticker ?? "—"}
            {trade.ticker && detail.ticker_origin && detail.ticker_origin !== "reported" && (
              <span className="ml-1 text-xs text-muted-foreground">{t(`ui.notable.trade.origin.${detail.ticker_origin}`, { defaultValue: detail.ticker_origin })}</span>
            )}
          </Fact>
          {detail.notification_date && <Fact label={t("ui.notable.trade.notified")}>{formatDay(detail.notification_date, { year: true })}</Fact>}
          <Fact label={t("ui.notable.trade.report")} className="col-span-2">
            <a href={report.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline" data-original-link>
              {t(`ui.notable.trade.report_link.${trade.source}`, { day: formatDay(report.report_date ?? report.filed_date, { year: true }) })}
              <ExternalLink className="size-3" />
            </a>
            {report.file_url && <a href={report.file_url} target="_blank" rel="noreferrer" className="ml-2 text-xs text-muted-foreground hover:text-foreground hover:underline">{t("ui.notable.saved_copy")}</a>}
            {report.superseded && <span className="ml-2 text-xs text-muted-foreground" title={t("ui.notable.superseded_tip")}>{t("ui.notable.superseded")}</span>}
          </Fact>
        </dl>
        {trade.review.length > 0 && (
          <p className="mt-2 text-xs text-warn" data-review-reasons>
            {t("ui.notable.review_tip")} {trade.review.map((reason) => t(`ui.notable.reason.${reason}`)).join(t("ui.notable.trade.reason_join"))}
          </p>
        )}
      </section>

      <section className="rounded-lg border bg-card px-4 py-3" data-trade-chart>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <h3 className="card-title inline-flex items-center gap-1.5">
            {priceTitle}
            <InfoTip>{t("ui.notable.trade.basis_note", { n })}</InfoTip>
          </h3>
          {trade.price_ticker && <span className="text-xs text-muted-foreground">{t("ui.notable.trade.unit")}</span>}
          {tickerState?.fetched_at && <span className="text-xs text-muted-foreground">{t("ui.entity.prices_as_of", { time: formatEt(tickerState.fetched_at) })}</span>}
          <NControl className="ml-auto" />
        </div>
        <div className="mt-2 grid gap-3 md:grid-cols-[minmax(0,1fr)_17rem]">
          <div className="min-w-0">
            {bars.data?.bars.length ? (
              <>
                <PriceChart bars={bars.data.bars} lines={lines} bands={bands} height={260} />
                <BandLegend bands={bands} className="mt-1 px-2" />
              </>
            ) : (
              <div className="grid h-[260px] place-items-center px-4 text-center text-sm text-muted-foreground" data-chart-empty>
                {!trade.price_ticker
                  ? t(`ui.notable.no_window.${trade.price_none ?? "no_ticker"}`)
                  : tickerState?.status === "unavailable"
                    ? t("ui.window.unavailable")
                    : t("ui.entity.chart_fetching", { ticker: trade.price_ticker })}
              </div>
            )}
          </div>
          <div className="space-y-3 text-sm">
            {([
              ["trade", tradeWindow, trade.transaction_date] as const,
              ["published", publishedWindow, trade.published_basis] as const,
            ]).map(([basis, item, day]) => (
              <div key={basis} className="rounded-md border px-3 py-2" data-basis={basis}>
                <p className="font-medium">{t(`ui.notable.trade.basis.${basis}`)}</p>
                <p className="text-xs text-muted-foreground">
                  {item?.anchor_date || day ? t("ui.detail.basis.baseline", { day: formatDay(item?.anchor_date ?? day, { year: true }) }) : t("ui.detail.basis.none")}
                </p>
                <div className="mt-1.5 grid grid-cols-2 gap-2">
                  {(["before", "after"] as const).map((side) => (
                    <div key={side}>
                      <p className="text-xs text-muted-foreground">{t(`ui.window.${side}_head`, { n })}</p>
                      {trade.price_ticker && day
                        ? <WindowCell className="text-base" side={side} basis={basis} n={n} ticker={tickerState} change={item?.[side]} />
                        : <span className="text-base text-muted-foreground" title={t(`ui.notable.no_window.${trade.price_none ?? "no_ticker"}`)}>—</span>}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="rounded-lg border bg-card px-4 py-3 text-sm" data-raw-row>
        <h3 className="mb-1.5 inline-flex items-center gap-1.5 text-sm font-semibold">
          <FileText className="size-3.5 text-muted-foreground" aria-hidden />
          {t("ui.notable.trade.raw_title")}
          <span className="text-xs font-normal text-muted-foreground">{t("ui.notable.trade.page_row", { page: detail.page, row: detail.row_number })}</span>
        </h3>
        <p className="rounded-md bg-muted/50 px-3 py-2 font-mono text-xs break-words whitespace-pre-wrap">{detail.raw_row}</p>
        {trade.ocr && <p className="mt-1.5 text-xs text-muted-foreground">{t("ui.notable.scanned_tip")}</p>}
      </section>
    </div>
  );
}
