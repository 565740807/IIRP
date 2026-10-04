import { Timestamp } from "./Timestamp";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  useReadingState,
  sourceContext,
  useContextSearchParams,
} from "../researchStorage";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import {
  api,
  display,
  facts,
  formatNumber,
  requestId,
  rows,
  type Fact,
} from "../api";
import { Button, EmptyState, ErrorNotice, Loading } from "./ui";
import type { components } from "../generated/api";
import { mergeGroups, mergeRemovalIds } from "../feedMerge";
import { readFeedWithRetry } from "../feedRecovery";
type FeedUpdates = components["schemas"]["FeedUpdatesOutput"];
const filters = [
  { id: "focus", label: "全部重点" },
  { id: "buy", label: "买入" },
  { id: "sell", label: "卖出" },
  { id: "derivative", label: "期权与衍生品" },
  { id: "other", label: "其他行为" },
  { id: "all", label: "全部申报" },
];
type CardReading = { open: boolean; page: number; cursors: string[] };
type FeedStream = {
  removedIds?: string[];
  pendingMerge?: { baseSession: string; targetSession: string; cursor: string };
  recoveryUnknown?: boolean;
  groups: Fact[];
  latestSession: string;
  nextCursor: string | null;
  pages: number;
  total: number;
};
type FeedReading = {
  stream?: FeedStream;
  cursors: string[];
  cards: Record<string, CardReading>;
  schema?: number;
};
const readingCache = new Map<string, FeedReading>();
function savedReading(session: string): FeedReading {
  const cached = readingCache.get(session);
  if (cached) return cached;
  let reading: FeedReading = { cursors: [""], cards: {} };
  try {
    const stored = JSON.parse(
      sessionStorage.getItem(`iirp.feed.${session}`) ?? "null",
    );
    if (stored && Array.isArray(stored.cursors) && stored.cards)
      reading =
        [2, 3, 4].includes(stored.schema)
          ? stored
          : {
              schema: 2,
              cursors: stored.cursors,
              cards: Object.fromEntries(
                Object.entries(stored.cards).map(([key, value]) => [
                  key,
                  { open: (value as CardReading).open, page: 0, cursors: [""] },
                ]),
              ),
            };
    // Old mixed-version caches did not record whether a delta finished. Their
    // missing cursor cannot be reconstructed from card versions; keep the
    // reading visible, but require an explicit new snapshot before updating.
    if (reading.schema !== 4 && reading.stream && !reading.stream.pendingMerge &&
        reading.stream.groups.some((group) => group._session_id &&
          group._session_id !== reading.stream!.latestSession && !group._removed)) {
      reading.stream.recoveryUnknown = true;
    }
  } catch {
    /* A browser storage restriction must not prevent reading. */
  }
  if (session) readingCache.set(session, reading);
  return reading;
}
function saveReading(session: string, reading: FeedReading) {
  if (!session) return;
  reading.schema = 4;
  readingCache.set(session, reading);
  try {
    sessionStorage.setItem(`iirp.feed.${session}`, JSON.stringify(reading));
  } catch {
    /* The current tab still retains its reading state in memory. */
  }
}
export function TransactionTable({
  items,
  showCompany = false,
  hideOwner = false,
  focusOwnerId,
  hideDetailLink = false,
}: {
  items: Fact[];
  showCompany?: boolean;
  hideOwner?: boolean;
  focusOwnerId?: string;
  hideDetailLink?: boolean;
}) {
  const location = useLocation();
  const from = sourceContext(location);
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {showCompany && <th>公司 / ticker</th>}
            {!hideOwner && (
              <th>{focusOwnerId ? "申报时身份" : "主体 / 申报时身份"}</th>
            )}
            <th>行为 / 代码</th>
            <th>股数</th>
            <th>申报单价</th>
            <th>申报金额</th>
            <th>实际交易日</th>
            {!hideDetailLink && <th>查看</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((r, i) => (
            <tr key={r.id ?? i}>
              {showCompany && (
                <td>
                  <Link
                    state={from}
                    className="text-link"
                    to={"/companies/" + r.issuer_id}
                  >
                    {display(r.issuer_name ?? r.ticker ?? r.issuer_id)}
                  </Link>
                  {r.ticker && <small>{r.ticker}</small>}
                </td>
              )}
              {!hideOwner && (
                <td>
                  {focusOwnerId ? (
                    <span>
                      {rows(r.owners)
                        .filter((owner) => owner.id === focusOwnerId)
                        .map((owner) =>
                          [
                            (owner.roles ?? []).join("、"),
                            owner.entity_type === "institution" ? "机构" : "",
                          ]
                            .filter(Boolean)
                            .join(" · "),
                        )
                        .filter(Boolean)
                        .join("、") || "申报未注明职务"}
                    </span>
                  ) : rows(r.owners).length ? (
                    rows(r.owners).map((o) => (
                      <div key={o.id}>
                        <Link
                          state={from}
                          className="text-link owner-link"
                          to={`/people/${o.id}`}
                        >
                          {display(o.name)}
                        </Link>
                        <small>
                          {(o.roles ?? []).join("、") || "申报未注明职务"}
                          {o.entity_type === "institution" ? " · 机构" : ""}
                        </small>
                      </div>
                    ))
                  ) : r.owner_id ? (
                    <Link
                      state={from}
                      className="text-link"
                      to={`/people/${r.owner_id}`}
                    >
                      {display(r.owner ?? r.owner_name)}
                    </Link>
                  ) : rows(r.owners).length ? (
                    rows(r.owners).map((o) => (
                      <Link
                        key={o.id}
                        className="text-link owner-link"
                        to={`/people/${o.id}`}
                      >
                        {display(o.name)}
                      </Link>
                    ))
                  ) : (
                    display(r.owner ?? r.owner_name)
                  )}
                </td>
              )}
              <td
                className={
                  r.code === "P"
                    ? "trade-buy"
                    : r.code === "S"
                      ? "trade-sell"
                      : "trade-derivative"
                }
              >
                {display(r.action ?? r.kind)} · {display(r.code)}
              </td>
              <td>{r.shares == null ? "—" : formatNumber(r.shares)}</td>
              <td>
                {r.price == null
                  ? "源申报单价未知"
                  : `${r.currency ? `${r.currency} ` : ""}${formatNumber(r.price)}`}
                {r.price != null && !r.currency && (
                  <small className="muted">（币种未标注）</small>
                )}
              </td>
              <td>
                {r.amount == null
                  ? r.summary_exclusion_reason
                    ? "未计入确认汇总"
                    : "源申报金额未知"
                  : `${r.currency ? `${r.currency} ` : ""}${formatNumber(r.amount)}`}
                {r.amount != null && !r.currency && (
                  <small className="muted">（币种未标注）</small>
                )}
              </td>
              <td>
                {display(r.transaction_date)}
                {(r.form?.includes("/A") || r.is_amendment_update) && (
                  <small>历史交易修订</small>
                )}
                {r.summary_exclusion_reason && (
                  <small>{r.summary_exclusion_reason}</small>
                )}
              </td>
              {!hideDetailLink && (
                <td>
                  <Link
                    state={from}
                    className="text-link"
                    to={`/transactions/${r.id}`}
                  >
                    交易与价格 →
                  </Link>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function ownerCount(summary: Fact) {
  return (
    [
      summary.person_count ? `${summary.person_count} 人` : "",
      summary.institution_count ? `${summary.institution_count} 家机构` : "",
      summary.unknown_owner_count
        ? `${summary.unknown_owner_count} 个类型未注明主体`
        : "",
    ]
      .filter(Boolean)
      .join("、") || `${summary.owner_count ?? 0} 个主体`
  );
}
export function TradeSummary({
  summary,
  primary = false,
}: {
  summary: Fact[];
  primary?: boolean;
}) {
  const trades = primary
    ? summary.filter((s) => s.table === "I" && ["P", "S"].includes(s.code))
    : summary;
  return (
    <div className="trade-summary">
      {trades.map((s, i) => (
        <p key={i}>
          <strong>
            {s.table === "I" && s.code === "P"
              ? "买入"
              : s.table === "I" && s.code === "S"
                ? "卖出"
                : display(s.kind)}
          </strong>{" "}
          · {ownerCount(s)} ·{" "}
          {s.shares == null
            ? s.reported_value_review_rows
              ? "源申报有数值，待确认后汇总"
              : "源申报股数未知"
            : `${formatNumber(s.shares)} 股`}{" "}
          ·{" "}
          {s.known_amount == null
            ? s.reported_value_review_rows
              ? "金额未计入"
              : "金额未知"
            : `${s.currency || "币种未注明"} ${formatNumber(s.known_amount)}`}
          {(!primary || trades.length > 1 || !!s.review_rows || !!s.missing_price_rows) && (
            <small>
              {[
                !primary || trades.length > 1 ? display(s.security_title) : "",
                s.review_rows ? `${s.review_rows} 条待核对` : "",
                s.missing_price_rows
                  ? `${s.missing_price_rows} 条缺价，金额为已知部分`
                  : "",
              ].filter(Boolean).join(" · ")}
            </small>
          )}
        </p>
      ))}
      {primary && summary.length > trades.length && (
        <small>
          {trades.length ? "另有" : "本范围无主动买卖；"}
          授予、行权等其他行为，展开查看。
        </small>
      )}
    </div>
  );
}
function TraderGroup({
  item,
  session,
  groupId,
  readingSession,
}: {
  item: Fact;
  session: string;
  groupId: string;
  readingSession: string;
}) {
  const [open, setOpen] = useReadingState(
    `insider:${readingSession}:${groupId}:${item.id}:open`,
    false,
  );
  const [cursor, setCursor] = useReadingState(
    `insider:${readingSession}:${groupId}:${item.id}:cursor`,
    "",
  );
  const location = useLocation();
  const query = useQuery({
    queryKey: ["trader-detail", session, groupId, item.id, cursor],
    enabled: open,
    queryFn: () =>
      api<Fact>(
        `/feed/groups/${encodeURIComponent(groupId)}?${new URLSearchParams({ session_id: session, trader_key: item.id, cursor })}`,
      ),
    staleTime: Infinity,
  });
  return (
    <article className="trader-group panel-body" data-trader-id={item.id} data-detail-cursor={cursor} data-detail-open={open}>
      <p>
        <strong>{item.transaction_date}</strong> ·{" "}
        {rows(item.owners).map((o) => (
          <span key={o.id}>
            <Link
              state={sourceContext(location)}
              className="text-link"
              to={`/people/${o.id}`}
            >
              {o.name}
            </Link>{" "}
            · {(o.roles ?? []).join("、") || "申报未注明职务"}{" "}
          </span>
        ))}
      </p>
      <TradeSummary summary={rows(item.summary)} />
      <Button variant="ghost" onClick={() => setOpen(!open)}>
        {open ? "收起逐笔" : `查看 ${item.rows} 条逐笔拆分`}
      </Button>
      {open && (
        <>
          <ErrorNotice error={query.error} retry={query.refetch} />
          {query.isPending ? (
            <Loading />
          ) : (
            <TransactionTable items={rows(query.data?.items)} hideOwner />
          )}
          {(cursor || query.data?.next_cursor) && (
            <div className="button-row">
              <Button disabled={!cursor} onClick={() => setCursor("")}>
                首批明细
              </Button>
              <Button
                disabled={!query.data?.next_cursor}
                onClick={() => setCursor(query.data!.next_cursor)}
              >
                下一批明细
              </Button>
            </div>
          )}
        </>
      )}
    </article>
  );
}
function FeedCard({ group, session }: { group: Fact; session: string }) {
  const revisionSession = String(group._session_id ?? session);
  const [reading, setReading] = useState<CardReading>(
    () =>
      savedReading(session).cards[group.id] ?? {
        open: false,
        page: 0,
        cursors: [""],
      },
  );
  const { page, cursors } = reading;
  const cursor = cursors[page] ?? "";
  function updateReading(next: CardReading) {
    setReading(next);
    const saved = savedReading(session);
    saveReading(session, {
      ...saved,
      cards: { ...saved.cards, [group.id]: next },
    });
  }
  const query = useQuery({
    queryKey: ["feed-group", revisionSession, group.id, cursor],
    queryFn: () =>
      api<Fact>(
        `/feed/groups/${encodeURIComponent(group.id)}?${new URLSearchParams({ session_id: revisionSession, cursor })}`,
      ),
    enabled: !!cursor,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const next = cursor ? query.data?.next_cursor : group.next_trader_cursor;
  const location = useLocation();
  return (
    <details
      className="feed-card"
      data-group-id={group.id}
      data-revision-id={group.revision_id}
      open={reading.open}
      onToggle={(event) => {
        if (event.currentTarget.open !== reading.open)
          updateReading({ ...reading, open: event.currentTarget.open });
      }}
    >
      <summary className="feed-card-trigger">
        <div style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>
          <h3>
            {display(group.company)}{" "}
            <span className="ticker">{group.ticker ? display(group.ticker) : "证券未知"}</span>
          </h3>
          <p>
            实际交易日：{display(group.transaction_dates)} ·{" "}
            {display(group.owners)} 位主体 · {display(group.filings)} 份申报
          </p>
          {group._removed && (
            <p role="status">此组已修订，不再符合当前筛选。保留您正在阅读的原版本。</p>
          )}
          <TradeSummary summary={rows(group.summary)} primary />
          {Number(group.amendment_count) > 0 && (
            <small>
              历史交易修订 · {group.amendment_count} 条 ·
              详见原交易日期与核对状态
            </small>
          )}
        </div>
        <span className="text-link" style={{ flexShrink: 0, whiteSpace: "nowrap" }}>
          {reading.open ? "收起" : "展开"}
        </span>
      </summary>
      <div className="feed-company-link">
        <Link
          className="text-link"
          state={sourceContext(location)}
          to={`/companies/${group.company_id ?? group.issuer_id ?? group.id}`}
        >
          查看公司历史 →
        </Link>
        <small>
          本筛选最近 SEC 接受（美东）：
          <Timestamp value={group.accepted_at} />
        </small>
      </div>
      <ErrorNotice error={query.error} retry={query.refetch} />
      {cursor && query.isPending ? (
        <Loading />
      ) : (
        (cursor ? rows(query.data?.items) : rows(group.trader_groups)).map(
          (item) => (
            <TraderGroup
              key={item.id}
              item={item}
              session={revisionSession}
              readingSession={session}
              groupId={group.id}
            />
          ),
        )
      )}
      {(group.next_trader_cursor || page > 0) && (
        <div className="pagination">
          <Button
            disabled={!page || query.isFetching}
            onClick={() => updateReading({ ...reading, page: page - 1 })}
          >
            上一组明细
          </Button>
          <span>
            组内第 {page + 1} 页 · 共 {display(group.matching_transactions)} 条
          </span>
          <Button
            disabled={!next || query.isFetching}
            onClick={() => {
              updateReading({
                ...reading,
                cursors: [...cursors.slice(0, page + 1), next],
                page: page + 1,
              });
            }}
          >
            下一组明细
          </Button>
        </div>
      )}
      <small className="muted panel-footer">
        记录版本 {String(group.revision_id ?? "").slice(0, 8)}
        {group.revision_created_at && <> · 保存于 <Timestamp value={group.revision_created_at} /></>}
      </small>
      {group.coverage && (
        <p className="panel-footer">{display(group.coverage)}</p>
      )}
    </details>
  );
}
export function Feed() {
  const [params, setParams] = useContextSearchParams();
  const filter = params.get("filter") ?? "focus";
  const order = params.get("order") === "accepted" ? "accepted" : "transaction";
  const session = params.get("session_id") ?? "";
  const cursor = params.get("cursor") ?? "";
  const [epoch, setEpoch] = useState(requestId);
  const client = useQueryClient();
  const panel = useRef<HTMLElement>(null);
  const [view, setView] = useState<{ session: string; stream: FeedStream | undefined }>(() => ({ session, stream: savedReading(session).stream }));
  const stream = view.session === session ? view.stream : savedReading(session).stream;
  const streamRef = useRef(stream);
  streamRef.current = stream;
  const [busy, setBusy] = useState(false);
  const [updateError, setUpdateError] = useState<Error | null>(null);
  const [notice, setNotice] = useState("");
  const [failedOperation, setFailedOperation] = useState<"merge" | "more" | null>(null);
  const requestScope = `${filter}:${order}:${session || epoch}`;
  const scopeRef = useRef(requestScope);
  scopeRef.current = requestScope;
  const operation = useRef(0);
  const attemptedAutoVersion = useRef("");
  const inFlight = useRef(false);
  useEffect(() => {
    inFlight.current = false;
    attemptedAutoVersion.current = "";
    setBusy(false);
    setUpdateError(null);
    setFailedOperation(null);
    setNotice("");
    return () => { operation.current += 1; };
  }, [requestScope]);
  const anchor = useRef<{ id: string; top: number } | null>(null);
  const query = useQuery({
    queryKey: ["feed", filter, session || epoch, cursor, order],
    queryFn: async () => {
      const p = new URLSearchParams({ type: filter, order });
      if (session) p.set("session_id", session);
      if (cursor) p.set("cursor", cursor);
      return api<Fact>(`/feed?${p}`);
    },
    enabled: !stream,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    retry: 1,
  });
  const currentSession = session || String(query.data?.session_id ?? "");
  const latestSession = stream?.latestSession || currentSession;
  function persist(next: FeedStream, base = currentSession) {
    saveReading(base, { ...savedReading(base), stream: next });
    streamRef.current = next;
    setView({ session: base, stream: next });
  }
  useEffect(() => {
    if (!query.data || stream) return;
    const createdSession = String(query.data.session_id);
    const next = {
      groups: rows(query.data.groups).map((group) => ({ ...group, _session_id: createdSession })),
      latestSession: createdSession,
      nextCursor: query.data.next_cursor ?? null,
      pages: Math.max(1, Number(params.get("feed_page")) || 1),
      total: Number(query.data.total_groups) || 0,
    };
    persist(next, createdSession);
    // The server-issued URL version reuses this exact first response.
    client.setQueryData(["feed", filter, createdSession, cursor, order], query.data);
    if (!session || query.data.order !== order)
      setParams((previous) => {
        const updated = new URLSearchParams(previous);
        updated.set("session_id", createdSession);
        updated.set("feed_page", String(next.pages));
        updated.set("order", query.data!.order ?? order);
        return updated;
      }, { replace: true });
  }, [query.data, session, stream, filter, order, cursor, client, setParams]);
  const updates = useQuery({
    queryKey: ["feed-updates", latestSession],
    queryFn: () => api<FeedUpdates>(`/feed/updates?${new URLSearchParams({ session_id: latestSession })}`),
    enabled: !!latestSession,
    refetchInterval: () => (document.hidden ? 30000 : 5000),
    refetchOnWindowFocus: false,
    staleTime: 4000,
  });
  function captureAnchor() {
    const visible = [...(panel.current?.querySelectorAll<HTMLElement>("[data-group-id]") ?? [])]
      .find((node) => node.getBoundingClientRect().bottom > 100);
    if (visible) anchor.current = { id: visible.dataset.groupId!, top: visible.getBoundingClientRect().top };
  }
  useLayoutEffect(() => {
    const saved = anchor.current;
    if (!saved) return;
    const node = [...(panel.current?.querySelectorAll<HTMLElement>("[data-group-id]") ?? [])]
      .find((item) => item.dataset.groupId === saved.id);
    if (node) window.scrollBy(0, node.getBoundingClientRect().top - saved.top);
    anchor.current = null;
  }, [stream, updateError]);
  function reset(nextFilter = filter, nextOrder = order) {
    operation.current += 1;
    setBusy(false);
    setUpdateError(null);
    setNotice("");
    setEpoch(requestId());
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("filter", nextFilter);
      next.set("order", nextOrder);
      for (const key of ["session_id", "cursor", "feed_page"]) next.delete(key);
      return next;
    }, { replace: true });
  }
  async function prepareOpenDetails(incoming: Fact[], target: string) {
    // A revised summary and its expanded rows become readable together. Prime
    // only details the reader has actually opened; hidden cards create no work.
    for (const group of incoming) {
      const node = [...(panel.current?.querySelectorAll<HTMLElement>("[data-group-id]") ?? [])]
        .find((item) => item.dataset.groupId === String(group.id));
      if (!node?.hasAttribute("open")) continue;
      const reading = savedReading(currentSession).cards[group.id];
      const cursor = reading?.cursors[reading.page] ?? "";
      const jobs: Promise<unknown>[] = [];
      if (cursor) jobs.push(client.fetchQuery({
        queryKey: ["feed-group", target, group.id, cursor],
        queryFn: () => api<Fact>(`/feed/groups/${encodeURIComponent(group.id)}?${new URLSearchParams({ session_id: target, cursor })}`),
        staleTime: Infinity,
      }));
      for (const trader of node.querySelectorAll<HTMLElement>("[data-detail-open=true]")) {
        const traderKey = trader.dataset.traderId!;
        const detailCursor = trader.dataset.detailCursor ?? "";
        jobs.push(client.fetchQuery({
          queryKey: ["trader-detail", target, group.id, traderKey, detailCursor],
          queryFn: () => api<Fact>(`/feed/groups/${encodeURIComponent(group.id)}?${new URLSearchParams({ session_id: target, trader_key: traderKey, cursor: detailCursor })}`),
          staleTime: Infinity,
        }));
      }
      await Promise.all(jobs);
    }
  }
  async function applyUpdates() {
    if (inFlight.current || !streamRef.current || streamRef.current.recoveryUnknown || !latestSession) return;
    const scope = scopeRef.current;
    const request = ++operation.current;
    inFlight.current = true;
    if (updateError) captureAnchor();
    setBusy(true);
    setNotice("正在检查已保存的新申报…");
    setUpdateError(null);
    try {
      // Applied pages and their remaining cursor form one frozen delta. Keep
      // them together across failures/reloads before comparing newer versions.
      const pending = streamRef.current.pendingMerge;
      const baseSession = pending?.baseSession ?? latestSession;
      let target = pending?.targetSession ?? "";
      let nextCursor = pending?.cursor ?? "";
      let count = 0;
      do {
        const p = new URLSearchParams({ session_id: baseSession, include_groups: "true" });
        if (target) p.set("target_session_id", target);
        if (nextCursor) p.set("cursor", nextCursor);
        const data = await readFeedWithRetry(
          () => api<FeedUpdates>(`/feed/updates?${p}`),
          () => scopeRef.current === scope && operation.current === request,
        );
        if (!data) return;
        target = String(data.target_session_id);
        nextCursor = data.next_cursor ?? "";
        count = Number(data.new_count);
        await prepareOpenDetails(rows(data.groups), target);
        if (scopeRef.current !== scope || operation.current !== request) return;
        const current = streamRef.current!;
        captureAnchor();
        const removedIds = mergeRemovalIds(current.removedIds ?? [], data.removed_ids ?? [], rows(data.groups));
        persist({ ...current,
          removedIds,
          groups: mergeGroups(current.groups, rows(data.groups).map((group) => ({ ...group, _session_id: target })), "update", order, removedIds),
          latestSession: nextCursor ? current.latestSession : target,
          pendingMerge: nextCursor ? { baseSession, targetSession: target, cursor: nextCursor } : undefined,
        });
        if (!nextCursor) {
          // A newer announcement may have triggered this older continuation.
          // Completing it must not consume the newer version's merge attempt.
          if (pending) attemptedAutoVersion.current = "";
          client.setQueryData(["feed-updates", target], { ...data, new_count: 0, groups: [], changed_ids: [], removed_ids: [] });
          setNotice(count ? `已合并 ${count} 个公司日期组的更新，阅读位置已保留。` : "已检查，暂无新更新。");
        }
      } while (nextCursor);
    } catch (error) {
      if (scopeRef.current === scope && operation.current === request) {
        captureAnchor();
        setUpdateError(error instanceof Error ? error : new Error(String(error)));
        setFailedOperation("merge");
        setNotice("本次更新未全部合并，已有内容和阅读位置已保留；可点击重试再次合并。");
      }
    } finally {
      if (scopeRef.current === scope && operation.current === request) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  }
  useEffect(() => {
    if ((!Number(updates.data?.new_count) && !stream?.pendingMerge) || busy || document.hidden || !stream || stream.recoveryUnknown) return;
    const root = panel.current;
    const active = document.activeElement;
    const engaged = root?.querySelector("details[open]") || window.getSelection()?.toString() ||
      (active instanceof HTMLElement && root?.contains(active) && active !== document.body);
    const version = updates.data?.version ?? stream.pendingMerge?.targetSession ?? "";
    if (!engaged && window.scrollY < 160 && attemptedAutoVersion.current !== version) {
      attemptedAutoVersion.current = version;
      void applyUpdates();
    }
  }, [updates.data?.version, updates.data?.new_count, busy, stream?.pendingMerge]);
  async function loadMore() {
    const current = streamRef.current;
    if (!current?.nextCursor || inFlight.current) return;
    const scope = scopeRef.current;
    const request = ++operation.current;
    inFlight.current = true;
    if (updateError) captureAnchor();
    setBusy(true);
    setUpdateError(null);
    setNotice("正在加载更早的已保存申报…");
    try {
      const data = await api<Fact>(`/feed?${new URLSearchParams({ session_id: currentSession, type: filter, order, cursor: current.nextCursor })}`);
      if (scopeRef.current !== scope || operation.current !== request) return;
      const latest = streamRef.current!;
      persist({ ...latest,
        groups: mergeGroups(latest.groups, rows(data.groups).map((group) => ({ ...group, _session_id: currentSession })), "append", order, latest.removedIds ?? []),
        nextCursor: data.next_cursor ?? null,
        pages: latest.pages + 1,
      });
      setNotice(`已加载 ${latest.pages + 1} 批，已有内容和展开状态已保留。`);
    } catch (error) {
      if (scopeRef.current === scope && operation.current === request) {
        captureAnchor();
        setUpdateError(error instanceof Error ? error : new Error(String(error)));
        setFailedOperation("more");
        setNotice("加载未完成，已有内容和阅读位置已保留；可点击重试继续加载。");
      }
    } finally {
      if (scopeRef.current === scope && operation.current === request) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  }
  function retryFailedRead() {
    if (inFlight.current) return;
    if (updateError && failedOperation === "merge") void applyUpdates();
    else if (updateError && failedOperation === "more") void loadMore();
    else if (!stream) void query.refetch();
    else void updates.refetch();
  }
  const groups = stream?.groups ?? [];
  const pending = facts(updates.data?.pending_summary ?? query.data?.pending_summary);
  const previews = rows(updates.data?.pending_filings?.length ? updates.data.pending_filings : query.data?.pending_filings);
  return (
    <section className="panel feed-panel" ref={panel}>
      <div className="section-heading">
        <h2>Insider 信息流</h2>
        <label>排序 <select aria-label="信息流排序" value={order} onChange={(event) => reset(filter, event.target.value === "accepted" ? "accepted" : "transaction")}>
          <option value="transaction">最新实际交易</option><option value="accepted">最近披露</option>
        </select></label>
        <div className="button-row">
          <Button variant="ghost" disabled={busy || !stream || stream.recoveryUnknown} onClick={() => void applyUpdates()}>
            {busy ? "正在更新…" : stream?.pendingMerge ? "继续合并更新" : Number(updates.data?.new_count) > 0 ? `有 ${updates.data!.new_count} 条更新 · 查看` : "刷新列表"}
          </Button>
        </div>
      </div>
      <div className="feed-filters" aria-label="交易行为筛选">
        {filters.map((item) => <button key={item.id} className={`filter-button ${filter === item.id ? "selected" : ""}`} aria-pressed={filter === item.id} onClick={() => reset(item.id)}>{item.label}</button>)}
      </div>
      <p className="panel-body muted" role="status" aria-live="polite">{notice || (stream?.pendingMerge ? "上次更新尚未全部合并，可继续合并；已有内容和阅读位置已保留。" : Number(updates.data?.new_count) ? "新申报已保存，查看更新会保留当前阅读位置。" : "新申报按公司日期组更新，已展开内容会保留。")}</p>
      {stream?.recoveryUnknown && <div className="panel-body">
        <p>无法核对旧版阅读缓存的更新进度。已有内容和展开状态已保留；开始新阅读后才能继续合并更新。</p>
        <Button disabled={busy} onClick={() => reset()}>开始新阅读</Button>
      </div>}
      <ErrorNotice error={updateError ?? query.error ?? updates.error} retry={retryFailedRead} />
      {Number(pending.total) > 0 && <details className="result-notes">
        <summary>本地有 {pending.total} 份申报尚未解析 · 展开查看真实阶段</summary>
        {rows(pending.stages).map((stage) => <p key={stage.id}>{stage.label}：{stage.count} 份</p>)}
        <p>{display(pending.scope)}</p>
        {previews.length > 0 && <p>上次内容刷新时的 {previews.length} 份预览（不代表总数；阶段总量以上方最近检查为准）：</p>}
        {previews.map((item) => <p key={item.accession}>{display(item.company ?? item.accession)} · {item.form} · SEC 接受 <Timestamp value={item.accepted_at} /> · {item.status}</p>)}
        <Link className="text-link" to="/data?tab=tasks&status=waiting">查看来源任务 →</Link>
      </details>}
      {!stream && query.isPending ? <Loading /> : groups.length ? <div className="feed-content">
        {groups.map((group) => <FeedCard key={group.id} group={group} session={currentSession} />)}
      </div> : <EmptyState title="当前筛选下尚无本地申报" description="获取最新申报或回补历史后，在这里按公司阅读；已有覆盖不足会单独标记。"><Link className="text-link" to="/data">查看数据覆盖与获取进度 →</Link></EmptyState>}
      <p className="panel-footer">展示本地已解析记录；具体历史范围以索引核对和剩余缺口为准。每张卡与逐笔详情使用其标明的记录版本。</p>
      <div className="pagination">
        <span>已显示 {groups.length} 个公司日期组 · {stream?.pages ?? 1} 批</span>
        <Button disabled={!stream?.nextCursor || busy} onClick={() => void loadMore()}>{busy ? "正在加载…" : stream?.nextCursor ? "加载更多" : "已到本次阅读末尾"}</Button>
      </div>
    </section>
  );
}
