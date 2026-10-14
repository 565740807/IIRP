/// <reference types="vite/client" />
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import type { TickerMark } from "@/lib/tickerMarks";
import type { WatchlistNewsSource } from "@/lib/watchlistNews";

/**
 * Optional features: each directory under features/ with an index.tsx adds its
 * routes, a sidebar entry and its own translations (locales/<language>.json).
 * A checkout without such a directory simply has no such page.
 */
export type Feature = {
  /** Sidebar and page title: ui.nav.<key> and ui.page.<key>. */
  key: string;
  /** The sidebar link and the path prefix of the feature's pages. */
  path: string;
  icon: LucideIcon;
  /** Place in the sidebar: home 10, insiders 20, sectors 30, analysis 40, data 50. */
  order: number;
  routes: { path: string; element: ReactNode }[];
  /** A small mark beside stock codes on shared pages (see lib/tickerMarks). */
  tickerMark?: TickerMark;
  /** Entries of the home page's watchlist notice (see lib/watchlistNews). */
  watchlistNews?: WatchlistNewsSource;
};

const modules = import.meta.glob<{ feature: Feature }>("./features/*/index.tsx", { eager: true });
export const FEATURES: Feature[] = Object.values(modules).map((module) => module.feature).sort((a, b) => a.order - b.order);

/** Marks beside stock codes, one per feature that has one. */
export const TICKER_MARKS: TickerMark[] = FEATURES.flatMap((feature) => (feature.tickerMark ? [feature.tickerMark] : []));

/** Watchlist news sources, one per feature that has one. */
export const WATCHLIST_NEWS: WatchlistNewsSource[] = FEATURES.flatMap((feature) => (feature.watchlistNews ? [feature.watchlistNews] : []));
