/** Presentation decisions use persisted counts and stages, never elapsed-time guesses. */
export function taskEntryCategory(counts: Record<string, number> = {}) {
  return (counts.running ?? 0) + (counts.waiting ?? 0) > 0
    ? "active"
    : (counts.attention ?? 0) > 0 ? "attention" : "active";
}

export function taskSource(kind: string) {
  if (kind.startsWith("sec_")) return "SEC";
  if (kind === "earnings") return "SEC / 公告来源 / Yahoo";
  if (["market_history", "market_quotes", "event_dates"].includes(kind)) return "Yahoo / 本地计算";
  return "本地维护";
}

export function taskStage(stage: unknown, kind: string, status: string) {
  const current = String(stage ?? "");
  if (current && !["等待可执行任务", "等待依赖", "等待执行", "QUEUED", "WAITING"].includes(current)) return current;
  if (status === "READY" || status === "SUCCEEDED") return "所选范围已完成";
  if (status === "PAUSED") return "已暂停，已完成内容保留";
  if (status === "CANCELLED") return "已停止后续工作，已完成内容保留";
  if (["FAILED", "PARTIAL"].includes(status)) return "部分资料需要核对，请展开查看原因";
  return kind.startsWith("sec_") ? "正在安排 SEC 申报核对" : "正在安排所选范围";
}

/** A UI-only page number must not create another fetch for the same snapshot. */
export function entityRequestParams(params: URLSearchParams) {
  const request = new URLSearchParams(params);
  request.delete("entity_page");
  request.sort();
  return request.toString();
}

/** Immutable entity snapshots are unaffected by unrelated background progress. */
export function isFrozenEntityQuery(key: readonly unknown[]) {
  return key[0] === "entity" && ["company", "person"].includes(String(key[1])) &&
    new URLSearchParams(String(key[3] ?? "")).has("cursor");
}

export function acquiredSessionsText(acquired: unknown, expected: unknown) {
  if (typeof acquired !== "number" || !Number.isFinite(acquired)) return null;
  return `已获取 ${acquired.toLocaleString("zh-CN")} 个有效交易日` +
    (typeof expected === "number" && Number.isFinite(expected) && expected > 0
      ? ` / 所需 ${expected.toLocaleString("zh-CN")} 日` : "");
}

/** The first response gives the canonical, immutable reading URL. */
export function entitySnapshotParams(
  params: URLSearchParams,
  sessionId: unknown,
  query: { status: string; fetchStatus: string },
) {
  // A background check can expose cached data before its new session arrives.
  // Keep that data readable, but only freeze the completed successful response.
  if (query.status !== "success" || query.fetchStatus !== "idle") return null;
  if (typeof sessionId !== "string" || !sessionId || params.get("cursor")) return null;
  const next = new URLSearchParams(params);
  next.set("cursor", `${sessionId}:0`);
  next.set("entity_page", "0");
  return next;
}
