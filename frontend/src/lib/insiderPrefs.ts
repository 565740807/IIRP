/**
 * Choices on the Insider pages: the range of a company/person page and
 * the n of before/after-n sessions. A choice applies at once, is kept in
 * the link (?range=…&n=…) and remembered in this browser per page type; the
 * defaults come from the server (config files).
 */
export type PageKind = "company" | "person";

export type Range =
  | { mode: "months"; months: number }
  | { mode: "count"; count: number }
  | { mode: "dates"; start: string; end: string };

export const DEFAULT_RANGE: Range = { mode: "months", months: 6 };
export const DEFAULT_N = 5;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function parseRange(value: string | null | undefined): Range | null {
  if (!value) return null;
  let match = /^(\d{1,3})m$/.exec(value);
  if (match && Number(match[1]) >= 1 && Number(match[1]) <= 120) return { mode: "months", months: Number(match[1]) };
  match = /^(\d{1,3})t$/.exec(value);
  if (match && Number(match[1]) >= 1 && Number(match[1]) <= 500) return { mode: "count", count: Number(match[1]) };
  const [start, end] = value.split("..");
  if (start && end && DATE.test(start) && DATE.test(end) && start <= end) return { mode: "dates", start, end };
  return null;
}

export function rangeParam(range: Range) {
  return range.mode === "months" ? `${range.months}m` : range.mode === "count" ? `${range.count}t` : `${range.start}..${range.end}`;
}

export function sameRange(a: Range, b: Range) {
  return rangeParam(a) === rangeParam(b);
}

/** Today in US Eastern time, as the SEC and the exchange count days. */
export function todayEt(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
}

/** Calendar date `months` before `day` (end of month clamps: Aug 31 − 6 → Feb 28). */
export function monthsBefore(day: string, months: number) {
  const [y, m, d] = day.split("-").map(Number);
  const target = new Date(Date.UTC(y, m - 1 - months, 1));
  const last = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  target.setUTCDate(Math.min(d, last));
  return target.toISOString().slice(0, 10);
}

/** Query parameters of the entity history for a range. */
export function rangeQuery(range: Range, today = todayEt()) {
  if (range.mode === "count") return { recent_count: range.count };
  if (range.mode === "months") return { start_date: monthsBefore(today, range.months), end_date: today };
  return { start_date: range.start, end_date: range.end };
}

function read(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* Optional: the link still carries the choice. */
  }
}

export function savedRange(kind: PageKind): Range | null {
  return parseRange(read(`iirp.insider.range.${kind}`));
}

export function saveRange(kind: PageKind, range: Range | null) {
  write(`iirp.insider.range.${kind}`, range ? rangeParam(range) : null);
}

export function parseN(value: string | null | undefined, max = 60): number | null {
  if (!value || !/^\d{1,2}$/.test(value)) return null;
  const n = Number(value);
  return n >= 1 && n <= max ? n : null;
}

/** The Insider feature's n, shared by the home feed, company, person and transaction pages. */
export function savedN(): number | null {
  return parseN(read("iirp.insider.n"));
}

export function saveN(n: number | null) {
  write("iirp.insider.n", n === null ? null : String(n));
}
