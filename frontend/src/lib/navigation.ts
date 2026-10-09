/** Where a page was opened from (for its back link), and the last opened analysis. */
export function sourceContext(location: { pathname: string; search: string; state: unknown }) {
  return { from: location.pathname + location.search, fromState: location.state };
}

export function sourceHref(state: unknown, fallback: string) {
  const from = (state as { from?: unknown } | null)?.from;
  return typeof from === "string" && from.startsWith("/") && !from.startsWith("//") ? from : fallback;
}

const LAST = "iirp.analysis.last";

/** The analysis tab's link: the research last opened there, else the empty page. */
export function analysisHref(kind: string) {
  try {
    const value = localStorage.getItem(`${LAST}.${kind}`);
    if (value?.startsWith(`/analysis/${kind}?`)) return value;
  } catch {
    /* Navigation still works when browser storage is disabled. */
  }
  return `/analysis/${kind}`;
}

export function rememberAnalysis(kind: string, search: string) {
  try {
    if (search) localStorage.setItem(`${LAST}.${kind}`, `/analysis/${kind}?${search}`);
    else localStorage.removeItem(`${LAST}.${kind}`);
  } catch {
    /* Optional convenience. */
  }
}
