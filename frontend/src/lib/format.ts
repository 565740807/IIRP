import i18n, { locale } from "@/i18n";

/**
 * Display formatting in the current language. These only format numbers the
 * backend computed; no financial calculation happens here.
 */
const ET = "America/New_York";
const MINUS = "−";

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function formatNumber(value: unknown, digits = 2) {
  const n = toNumber(value);
  return n === null ? "—" : new Intl.NumberFormat(locale(), { maximumFractionDigits: digits }).format(n);
}

/** 20,678,630 → "20.68M" (en) / "2,068万" (zh); small counts stay exact. */
export function formatCompact(value: unknown) {
  const n = toNumber(value);
  if (n === null) return "—";
  if (Math.abs(n) < 10_000) return formatNumber(n, 0);
  return new Intl.NumberFormat(locale(), { notation: "compact", maximumFractionDigits: 2 }).format(n);
}

/** A dollar amount, compact above 10,000; sign only when asked for. */
export function formatMoney(value: unknown, { signed = false, currency }: { signed?: boolean; currency?: string | null } = {}) {
  const n = toNumber(value);
  if (n === null) return "—";
  const absolute = Math.abs(n);
  const text = new Intl.NumberFormat(locale(), {
    style: "currency",
    currency: currency || "USD",
    currencyDisplay: "narrowSymbol",
    notation: absolute >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: absolute >= 10_000 ? 2 : absolute >= 100 ? 0 : 2,
  }).format(absolute);
  if (!signed || n === 0) return n < 0 ? MINUS + text : text;
  return (n > 0 ? "+" : MINUS) + text;
}

/** Percent already in percent units (quotes): 0.58 → "+0.58%". Always signed. */
export function formatSignedPercent(value: unknown) {
  const n = toNumber(value);
  if (n === null) return "—";
  const text = new Intl.NumberFormat(locale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(n));
  return `${n > 0 ? "+" : n < 0 ? MINUS : ""}${text}%`;
}

export function formatSigned(value: unknown, digits = 2) {
  const n = toNumber(value);
  if (n === null) return "—";
  const text = new Intl.NumberFormat(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Math.abs(n));
  return `${n > 0 ? "+" : n < 0 ? MINUS : ""}${text}`;
}

/** Calendar date "2026-10-05" → "Oct 5" or "Oct 5, 2025" (other years). No time zone shift. */
export function formatDay(value: string | null | undefined, { year }: { year?: boolean } = {}) {
  if (!value || !/^\d{4}-\d{2}-\d{2}/.test(value)) return value ?? "—";
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const showYear = year ?? y !== new Date().getUTCFullYear();
  return new Intl.DateTimeFormat(locale(), {
    month: "short", day: "numeric", ...(showYear ? { year: "numeric" } : {}), timeZone: "UTC",
  }).format(date);
}

/** "Oct 2 – 5" for a two-date range. */
export function formatDayRange(days: readonly string[]) {
  if (!days.length) return "—";
  if (days.length === 1 || days[0] === days[days.length - 1]) return formatDay(days[0]);
  return `${formatDay(days[0])} – ${formatDay(days[days.length - 1])}`;
}

/** An instant in US Eastern time with the zone named: "Oct 6, 8:53 PM ET". */
export function formatEt(value: string | null | undefined, { date = true }: { date?: boolean } = {}) {
  if (!value) return "—";
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  const text = new Intl.DateTimeFormat(locale(), {
    ...(date ? { month: "short", day: "numeric" } : {}),
    hour: "numeric", minute: "2-digit", hour12: i18n.language !== "zh", timeZone: ET,
  }).format(instant);
  return `${text} ${i18n.t("ui.time.et")}`;
}

/** The same instant in the reader's own time zone, for tooltips. */
export function formatLocal(value: string | null | undefined) {
  if (!value) return "—";
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  return new Intl.DateTimeFormat(locale(), { dateStyle: "medium", timeStyle: "short" }).format(instant);
}

/** "3 min ago" relative to now; used next to update times. */
export function formatAgo(value: string | null | undefined, now = Date.now()) {
  if (!value) return "—";
  const seconds = Math.round((new Date(value).getTime() - now) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  const format = new Intl.RelativeTimeFormat(locale(), { numeric: "auto" });
  const abs = Math.abs(seconds);
  if (abs < 60) return format.format(Math.round(seconds), "second");
  if (abs < 3600) return format.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return format.format(Math.round(seconds / 3600), "hour");
  return format.format(Math.round(seconds / 86400), "day");
}
