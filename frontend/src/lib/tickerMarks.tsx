import { createContext, useContext, useMemo, type ComponentType, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Small marks beside a stock code. An optional feature adds one through
 * Feature.tickerMark; shared pages wrap a table in TickerBatch and put
 * TickerMarks beside each code. Each mark receives the code and the batch of
 * codes it belongs to, so it can read the whole table in one request with one
 * shared query key. Without any such feature nothing is rendered.
 */

/** Codes compare in upper case with "." read as "-" (BRK.B is BRK-B), as the backend compares them. */
export function tickerKey(value: string | null | undefined) {
  return (value ?? "").trim().toUpperCase().replace(/\./g, "-");
}

/** The most codes one batch (and so one request) carries. */
export const BATCH_LIMIT = 200;

/** Unique codes in sorted order, split into batches of at most `limit`. */
export function tickerBatches(tickers: readonly (string | null | undefined)[], limit = BATCH_LIMIT): string[][] {
  const keys = [...new Set(tickers.map(tickerKey).filter(Boolean))].sort();
  const batches: string[][] = [];
  for (let start = 0; start < keys.length; start += limit) batches.push(keys.slice(start, start + limit));
  return batches;
}

export type TickerMarkProps = {
  /** The code in its compared form (tickerKey). */
  ticker: string;
  /** Every code of the same batch, the code included, sorted. */
  batch: readonly string[];
  /** Tight places (a table's company column): the shortest form, the rest on hover. */
  compact?: boolean;
};
export type TickerMark = ComponentType<TickerMarkProps>;

/** The installed features' marks; provided once by the application. */
export const TickerMarkSources = createContext<readonly TickerMark[]>([]);

const Batches = createContext<ReadonlyMap<string, readonly string[]> | null>(null);

/** Groups the codes of one table so every mark of the table reads them together. */
export function TickerBatch({ tickers, children }: { tickers: readonly (string | null | undefined)[]; children: ReactNode }) {
  const batches = tickerBatches(tickers);
  const signature = batches.map((batch) => batch.join(",")).join(";");
  const lookup = useMemo(() => {
    const map = new Map<string, readonly string[]>();
    for (const batch of signature ? signature.split(";").map((part) => part.split(",")) : []) {
      for (const key of batch) map.set(key, batch);
    }
    return map;
  }, [signature]);
  return <Batches.Provider value={lookup}>{children}</Batches.Provider>;
}

/** The batch a code belongs to; a code outside any TickerBatch is a batch of its own. */
export function useTickerBatch(ticker: string | null | undefined): readonly string[] {
  const lookup = useContext(Batches);
  const key = tickerKey(ticker);
  return lookup?.get(key) ?? (key ? [key] : []);
}

/** Every installed feature's mark for one code, or nothing at all. */
export function TickerMarks({ ticker, compact, className }: { ticker: string | null | undefined; compact?: boolean; className?: string }) {
  const marks = useContext(TickerMarkSources);
  const batch = useTickerBatch(ticker);
  const key = tickerKey(ticker);
  if (!marks.length || !key) return null;
  // A feature without a mark for this code renders nothing; :empty then hides the box.
  return (
    <span data-ticker-marks className={cn("inline-flex shrink-0 items-center gap-1 empty:hidden", className)}>
      {marks.map((Mark, index) => <Mark key={index} ticker={key} batch={batch} compact={compact}/>)}
    </span>
  );
}
