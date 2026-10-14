import { createContext, type ReactNode } from "react";

/**
 * News of watched stocks on the home page. The Insider part is public; an
 * optional feature adds its own entries through Feature.watchlistNews. Without
 * any such feature the notice holds Insider entries only, and with no news at
 * all it is not rendered.
 */
export type WatchlistNewsItem = {
  /** Unique among the feature's entries. */
  key: string;
  /** The watched code in its compared form (tickerKey). */
  ticker: string;
  /** When it became news (an ISO instant): entries up to the acknowledged time are hidden. */
  at: string;
  /** The short phrase in the one-line notice after the code. */
  summary: ReactNode;
  /** Its line in the "View" list. */
  detail: ReactNode;
};

/**
 * A React hook an optional feature supplies: its news of the watched codes since
 * each code's start (the public watchlist's rule), or undefined while it loads.
 * Its query key carries WATCH_DEPENDENT, so it is read again after the watchlist
 * changes or is acknowledged. The installed features are fixed when the
 * application loads, so every render calls the same hooks in the same order.
 */
export type WatchlistNewsSource = () => readonly WatchlistNewsItem[] | undefined;

/** The installed features' sources; provided once by the application. */
export const WatchlistNewsSources = createContext<readonly WatchlistNewsSource[]>([]);

export type NewsStock = { ticker: string; at: string; summaries: { key: string; node: ReactNode }[]; details: { key: string; node: ReactNode }[] };

/**
 * Entries grouped by code, newest code first, leaving out anything at or before
 * ``acknowledged`` (the time just acknowledged, before the server reads it back).
 */
export function newsByStock(items: readonly WatchlistNewsItem[], acknowledged: string | null): NewsStock[] {
  const since = acknowledged ? Date.parse(acknowledged) : -Infinity;
  const stocks = new Map<string, NewsStock>();
  for (const item of items) {
    if (Date.parse(item.at) <= since) continue;
    const stock = stocks.get(item.ticker) ?? { ticker: item.ticker, at: item.at, summaries: [], details: [] };
    if (Date.parse(item.at) > Date.parse(stock.at)) stock.at = item.at;
    stock.summaries.push({ key: item.key, node: item.summary });
    stock.details.push({ key: item.key, node: item.detail });
    stocks.set(item.ticker, stock);
  }
  return [...stocks.values()].sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || (a.ticker < b.ticker ? -1 : 1));
}
