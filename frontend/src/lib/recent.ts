/**
 * Companies and insiders opened recently, kept in this browser only
 * (shown under the Insider search box; the reader can clear them).
 */
export type RecentEntity = { kind: "company" | "person"; id: string; name: string; ticker?: string | null };

const KEY = "iirp.insider.recent";
const MAX = 10;

export function readRecent(): RecentEntity[] {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((item) => item && item.id && item.name).slice(0, MAX) : [];
  } catch {
    return [];
  }
}

export function rememberEntity(item: RecentEntity) {
  try {
    const rest = readRecent().filter((entry) => !(entry.kind === item.kind && entry.id === item.id));
    localStorage.setItem(KEY, JSON.stringify([item, ...rest].slice(0, MAX)));
  } catch {
    /* Optional convenience. */
  }
}

export function clearRecent() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* Optional convenience. */
  }
}
