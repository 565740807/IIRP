import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatDay, formatMoney } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { personHref } from "@/lib/notable";

/**
 * Notifications made by the worker from new local data: followed people's trades and
 * rule matches. The bell reads them; the settings decide whether they are made and
 * whether they also go to a phone (ntfy). Desktop notifications are a choice of this
 * browser, so they are remembered here, not on the server.
 */
export type NotificationItem = Schemas["NotificationItem"];
export type NotificationSettings = Schemas["NotificationSettingsOutput"];

export const NOTIFICATIONS_KEY = ["notifications"] as const;
export const NOTIFICATION_SETTINGS_KEY = ["notification-settings"] as const;
const DESKTOP_KEY = "iirp.notifications.desktop";
const SEEN_KEY = "iirp.notifications.desktop_seen";

export function useNotifications(poll: number | false) {
  return useQuery({
    queryKey: NOTIFICATIONS_KEY,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notifications", { params: { query: { limit: 30 } }, signal })),
    refetchInterval: poll,
    // Desktop notifications need the list while the tab is in the background.
    refetchIntervalInBackground: desktopChosen(),
    staleTime: 10_000,
  });
}

export function useMarkRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { ids?: string[]; all?: boolean }) => unwrap(client.POST("/api/v1/notifications/read", { body: { ids: body.ids ?? [], all: body.all ?? false } })),
    onSuccess: (data) => queryClient.setQueryData(NOTIFICATIONS_KEY, data),
  });
}

export function useNotificationSettings() {
  return useQuery({
    queryKey: NOTIFICATION_SETTINGS_KEY,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notifications/settings", { signal })),
    refetchInterval: (query) => (query.state.data?.push.waiting ? 3000 : false),
  });
}

export type SettingsChange = Schemas["SettingsChange"];

export function useSaveNotificationSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (change: SettingsChange) => unwrap(client.PUT("/api/v1/notifications/settings", { body: change })),
    onSuccess: (data) => {
      queryClient.setQueryData(NOTIFICATION_SETTINGS_KEY, data);
      void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY });
    },
  });
}

/** The desktop choice of this browser; it only works while an IIRP page is open. */
export function desktopChosen() {
  try {
    return localStorage.getItem(DESKTOP_KEY) === "on";
  } catch {
    return false;
  }
}

export function chooseDesktop(on: boolean) {
  try {
    if (on) localStorage.setItem(DESKTOP_KEY, "on");
    else localStorage.removeItem(DESKTOP_KEY);
  } catch {
    /* Private windows may refuse storage; the switch then lasts for this page only. */
  }
}

/**
 * The notifications this browser has not shown on the desktop yet, oldest first, and the
 * newest time seen. The first reading only remembers where it is, so turning desktop
 * notifications on never replays old ones.
 */
export function unseen(items: readonly NotificationItem[], seen: string | null) {
  const newest = items.reduce<string | null>((latest, item) => (!latest || item.created_at > latest ? item.created_at : latest), seen);
  if (!seen) return { fresh: [] as NotificationItem[], newest };
  return { fresh: items.filter((item) => item.created_at > seen).sort((a, b) => a.created_at.localeCompare(b.created_at)), newest };
}

export function seenAt() {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null;
  }
}

export function markSeen(value: string | null) {
  try {
    if (value) localStorage.setItem(SEEN_KEY, value);
  } catch {
    /* As above. */
  }
}

type T = (key: string, options?: Record<string, unknown>) => string;
type Data = Record<string, any>;

function money(value: unknown) {
  return value == null ? null : formatMoney(String(value));
}

function range(low: unknown, high: unknown, t: T) {
  if (low == null) return null;
  return high == null ? t("ui.notable.amount_over", { amount: money(low) }) : t("ui.notable.amount_range", { low: money(low), high: money(high) });
}

/** One notification as two short lines: what happened, and when. */
export function describe(item: NotificationItem, t: T) {
  const data = item.data as Data;
  const asset = data.ticker ?? data.asset_name ?? "";
  const day = (value: string | null | undefined) => (value ? formatDay(value) : "—");
  const traded = data.traded_from && data.traded_to && data.traded_to !== data.traded_from
    ? t("ui.notifications.dates_range", { from: day(data.traded_from), to: day(data.traded_to) })
    : day(data.traded_from);
  const when = t("ui.notifications.when", { traded, published: day(data.published) });
  if (item.kind === "rule_executive") {
    const role = (data.person?.roles ?? []).map((value: string) => t(`ui.notable.role.${value}`)).join(" / ");
    return { title: t("ui.notifications.executive", { role, asset, amount: money(data.amount) }), detail: `${readableCase(data.person?.name ?? "")} · ${when}` };
  }
  if (item.kind === "rule_cluster") {
    return {
      title: t("ui.notifications.cluster", { count: data.people, days: data.days, asset }),
      detail: `${(data.names ?? []).map((name: string) => readableCase(name)).join(", ")} · ${when}`,
    };
  }
  const person = readableCase(data.person?.name ?? "");
  let what: string;
  if (data.source === "sec") {
    what = (["buy", "sell"] as const).filter((side) => data[side]).map((side) => {
      const part = data[side];
      const plan = side === "sell" && part.plan ? ` (${t(`ui.notifications.plan.${part.plan}`)})` : "";
      return `${t(`ui.notable.side.${side}`)} ${money(part.amount) ?? ""}${plan}`.trim();
    }).join(" · ");
    what = `${what} ${asset}`.trim();
  } else if (data.rows === 1 && data.items?.[0]) {
    const row = data.items[0];
    what = [t(`ui.notable.side.${row.side}`), row.ticker ?? row.asset_name, range(row.amount_low, row.amount_high, t)].filter(Boolean).join(" ");
  } else {
    what = [data.buys ? t("ui.notifications.report_buys", { count: data.buys }) : "",
      data.sells ? t("ui.notifications.report_sells", { count: data.sells }) : ""].filter(Boolean).join(t("ui.notifications.join"));
  }
  return { title: t("ui.notifications.trade", { person, what }), detail: when };
}

/** Where a notification opens: the trade, the person (a report with several rows), or the company. */
export function notificationHref(item: NotificationItem) {
  const data = item.data as Data;
  if (item.kind !== "trade") return item.issuer_id ? `/companies/${item.issuer_id}` : "/notable";
  if (data.source === "sec") return data.trade_id ? `/transactions/${data.trade_id}` : "/notable";
  if (data.trade_id) return `/notable/trades/${encodeURIComponent(data.trade_id)}`;
  return item.person_key ? personHref(item.person_key) : "/notable";
}
