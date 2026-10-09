import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, Building2, Globe, History, LoaderCircle, Search, User, X } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatDay } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { sourceContext } from "@/lib/navigation";
import { clearRecent, readRecent, type RecentEntity } from "@/lib/recent";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InsiderOverview } from "@/components/insider/InsiderOverview";

type Candidate = Schemas["LookupCandidate"];

function useDebounced(value: string, delay = 250) {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setCurrent(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return current;
}

function ResultRow({ to, icon: Icon, name, detail, extra }: { to: string; icon: typeof User; name: string; detail?: string | null; extra?: string | null }) {
  const location = useLocation();
  return (
    <Link to={to} state={sourceContext(location)} className="group flex items-center gap-3 rounded-md px-3 py-2 hover:bg-accent">
      <Icon className="size-4 shrink-0 text-muted-foreground" strokeWidth={1.8} />
      <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
      {detail && <span className="shrink-0 rounded bg-secondary px-1.5 text-xs font-semibold tabular-nums">{detail}</span>}
      {extra && <span className="w-36 shrink-0 text-right text-xs text-muted-foreground tabular-nums">{extra}</span>}
      <ArrowRight className="size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </Link>
  );
}

/** SEC candidates: choosing one fetches that entity's last six months of filings, then opens its page. */
function SecResults({ query }: { query: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const start = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/insider/lookup", { body: { query } })),
  });
  const jobId = start.data?.job_id;
  const status = useQuery({
    queryKey: ["insider-lookup", jobId],
    queryFn: () => unwrap(client.GET("/api/v1/insider/lookup/{job_id}", { params: { path: { job_id: jobId! } } })),
    enabled: !!jobId,
    initialData: start.data,
    refetchInterval: (state) => (["SUCCEEDED", "FAILED", "CANCELLED", "PARTIAL"].includes(state.state.data?.status ?? "") ? false : 1000),
  });
  const fetch = useMutation({
    mutationFn: (candidate: Candidate) =>
      unwrap(client.POST("/api/v1/insider/fetch", { body: { cik: candidate.cik, kind: candidate.kind, name: candidate.name } }))
        .then((result) => ({ result, candidate })),
    onSuccess: ({ result, candidate }) =>
      navigate(`/${candidate.kind === "company" ? "companies" : "people"}/${candidate.cik}`, {
        state: { ...sourceContext(location), fetchBatch: result.batch_id, name: candidate.name },
      }),
  });
  useEffect(() => {
    start.reset();
    if (query) start.mutate();
    // Each submitted query starts (or reuses) one lookup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);
  const data = status.data;
  const done = data?.status === "SUCCEEDED";
  return (
    <section className="rounded-lg border bg-card">
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <Globe className="size-4 text-muted-foreground" strokeWidth={1.8} />
        <h2 className="text-sm font-semibold">{t("ui.lookup.sec_title")}</h2>
        <span className="text-xs text-muted-foreground">{t("ui.lookup.sec_hint")}</span>
        {!done && !start.error && !status.error && data?.status !== "FAILED" && (
          <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
            {t("ui.lookup.sec_searching")}
          </span>
        )}
      </header>
      <div className="p-1.5">
        {(start.error || status.error) && <p className="px-3 py-2 text-sm text-destructive">{(start.error ?? status.error)!.message}</p>}
        {data?.status === "FAILED" && <p className="px-3 py-2 text-sm text-destructive">{tm(data.error) || t("ui.lookup.sec_failed")}</p>}
        {done && !data.candidates.length && <p className="px-3 py-3 text-sm text-muted-foreground">{t("ui.lookup.sec_none", { query })}</p>}
        {done &&
          data.candidates.map((candidate) => (
            <button
              key={candidate.cik}
              type="button"
              disabled={fetch.isPending}
              onClick={() => fetch.mutate(candidate)}
              className="group flex w-full items-center gap-3 rounded-md px-3 py-2 text-left hover:bg-accent disabled:opacity-60"
            >
              {candidate.kind === "company" ? (
                <Building2 className="size-4 shrink-0 text-muted-foreground" strokeWidth={1.8} />
              ) : (
                <User className="size-4 shrink-0 text-muted-foreground" strokeWidth={1.8} />
              )}
              <span className="min-w-0 flex-1 truncate font-medium">{readableCase(candidate.name)}</span>
              {candidate.tickers.slice(0, 3).map((ticker) => (
                <span key={ticker} className="shrink-0 rounded bg-secondary px-1.5 text-xs font-semibold tabular-nums">{ticker}</span>
              ))}
              <span className="w-24 shrink-0 text-right text-xs text-muted-foreground tabular-nums">CIK {Number(candidate.cik)}</span>
              {candidate.local && <Badge variant="outline">{t("ui.lookup.saved")}</Badge>}
              <span className="inline-flex w-36 shrink-0 items-center justify-end gap-1 text-xs text-muted-foreground group-hover:text-foreground">
                {fetch.isPending && fetch.variables?.cik === candidate.cik ? (
                  <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
                ) : (
                  t("ui.lookup.fetch_six_months")
                )}
              </span>
            </button>
          ))}
        {fetch.error && <p className="px-3 py-2 text-sm text-destructive">{fetch.error.message}</p>}
      </div>
    </section>
  );
}

/** Companies and insiders opened recently in this browser; can be cleared. */
function RecentlyViewed() {
  const { t } = useTranslation();
  const [items, setItems] = useState<RecentEntity[]>(() => readRecent());
  if (!items.length) return null;
  return (
    <section className="rounded-lg border bg-card">
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <History className="size-4 text-muted-foreground" strokeWidth={1.8} />
        <h2 className="text-sm font-semibold">{t("ui.lookup.recent_title")}</h2>
        <span className="text-xs text-muted-foreground">{t("ui.lookup.recent_hint")}</span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="ml-auto h-7 text-xs text-muted-foreground"
          onClick={() => {
            clearRecent();
            setItems([]);
          }}
        >
          <X />
          {t("ui.lookup.recent_clear")}
        </Button>
      </header>
      <div className="grid gap-x-2 p-1.5 md:grid-cols-2">
        {items.map((item) => (
          <ResultRow
            key={`${item.kind}:${item.id}`}
            to={`/${item.kind === "company" ? "companies" : "people"}/${item.id}`}
            icon={item.kind === "company" ? Building2 : User}
            name={readableCase(item.name)}
            detail={item.ticker}
          />
        ))}
      </div>
    </section>
  );
}

/**
 * Insider lookup: a ticker, company name or insider name. Saved data answers
 * as you type; SEC is searched when nothing local matches (or on request).
 */
export function InsidersPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [input, setInput] = useState(params.get("q") ?? "");
  const term = useDebounced(input.trim());
  const secQuery = params.get("sec") ?? "";
  const local = useQuery({
    queryKey: ["insider-local", term],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/lookup", { params: { query: { q: term } }, signal })),
    enabled: term.length > 0,
    placeholderData: (previous) => previous,
  });
  const companies = term ? local.data?.companies ?? [] : [];
  const people = term ? local.data?.people ?? [] : [];
  const empty = term.length > 0 && local.isSuccess && local.data.query === term && !companies.length && !people.length;
  const searchSec = (value: string) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("q", value);
      next.set("sec", value);
      return next;
    }, { replace: true });
  // Nothing saved locally: look the same text up at SEC.
  useEffect(() => {
    if (empty && secQuery !== term) searchSec(term);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [empty, term]);
  useEffect(() => {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (term) next.set("q", term);
      else next.delete("q");
      if (next.get("sec") && next.get("sec") !== term) next.delete("sec");
      return next;
    }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [term]);
  return (
    <div className="mx-auto space-y-4">
      <form
        className="relative"
        onSubmit={(event) => {
          event.preventDefault();
          if (input.trim()) searchSec(input.trim());
        }}
      >
        <Search className="pointer-events-none absolute top-1/2 left-3.5 size-5 -translate-y-1/2 text-muted-foreground" />
        <input
          autoFocus
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder={t("ui.lookup.placeholder")}
          aria-label={t("ui.lookup.placeholder")}
          className="h-12 w-full rounded-lg border bg-card pr-36 pl-11 text-base shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30"
        />
        <Button type="submit" size="sm" variant="outline" className="absolute top-1/2 right-2 -translate-y-1/2" disabled={!input.trim()}>
          <Globe />
          {t("ui.lookup.search_sec")}
        </Button>
      </form>
      <p className="px-1 text-xs text-muted-foreground">{t("ui.lookup.hint")}</p>
      {!term && !secQuery && <RecentlyViewed />}

      {term && (
        <section className="rounded-lg border bg-card">
          <header className="flex items-center gap-2 border-b px-4 py-2.5">
            <h2 className="text-sm font-semibold">{t("ui.lookup.local_title")}</h2>
            <span className="text-xs text-muted-foreground">{t("ui.lookup.local_hint")}</span>
            {local.isFetching && <LoaderCircle className="ml-auto size-3.5 text-muted-foreground motion-safe:animate-spin" />}
          </header>
          <div className={cn("grid gap-x-2 p-1.5", companies.length && people.length && "md:grid-cols-2")}>
            {companies.length > 0 && (
              <div>
                <h3 className="px-3 pt-1.5 pb-1 text-xs font-medium text-muted-foreground">{t("ui.lookup.companies")}</h3>
                {companies.map((row) => (
                  <ResultRow key={row.cik} to={`/companies/${row.cik}`} icon={Building2} name={readableCase(row.name)} detail={row.ticker}
                    extra={row.last_trade ? t("ui.lookup.last_trade", { day: formatDay(row.last_trade) }) : null} />
                ))}
              </div>
            )}
            {people.length > 0 && (
              <div>
                <h3 className="px-3 pt-1.5 pb-1 text-xs font-medium text-muted-foreground">{t("ui.lookup.people")}</h3>
                {people.map((row) => (
                  <ResultRow key={row.cik} to={`/people/${row.cik}`} icon={User} name={readableCase(row.name)}
                    extra={row.last_trade ? t("ui.lookup.last_trade", { day: formatDay(row.last_trade) }) : null} />
                ))}
              </div>
            )}
            {empty && <p className="px-3 py-3 text-sm text-muted-foreground">{t("ui.lookup.local_none")}</p>}
            {local.error && <p className="px-3 py-3 text-sm text-destructive">{local.error.message}</p>}
          </div>
        </section>
      )}
      {secQuery && <SecResults query={secQuery} />}
      <InsiderOverview />
    </div>
  );
}
