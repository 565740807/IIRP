import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ExternalLink, LoaderCircle } from "lucide-react";
import i18n, { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatDay, formatEt, formatLocal, formatNumber, formatSignedRatio } from "@/lib/format";
import { actionLabel, plainKey, readableCase, rolesText, side, sideClass } from "@/lib/insider";
import { todayEt } from "@/lib/insiderPrefs";
import { useInsiderN } from "@/lib/windows";
import { sourceContext, sourceHref } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { NControl } from "@/components/insider/NControl";
import { PriceChart } from "@/components/insider/PriceChart";
import { money, price, type Trade } from "@/components/insider/TradeTable";
import { WindowCell } from "@/components/insider/WindowCell";
import { AmendmentReview } from "@/components/insider/AmendmentReview";

type Detail = Trade & {
  footnotes?: { id: string; text: string }[];
  source_url?: string | null;
  security_title?: string | null;
  amendments?: { id: string; action: string; original_event_id?: string | null }[];
};
type Basis = { baseline_date?: string | null; before_n?: string | null; after_n?: string | null; before_date?: string | null; after_date?: string | null; before_status?: string | null; after_status?: string | null };

/** The filing's index page on sec.gov. */
function filingUrl(row: Trade) {
  return `https://www.sec.gov/Archives/edgar/data/${Number(row.issuer_id)}/${row.accession.replace(/-/g, "")}/${row.accession}-index.html`;
}

/** One sentence in plain words: what this row is, how it is held, what is held afterwards. */
function sentence(row: Detail, t: (key: string, options?: Record<string, unknown>) => string, language: string) {
  const shares = formatNumber(row.shares, 0, language);
  const after = formatNumber(row.shares_after, 0, language);
  const at = row.price != null && side(row)
    ? t("ui.plain.at", { price: price(row.price, row.currency, language), amount: money(row.amount, row.currency, false, language) })
    : "";
  const parts = [t(plainKey(row), { shares, at, action: actionLabel(row, t), after })];
  if (row.direct_or_indirect === "D") parts.push(t("ui.plain.ownership.direct"));
  if (row.direct_or_indirect === "I")
    parts.push(row.nature_of_ownership ? t("ui.plain.ownership.indirect", { nature: row.nature_of_ownership }) : t("ui.plain.ownership.indirect_plain"));
  if (row.code && row.shares_after != null) parts.push(t("ui.plain.after", { after }));
  return parts.join(t("ui.plain.join"));
}

/** The server's window status as a cell change (pending, intraday, missing or a value). */
function change(basis: Basis | null | undefined, which: "before" | "after") {
  if (!basis?.baseline_date) return null;
  const start = which === "before" ? basis.before_date : basis.baseline_date;
  const end = which === "before" ? basis.baseline_date : basis.after_date;
  const status = which === "before" ? basis.before_status : basis.after_status;
  const later = (end ?? "") > (start ?? "") ? end : start;
  return {
    value: which === "before" ? basis.before_n : basis.after_n,
    start_date: start,
    end_date: end,
    status: status === "available" ? "available" : status === "not_yet_formed" ? (later === todayEt() ? "intraday" : "pending") : "missing",
  };
}

function Fact({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="truncate tabular-nums">{children}</dd>
    </div>
  );
}

export function TransactionPage() {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { n } = useInsiderN();
  const query = useQuery({
    queryKey: ["transaction", id, n],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/transactions/{transaction_id}", { params: { path: { transaction_id: id }, query: { n } }, signal })),
    placeholderData: (previous) => previous,
    refetchInterval: (state) => (["FETCHING", "NOT_FETCHED"].includes(String(state.state.data?.data.price_context.status)) ? 3000 : false),
  });
  const row = query.data?.data.transaction as Detail | undefined;
  const context = query.data?.data.price_context as (Schemas["PriceContext"] & { ticker?: string; identity?: string; bars?: { date: string; open: string; high: string; low: string; close: string }[]; accepted_at?: string | null }) | undefined;

  // No prices yet: ask once for this ticker (one request, 24-hour cache).
  const asked = useRef(false);
  useEffect(() => {
    if (!row || !context || context.status !== "NOT_FETCHED" || asked.current || !row.ticker || !row.transaction_date) return;
    asked.current = true;
    void unwrap(client.POST("/api/v1/insider/prices", { body: { n, items: [{ ticker: row.ticker, date: row.transaction_date, issuer_id: row.issuer_id }] } }))
      .then(() => queryClient.invalidateQueries({ queryKey: ["transaction", id] }))
      .catch(() => undefined);
  }, [row, context, n, id, queryClient]);

  const other = i18n.language === "zh" ? "en" : "zh";
  const otherT = useMemo(() => i18n.getFixedT(other), [other]);
  if (query.error) return <p className="text-sm text-destructive">{query.error.message}</p>;
  if (!row || !context)
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  const trade = context.transaction as Basis | null | undefined;
  const disclosure = context.disclosure as Basis | null | undefined;
  const pending = context.identity !== "confirmed" && !!context.ticker;
  const tickerState = {
    status: (context.status === "READY" ? "ready" : context.status === "FETCHING" ? "fetching" : context.status === "NOT_FETCHED" ? "not_fetched" : "unavailable") as "ready",
    reason: context.reason,
    confirmed: context.identity === "confirmed",
  };
  const acceptedDay = row.accepted_at ? new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date(row.accepted_at)) : null;
  const lines = [
    ...(row.transaction_date ? [{ date: row.transaction_date, label: t("ui.detail.line.trade") }] : []),
    ...(acceptedDay ? [{ date: acceptedDay, label: t("ui.detail.line.filed") }] : []),
  ];
  return (
    <div className="space-y-4">
      <div>
        <Link to={sourceHref(location.state, "/insiders")} state={(location.state as { fromState?: unknown } | null)?.fromState} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-3.5" />
          {t("ui.entity.back")}
        </Link>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <Link to={`/companies/${row.issuer_id}`} state={sourceContext(location)} className="inline-flex items-baseline gap-1.5 text-lg font-semibold hover:underline">
            {row.ticker && <span className="rounded bg-secondary px-1.5 text-sm tabular-nums">{row.ticker}</span>}
            {readableCase(row.issuer_name ?? row.issuer_id)}
          </Link>
          <span className="text-muted-foreground">·</span>
          {row.owners.map((owner) => (
            <span key={owner.id}>
              <Link to={`/people/${owner.id}`} state={sourceContext(location)} className="font-medium hover:underline">{readableCase(owner.name)}</Link>
              {owner.roles.length > 0 && <span className="ml-1 text-sm text-muted-foreground">{rolesText(owner)}</span>}
            </span>
          ))}
          {pending && <Badge variant="outline" title={t("ui.entity.pending_tip")}>{t("ui.entity.pending")}</Badge>}
        </div>
      </div>

      <section className="rounded-lg border bg-card px-4 py-3">
        <p className={cn("text-base font-medium", sideClass(row))}>{sentence(row, t, i18n.language)}</p>
        <p className="mt-0.5 text-sm text-muted-foreground" lang={other === "zh" ? "zh-CN" : "en"}>{sentence(row, otherT, other)}</p>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4 lg:grid-cols-6">
          <Fact label={t("ui.trades.action")}><span className={sideClass(row)}>{actionLabel(row, t)}</span></Fact>
          <Fact label={t("ui.trades.traded")}>
            {formatDay(row.transaction_date, { year: true })}
            {row.date_anomaly && <Badge variant="outline" className="ml-1.5 h-5 border-warn/40 px-1 text-[11px] text-warn" title={t("ui.feed.date_anomaly_detail", { trade: formatDay(row.date_anomaly.transaction_date, { year: true }), accepted: formatDay(row.date_anomaly.accepted_date, { year: true }) })}>{t("ui.feed.date_anomaly")}</Badge>}
          </Fact>
          <Fact label={t("ui.detail.accepted")}><span title={t("ui.time.local", { time: formatLocal(row.accepted_at) })}>{formatEt(row.accepted_at)}</span></Fact>
          <Fact label={t("ui.detail.lag")}>{context.disclosure_lag_calendar_days == null ? "—" : t("ui.detail.lag_days", { count: context.disclosure_lag_calendar_days })}</Fact>
          <Fact label={t("ui.trades.shares")}>{formatNumber(row.shares, 0)}</Fact>
          <Fact label={t("ui.trades.price")}>{row.price == null ? "—" : price(row.price, row.currency)}</Fact>
          <Fact label={t("ui.trades.amount")}><span className={sideClass(row)}>{row.amount == null ? "—" : money(row.amount, row.currency)}</span></Fact>
          <Fact label={t("ui.trades.after")}>
            {row.shares_after == null ? "—" : formatNumber(row.shares_after, 0)}
            {row.holding_change != null && <span className="ml-1.5 text-xs text-muted-foreground">{formatSignedRatio(row.holding_change)}</span>}
          </Fact>
          <Fact label={t("ui.trades.ownership")}>
            {row.direct_or_indirect === "D" ? t("ui.trades.direct") : row.direct_or_indirect === "I" ? (row.nature_of_ownership ? t("ui.trades.indirect_by", { nature: row.nature_of_ownership }) : t("ui.trades.indirect")) : "—"}
          </Fact>
          <Fact label={t("ui.trades.plan")}>
            {["1", "true"].includes((row.raw_10b5_1_flag ?? "").toLowerCase()) ? t("ui.trades.plan_yes") : ["0", "false"].includes((row.raw_10b5_1_flag ?? "").toLowerCase()) ? t("ui.trades.plan_no") : "—"}
          </Fact>
          <Fact label={t("ui.detail.security")} className="col-span-2">{row.security_title ?? "—"}{row.table === "II" && ` · ${t("ui.detail.derivative")}`}</Fact>
          <Fact label={t("ui.detail.form")}>{row.form ?? "—"}</Fact>
          <Fact label={t("ui.detail.source")} className="col-span-2">
            <a href={filingUrl(row)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline">
              {t("ui.detail.sec_filing", { accession: row.accession })}
              <ExternalLink className="size-3" />
            </a>
          </Fact>
        </dl>
      </section>

      <section className="rounded-lg border bg-card px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <h3 className="text-sm font-semibold">{context.ticker ? t("ui.detail.price_title", { ticker: context.ticker, n }) : t("ui.entity.chart_none")}</h3>
          {context.price_fetched_at && <span className="text-xs text-muted-foreground">{t("ui.entity.prices_as_of", { time: formatEt(context.price_fetched_at) })}</span>}
          <NControl className="ml-auto" />
        </div>
        <div className="mt-2 grid gap-3 md:grid-cols-[minmax(0,1fr)_17rem]">
          {context.bars?.length ? (
            <PriceChart bars={context.bars} lines={lines} height={260} />
          ) : (
            <div className="grid h-[260px] place-items-center text-sm text-muted-foreground">
              {["FETCHING", "NOT_FETCHED"].includes(String(context.status)) ? (
                <span className="inline-flex items-center gap-2"><LoaderCircle className="size-4 motion-safe:animate-spin" />{t("ui.entity.chart_fetching", { ticker: context.ticker })}</span>
              ) : (
                tm(context.reason) || t("ui.window.unavailable")
              )}
            </div>
          )}
          <div className="space-y-3 text-sm">
            {([
              ["trade", trade],
              ["disclosure", disclosure],
            ] as const).map(([key, basis]) => (
              <div key={key} className="rounded-md border px-3 py-2">
                <p className="font-medium">{t(`ui.detail.basis.${key}`)}</p>
                <p className="text-xs text-muted-foreground">
                  {basis?.baseline_date ? t("ui.detail.basis.baseline", { day: formatDay(basis.baseline_date, { year: true }) }) : t("ui.detail.basis.none")}
                </p>
                <div className="mt-1.5 grid grid-cols-2 gap-2">
                  <div>
                    <p className="text-xs text-muted-foreground">{t("ui.window.before_head", { n })}</p>
                    <WindowCell className="text-base" side="before" n={n} ticker={tickerState} change={change(basis, "before")} />
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">{t("ui.window.after_head", { n })}</p>
                    <WindowCell className="text-base" side="after" n={n} ticker={tickerState} change={change(basis, "after")} />
                  </div>
                </div>
              </div>
            ))}
            <p className="text-xs text-muted-foreground">{t("ui.detail.basis.note", { n })}</p>
          </div>
        </div>
      </section>

      {!!row.footnotes?.length && (
        <section className="rounded-lg border bg-card px-4 py-3 text-sm">
          <h3 className="mb-1.5 text-sm font-semibold">{t("ui.detail.footnotes")}</h3>
          <ol className="space-y-1">
            {row.footnotes.map((note) => (
              <li key={note.id} className="flex gap-2"><span className="shrink-0 text-xs text-muted-foreground">{note.id}</span><span>{note.text}</span></li>
            ))}
          </ol>
        </section>
      )}
      {row.amendments?.filter((relation) => relation.action.startsWith("UNCONFIRMED")).map((relation) => (
        <AmendmentReview key={relation.id} relation={relation} transaction={row} />
      ))}
    </div>
  );
}
