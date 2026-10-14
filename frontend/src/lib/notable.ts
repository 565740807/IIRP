import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatMoney } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import type { TradeKey } from "@/lib/windows";

/**
 * The followed people, kept on the server. A person is named by a stable key:
 * "sec:<CIK>", "house:<name>|<district>" or "oge:<name>|<position>".
 */
export type NotablePeople = Schemas["NotablePeopleOutput"];
export type NotablePerson = Schemas["NotablePersonEntry"];
export type NotableReport = Schemas["NotableReport"];
export type NotableTrade = Schemas["NotableTrade"];
export type TradePerson = Schemas["TradePerson"];

export const NOTABLE_KEY = ["notable-people"] as const;
/** Queries that change when the list changes carry this element in their key. */
export const NOTABLE_DEPENDENT = "notable";

export function useNotablePeople(scope: "watched" | "all" = "watched", enabled = true) {
  return useQuery({
    queryKey: scope === "watched" ? NOTABLE_KEY : [...NOTABLE_KEY, "all"],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/people", { params: { query: { scope } }, signal })),
    staleTime: 5 * 60_000,
    enabled,
  });
}

/** The page of one person: /notable/<source>/<the rest of the key>. */
export function personHref(key: string) {
  const at = key.indexOf(":");
  return `/notable/${key.slice(0, at)}/${encodeURIComponent(key.slice(at + 1))}`;
}

/**
 * Follow or stop following one person. The star changes at once; when the server
 * refuses, the list returns to what it was and the error goes back to the caller.
 */
export function useFollowToggle() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, follow, name }: { key: string; follow: boolean; name?: string }) =>
      unwrap(follow
        ? client.POST("/api/v1/notable/people", { body: { key, name: name ?? null } })
        : client.DELETE("/api/v1/notable/people", { params: { query: { key } } })),
    onMutate: async ({ key, follow, name }) => {
      await queryClient.cancelQueries({ queryKey: NOTABLE_KEY, exact: true });
      const previous = queryClient.getQueryData<NotablePeople>(NOTABLE_KEY);
      if (previous) {
        const items = previous.items.filter((item) => item.key !== key);
        if (follow) {
          const source = key.slice(0, key.indexOf(":")) as NotablePerson["source"];
          items.push({ key, source, name: name ?? key, category: null, note: null, cik: null, district: null,
            position: null, default: false, local: true, watched: true, added_at: new Date().toISOString() });
        }
        queryClient.setQueryData<NotablePeople>(NOTABLE_KEY, { ...previous, items });
      }
      return { previous };
    },
    onError: (_error, _change, context) => {
      if (context?.previous) queryClient.setQueryData(NOTABLE_KEY, context.previous);
      void queryClient.invalidateQueries({ queryKey: NOTABLE_KEY, exact: true });
    },
    onSuccess: (data) => {
      queryClient.setQueryData(NOTABLE_KEY, data);
      void queryClient.invalidateQueries({ predicate: (query) => query.queryKey.indexOf(NOTABLE_DEPENDENT) > 0
        || (query.queryKey[0] === NOTABLE_KEY[0] && query.queryKey.length > 1) });
    },
  });
}

type T = (key: string, options?: Record<string, unknown>) => string;

/** "CEO · Director", "Representative CA11", "Secretary of Commerce"; a role without a name shows its title. */
export function roleLabel(person: { roles: readonly string[]; title?: string | null; district?: string | null }, t: T) {
  return person.roles.slice(0, 2).map((role) => role === "official" || role === "other"
    ? readableCase(person.title ?? "") || t(`ui.notable.role.${role}`)
    : t(`ui.notable.role.${role}`, { district: person.district ?? "" })).join(" · ");
}

/** A display name: the default list's, else the filed name in readable case. */
export function personName(person: { name: string }) {
  return readableCase(person.name);
}

/** A reported value range: "$1M–$5M", "Over $50M"; an exact SEC amount stays exact. */
export function amountText(trade: Pick<NotableTrade, "amount" | "amount_low" | "amount_high" | "currency">, t: T) {
  const currency = trade.currency && trade.currency !== "USD" ? trade.currency : undefined;
  if (trade.amount != null) return formatMoney(trade.amount, { currency });
  if (trade.amount_low == null) return null;
  if (trade.amount_high == null) return t("ui.notable.amount_over", { amount: formatMoney(trade.amount_low) });
  return t("ui.notable.amount_range", { low: formatMoney(trade.amount_low), high: formatMoney(trade.amount_high) });
}

/** Where a row opens: the SEC trade page, or the government trade page. */
export function tradeHref(trade: Pick<NotableTrade, "id" | "source">) {
  return trade.source === "sec" ? `/transactions/${trade.id}` : `/notable/trades/${encodeURIComponent(trade.id)}`;
}

/**
 * The window keys of the rows on screen only: each coded trade once from its trade
 * date (before and after n) and once from the last close before its publication
 * could move the market (``published_basis``, after n). Rows off
 * screen, and bonds, funds and rows without a code, ask for nothing.
 */
export function windowKeys(trades: readonly NotableTrade[], visible: readonly string[]): TradeKey[] {
  const shown = new Set(visible);
  return trades.filter((trade) => shown.has(trade.id) && trade.price_ticker).flatMap((trade) => [
    ...(trade.transaction_date ? [{ ticker: trade.price_ticker, date: trade.transaction_date, issuer_id: trade.issuer_id }] : []),
    ...(trade.published_basis ? [{ ticker: trade.price_ticker, date: trade.published_basis, issuer_id: trade.issuer_id }] : []),
  ]);
}
