/**
 * Whether CME equity index futures (ES=F, NQ=F) are trading at a moment.
 *
 * Globex trades them from Sunday 18:00 to Friday 17:00 US Eastern time, with a
 * daily pause from 17:00 to 18:00. Exchange holidays close or shorten single
 * days, and the quote source keeps reporting a whole calendar day as the
 * "session" over weekends, so the last trade time decides as well: a quote
 * whose last trade is older than ``STALE_MINUTES`` is a closed market, not a
 * live one. Only then would the strip show a stale 0.00% change.
 */
export const STALE_MINUTES = 30;

function eastern(at: Date) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(at);
  const value = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  const day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(value("weekday"));
  return { day, minutes: Number(value("hour")) * 60 + Number(value("minute")) };
}

/** Inside the weekly Globex window and outside the daily pause (US Eastern time). */
export function globexOpen(at: Date) {
  const { day, minutes } = eastern(at);
  const evening = minutes >= 18 * 60;
  const pause = minutes >= 17 * 60 && minutes < 18 * 60;
  if (day === 6) return false;
  if (day === 0) return evening;
  if (day === 5) return minutes < 17 * 60;
  return !pause;
}

/** Trading now: the Globex window is open and the last trade is recent. */
export function indexFuturesTrading(at: Date, lastTrade: string | null | undefined) {
  if (!globexOpen(at)) return false;
  if (!lastTrade) return false;
  const age = at.getTime() - Date.parse(lastTrade);
  return Number.isFinite(age) && age <= STALE_MINUTES * 60_000;
}
