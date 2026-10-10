import { useEffect, useRef, useState, type RefObject } from "react";
import { useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";

export type PriceComparison = Schemas["PriceComparison"];
export type CompanyOverview = Schemas["CompanyOverview"];
export type OverviewTrade = Schemas["OverviewTrade"];
export const INDICES = ["all", "sp500", "nasdaq100"] as const;
export type IndexFilter = (typeof INDICES)[number];

function remembered(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}

/** URL choices override optional browser preferences, with validated defaults. */
export function useOverviewChoice(name: string, choices: readonly string[] | null, fallback: string, scope = "overview") {
  const [params, setParams] = useSearchParams();
  const storage = `iirp.insider.${scope}.${name}`;
  const candidate = params.get(name) ?? remembered(storage) ?? fallback;
  const value = choices ? choices.includes(candidate) ? candidate : fallback : /^\d+(\.\d{1,2})?$/.test(candidate) ? candidate : fallback;
  const choose = (next: string) => {
    try { localStorage.setItem(storage, next); } catch { /* The URL still stores the choice. */ }
    // Router callbacks do not queue like React state; preserve rapid choices
    // using the URL already written by the preceding control.
    const result = new URLSearchParams(window.location.search);
    result.set(name, next);
    setParams(result, { replace: true });
  };
  return [value, choose] as const;
}

/** Only rows actually intersecting the viewport can ask for current quotes. */
export function useVisibleStocks(container: RefObject<HTMLElement | null>, revision: unknown) {
  const [rows, setRows] = useState<{ symbols: string[]; ids: string[] }>({ symbols: [], ids: [] });
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    const seen = new Set<HTMLElement>();
    const publish = () => {
      const symbols = [...new Set([...seen].map((row) => row.dataset.quoteSymbol?.trim().toUpperCase().replace(/\./g, "-")).filter((symbol): symbol is string => !!symbol && /^[A-Z0-9][A-Z0-9^=-]{0,31}$/.test(symbol)))].sort();
      const ids = [...new Set([...seen].flatMap((row) => (row.dataset.priceIds ?? "").split(",")).filter(Boolean))].sort();
      setRows((previous) => previous.symbols.join() === symbols.join() && previous.ids.join() === ids.join() ? previous : { symbols, ids });
    };
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) seen.add(entry.target as HTMLElement);
        else seen.delete(entry.target as HTMLElement);
      }
      publish();
    });
    const elements = root.querySelectorAll<HTMLElement>("[data-quote-symbol]");
    for (const element of elements) observer.observe(element);
    if (!elements.length) publish();
    return () => observer.disconnect();
  }, [container, revision]);
  return rows;
}

const INSIDER_QUOTE_READS = [["insider-feed-prices"], ["insider-overview"]] as const;

/**
 * One merged durable quote request; server-side due checks coalesce all tabs.
 * Accepted requests re-read the given queries. Returns the last error and when
 * the last request was accepted.
 */
export function useStockQuotes(symbols: readonly string[], reads: readonly (readonly string[])[] = INSIDER_QUOTE_READS) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const queryClient = useQueryClient();
  const signature = symbols.join(",");
  const active = useRef(false);
  const [error, setError] = useState<Error | null>(null);
  const [askedAt, setAskedAt] = useState<number | null>(null);
  useEffect(() => {
    if (!visible || !signature) return;
    const accepted = () => {
      setError(null);
      setAskedAt(Date.now());
      for (const queryKey of reads) void queryClient.invalidateQueries({ queryKey });
    };
    const request = () => {
      if (active.current) return;
      active.current = true;
      void unwrap(client.POST("/api/v1/insider/quotes", { body: { symbols: signature.split(",") } }))
        .then(accepted)
        .catch((reason: Error) => setError(reason))
        .finally(() => { active.current = false; });
    };
    // Give the observer one frame to collect all visible symbols into one request.
    const initial = window.setTimeout(request, 100);
    const timer = window.setInterval(request, intervals.ensure_seconds * 1000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [signature, visible, intervals.ensure_seconds, queryClient]);
  return { error, askedAt };
}
