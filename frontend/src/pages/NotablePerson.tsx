import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, FileText, LoaderCircle } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { formatDay, formatEt, formatNumber } from "@/lib/format";
import { NOTABLE_DEPENDENT, personName, roleLabel } from "@/lib/notable";
import { sourceContext, sourceHref } from "@/lib/navigation";
import { useInsiderN } from "@/lib/windows";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FoldSection } from "@/components/ui/fold";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/ui/states";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { FollowStar } from "@/components/notable/FollowStar";
import { NControl } from "@/components/insider/NControl";
import { AssetChoice } from "@/components/notable/AssetChoice";
import { NotableTable } from "@/components/notable/NotableTable";
import { PersonSummary } from "@/components/notable/PersonSummary";

const PAGE = 5;

/** One followed (or followable) person: who they are, all their trades from every source, and their original filings. */
export function NotablePersonPage() {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const { source = "", id = "" } = useParams();
  const key = `${source}:${id}`;
  const [params, setParams] = useSearchParams();
  const side: "any" | "buy" | "sell" = params.get("side") === "buy" ? "buy" : params.get("side") === "sell" ? "sell" : "any";
  const assets: "equities" | "all" = params.get("assets") === "all" ? "all" : "equities";
  const change = (name: string, value: string) => setParams(() => {
    const next = new URLSearchParams(window.location.search);
    next.set(name, value);
    return next;
  }, { replace: true });
  const { n } = useInsiderN();
  const [cursors, setCursors] = useState<string[]>([""]);
  useEffect(() => setCursors([""]), [key, side, assets]);
  const cursor = cursors[cursors.length - 1];
  const person = useQuery({
    queryKey: ["notable-person", key, NOTABLE_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/person", { params: { query: { key } }, signal })),
    retry: false,
  });
  const query = { person: key, assets, limit: PAGE, ...(side !== "any" ? { direction: side } : {}), ...(cursor ? { cursor } : {}) };
  const trades = useQuery({
    queryKey: ["notable-reports", query],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/reports", { params: { query }, signal })),
    placeholderData: (previous) => previous,
    enabled: !person.error,
  });
  const data = person.data;
  const name = data ? personName(data) : "";
  const note = data?.note ? (i18n.language === "zh" ? data.note.zh : data.note.en) : null;
  const role = data ? roleLabel({ roles: data.roles, title: data.title ?? data.position, district: data.district }, t) : "";
  const first = (cursors.length - 1) * PAGE;

  if (person.error) {
    return (
      <div className="space-y-3">
        <Link to="/notable" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"><ArrowLeft className="size-3.5" />{t("ui.notable.back")}</Link>
        <ErrorState error={person.error} hint={t("ui.notable.person_missing_hint")} />
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div>
        <Link to={sourceHref(location.state, "/notable")} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-3.5" />{t("ui.notable.back")}
        </Link>
        <h2 className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xl font-semibold tracking-tight" data-person-name>
          {data ? <span className="truncate" title={name}>{name}</span> : <Skeleton className="h-6 w-56" />}
          {data && <FollowStar personKey={key} name={name} className="-mx-1" />}
          {data && role && <Badge variant="secondary" className="text-xs font-normal" title={data.title ?? data.position ?? undefined}>{role}</Badge>}
          {data?.category && data.source === "sec" && <span className="text-xs font-normal text-muted-foreground">{t(`ui.notable.category_one.${data.category}`)}</span>}
        </h2>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground" data-person-identity>
          {note && <span className="text-foreground">{note}</span>}
          <span>{t(`ui.notable.source_name.${source}`, { defaultValue: source })}</span>
          {data?.cik && <span className="tabular-nums">CIK {Number(data.cik)}</span>}
          {data?.filings && (
            <>
              <span>{t("ui.notable.filings_local", { count: data.filings.count })}{data.filings.latest_at ? ` · ${t("ui.notable.latest", { time: formatEt(data.filings.latest_at, { zone: false }) })}` : ""}</span>
              <Link to={`/people/${data.cik}`} state={sourceContext(location)} className="hover:text-foreground hover:underline">{t("ui.notable.insider_page")}</Link>
              <a href={data.filings.edgar_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:text-foreground hover:underline">
                {t("ui.notable.edgar_list")}<ExternalLink className="size-3" aria-hidden />
              </a>
            </>
          )}
          {data && data.source !== "sec" && (
            <a href="#notable-reports" className="hover:text-foreground hover:underline">{t("ui.notable.reports_local", { count: data.reports.length })}</a>
          )}
          {data && !data.local && <span className="text-warn">{t(`ui.notable.not_local.${data.source}`)}</span>}
        </p>
      </div>

      {!person.isPending && <PersonSummary personKey={key} n={n} />}

      {trades.error && !trades.data ? <ErrorState error={trades.error} retry={() => void trades.refetch()} /> : !trades.data ? <TableSkeleton rows={8} columns={7} /> : (
        <NotableTable table="notable.person" person={false} key={key + side + assets + cursor} groups={trades.data.items}
          toolbar={<>
            <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={side} aria-label={t("ui.notable.filter.side")}
              onValueChange={(value) => value && change("side", value)}>
              <ToggleGroupItem value="any" className="px-2.5">{t("ui.notable.filter.any_side")}</ToggleGroupItem>
              <ToggleGroupItem value="buy" className="px-2.5">{t("ui.notable.side.buy")}</ToggleGroupItem>
              <ToggleGroupItem value="sell" className="px-2.5">{t("ui.notable.side.sell")}</ToggleGroupItem>
            </ToggleGroup>
            <AssetChoice value={assets} choose={(value) => change("assets", value)} />
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
              {trades.isFetching && <LoaderCircle className="size-3.5 motion-safe:animate-spin" />}
              {t("ui.notable.group.total", { count: trades.data.total, formatted: formatNumber(trades.data.total, 0) })}
            </span>
            <NControl />
          </>}
          empty={<EmptyState title={t("ui.notable.person_empty")} reason={t(data && !data.local ? `ui.notable.not_local.${data.source}` : "ui.notable.person_empty_reason")} />}
          footer={trades.data.total > PAGE ? (
            <div className="flex items-center justify-end gap-2 pt-2 text-xs text-muted-foreground" data-pager>
              <span className="tabular-nums">{t("ui.notable.group.page_range", { start: formatNumber(first + 1, 0), end: formatNumber(first + trades.data.items.length, 0), total: formatNumber(trades.data.total, 0) })}</span>
              <Button size="xs" variant="outline" disabled={cursors.length === 1 || trades.isFetching} onClick={() => setCursors((list) => list.slice(0, -1))}>
                <ChevronLeft />{t("ui.notable.previous")}
              </Button>
              <Button size="xs" variant="outline" disabled={!trades.data.next_cursor || trades.isFetching} onClick={() => setCursors((list) => [...list, trades.data!.next_cursor!])}>
                {t("ui.notable.next")}<ChevronRight />
              </Button>
            </div>
          ) : null} />
      )}

      {data && data.source !== "sec" && (
        <FoldSection id="notable.reports" anchor="notable-reports" title={t("ui.notable.reports_title")}
          meta={t("ui.notable.reports_meta", { count: data.reports.length })}>
          {data.reports.length ? (
            <ul className="grid gap-x-6 gap-y-1 rounded-lg border bg-card px-3 py-2 text-sm sm:grid-cols-2 xl:grid-cols-3" data-report-list>
              {data.reports.map((report) => (
                <li key={report.id} className="flex min-w-0 items-center gap-2">
                  <FileText className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  <a href={report.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 tabular-nums hover:underline" title={t("ui.notable.open_original")}>
                    {report.report_date ? formatDay(report.report_date, { year: true }) : t("ui.notable.no_date")}
                    <ExternalLink className="size-3 text-muted-foreground" aria-hidden />
                  </a>
                  {report.file_url && <a href={report.file_url} target="_blank" rel="noreferrer" className="text-xs text-muted-foreground hover:text-foreground hover:underline">{t("ui.notable.saved_copy")}</a>}
                  {report.amendment && <Badge variant="outline" className="h-4 px-1 text-xs">{t("ui.notable.amended")}</Badge>}
                  {report.superseded && <span className="text-xs text-muted-foreground" title={t("ui.notable.superseded_tip")}>{t("ui.notable.superseded")}</span>}
                  {report.ocr_pages > 0 && <span className="text-xs text-muted-foreground">{t("ui.notable.scanned_pages", { count: report.ocr_pages })}</span>}
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted-foreground">{t("ui.notable.reports_none")}</p>}
        </FoldSection>
      )}
    </div>
  );
}
