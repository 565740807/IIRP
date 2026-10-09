/** /system is cheap; the backend refreshes expensive inventory only after its TTL. */
export function systemRefreshInterval(status: unknown): number {
  return status === "fresh" ? 60_000 : 5_000;
}
