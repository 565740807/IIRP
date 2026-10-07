import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { homeQuery } from "@/lib/queries";
import {
  createFreshnessRefresher,
  marketPageVisible,
  newestFreshness,
  type RefreshReason,
} from "@/freshnessRefresh";

/**
 * Refresh rules (D22). The server owns every SEC and Yahoo request and
 * coalesces asks from all tabs; a page only says "I am visible, is anything
 * due?" when it opens, when it becomes visible again and every
 * `ensure_seconds` while visible. All intervals come from config/refresh.toml
 * through /api/v1/home; nothing polls while the tab is hidden.
 */
type Freshness = Schemas["FreshnessOutput"];
type BrowserRefresh = Schemas["BrowserRefresh"];

export const DEFAULT_REFRESH: BrowserRefresh = {
  home_poll_seconds: 10,
  feed_poll_seconds: 5,
  ensure_seconds: 30,
};

export function usePageVisible() {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || !document.hidden);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

/** Intervals from the last /home read (config/refresh.toml), defaults before it. */
export function useRefreshIntervals(): BrowserRefresh {
  // Observe the home read without starting it; the home page owns its polling.
  const query = useQuery({ ...homeQuery, enabled: false });
  return query.data?.refresh ?? DEFAULT_REFRESH;
}

type RefreshState = {
  /** A check is being requested or a source is fetching right now. */
  refreshing: boolean;
  freshness: Freshness | undefined;
  error: Error | null;
  checkNow: () => void;
};

const RefreshContext = createContext<RefreshState>({
  refreshing: false,
  freshness: undefined,
  error: null,
  checkNow: () => {},
});

export const useRefresh = () => useContext(RefreshContext);

function busy(freshness: Freshness | undefined) {
  return Object.values(freshness?.sources ?? {}).some(
    (source) => (source as { status?: string })?.status === "checking",
  );
}

export function RefreshProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const marketVisible = useRef(marketPageVisible(location.pathname));
  marketVisible.current = marketPageVisible(location.pathname);
  const [asking, setAsking] = useState<RefreshReason | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const controller = useRef<ReturnType<typeof createFreshnessRefresher<Freshness>> | null>(null);

  const status = useQuery({
    queryKey: ["freshness"],
    queryFn: async () =>
      newestFreshness(
        queryClient.getQueryData<Freshness>(["freshness"]),
        await unwrap(client.GET("/api/v1/freshness")),
      ),
    // Follow a running fetch closely; otherwise the periodic ask brings news.
    refetchInterval: (query) =>
      visible ? (busy(query.state.data) ? 3000 : intervals.ensure_seconds * 1000) : false,
    refetchOnWindowFocus: false,
  });
  const wasBusy = useRef(false);
  useEffect(() => {
    const now = busy(status.data);
    // A fetch just finished: re-read what it saved.
    if (wasBusy.current && !now) {
      void queryClient.invalidateQueries({ queryKey: ["home"] });
      void queryClient.invalidateQueries({ queryKey: ["feed-updates"] });
    }
    wasBusy.current = now;
  }, [status.data, queryClient]);

  useEffect(() => {
    const current = createFreshnessRefresher<Freshness>({
      visible: () => !document.hidden,
      marketVisible: () => marketVisible.current,
      request: (payload) => unwrap(client.POST("/api/v1/freshness/ensure", { body: payload })),
      started: (reason) => {
        setAsking(reason);
        setError(null);
      },
      received: (value) => {
        queryClient.setQueryData<Freshness>(["freshness"], (previous) => newestFreshness(previous, value));
        void queryClient.invalidateQueries({ queryKey: ["home"] });
        void queryClient.invalidateQueries({ queryKey: ["feed-updates"] });
        void queryClient.invalidateQueries({ queryKey: ["my-tasks"] });
      },
      failed: (reason) => setError(reason instanceof Error ? reason : new Error(String(reason))),
      settled: () => setAsking(null),
    });
    controller.current = current;
    const online = () => void current.ensure("resume");
    window.addEventListener("online", online);
    return () => {
      current.dispose();
      controller.current = null;
      window.removeEventListener("online", online);
    };
  }, [queryClient]);

  // Opening a page (or moving to the home page) asks at once.
  useEffect(() => {
    void controller.current?.ensure("open");
  }, [location.pathname]);

  // Coming back to the tab asks at once; while visible, ask on the interval.
  useEffect(() => {
    if (!visible) return;
    void controller.current?.ensure("resume");
    const timer = window.setInterval(() => void controller.current?.ensure("visible"), intervals.ensure_seconds * 1000);
    return () => window.clearInterval(timer);
  }, [visible, intervals.ensure_seconds]);

  return (
    <RefreshContext.Provider
      value={{
        refreshing: asking !== null || busy(status.data),
        freshness: status.data,
        error: error ?? (status.error as Error | null),
        checkNow: () => void controller.current?.ensure("manual"),
      }}
    >
      {children}
    </RefreshContext.Provider>
  );
}
