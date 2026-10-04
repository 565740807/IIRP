import type { Fact } from "./api";

// Keep old nodes and their order. Append from the original immutable cursor;
// insert new groups by the selected source date without moving an existing card.
export function mergeGroups(current: Fact[], incoming: Fact[], mode: "append" | "update", order: string, removed: string[] = []) {
  const byId = new Map(current.map((group) => [String(group.id), group]));
  for (const group of incoming) {
    const old = byId.get(String(group.id));
    if (!old || (mode === "update" && (!old.revision_created_at || !group.revision_created_at || String(group.revision_created_at) >= String(old.revision_created_at))))
      byId.set(String(group.id), group);
  }
  for (const id of removed) {
    const old = byId.get(id);
    if (old) byId.set(id, { ...old, _removed: true });
  }
  const result = current.map((group) => byId.get(String(group.id))!);
  const existing = new Set(current.map((group) => String(group.id)));
  const added = [...byId.values()].filter((group) => !existing.has(String(group.id)));
  const dateKey = (group: Fact) => order === "accepted" ? String(group.accepted_at ?? "") : String((group.transaction_dates ?? []).at(-1) ?? "");
  for (const group of added) {
    const index = mode === "append" ? -1 : result.findIndex((old) => dateKey(old) < dateKey(group));
    if (index < 0) result.push(group);
    else result.splice(index, 0, group);
  }
  return result;
}

export function mergeRemovalIds(current: string[], removed: string[], incoming: Fact[]): string[] {
  const reappeared = new Set(incoming.map((group) => String(group.id)));
  return [...new Set([...current, ...removed])].filter((id) => !reappeared.has(id));
}
