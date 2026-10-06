import {
  useCallback,
  useEffect,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { useLocation, useSearchParams } from "react-router-dom";

export function useContextSearchParams() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const set = useCallback(
    (
      next: Parameters<typeof setParams>[0],
      options?: Parameters<typeof setParams>[1],
    ) => setParams(next, { state: location.state, ...options }),
    [setParams, location.state],
  );
  return [params, set] as const;
}

export function sourceContext(location: {
  pathname: string;
  search: string;
  state: unknown;
}) {
  return {
    from: location.pathname + location.search,
    fromState: location.state,
  };
}
export function sourceHref(state: unknown, fallback: string) {
  const from = (state as { from?: unknown } | null)?.from;
  return typeof from === "string" &&
    from.startsWith("/") &&
    !from.startsWith("//")
    ? from
    : fallback;
}

export function useReadingState<T>(
  key: string,
  initial: T,
): [T, Dispatch<SetStateAction<T>>] {
  const read = () => {
    try {
      const saved = localStorage.getItem(`iirp:reading:${key}`);
      return saved == null ? initial : (JSON.parse(saved) as T);
    } catch {
      return initial;
    }
  };
  const [state, setState] = useState(() => ({ key, value: read() }));
  const value = state.key === key ? state.value : read();
  useEffect(() => {
    if (state.key === key) {
      try {
        localStorage.setItem(
          `iirp:reading:${key}`,
          JSON.stringify(state.value),
        );
      } catch {
        /* Optional reading preferences. */
      }
    }
  }, [key, state]);
  return [
    value,
    (next) =>
      setState((old) => ({
        key,
        value:
          typeof next === "function"
            ? (next as (value: T) => T)(old.key === key ? old.value : read())
            : next,
      })),
  ];
}

export function yearList(value: string | null): number[] {
  return (value ?? "")
    .split(/[\s,，]+/)
    .filter(Boolean)
    .map(Number);
}
export function researchHref(kind: string, fallback = "") {
  try {
    const value = localStorage.getItem(`iirp:last-research:${kind}`);
    if (value?.startsWith(`/analysis/${kind}?`)) {
      const params = new URLSearchParams(value.split("?")[1]);
      params.delete("result_ids");
      return `/analysis/${kind}?${params}`;
    }
  } catch {
    /* Navigation still works when browser storage is disabled. */
  }
  return `/analysis/${kind}${fallback ? `?${fallback}` : ""}`;
}
export function rememberResearch(kind: string, query: string) {
  try {
    localStorage.setItem(
      `iirp:last-research:${kind}`,
      `/analysis/${kind}?${query}`,
    );
  } catch {
    /* Server-side recent requests remain available. */
  }
}
export function serializeResearch(values: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (
      key !== "request_id" &&
      value != null &&
      value !== "" &&
      !(Array.isArray(value) && !value.length)
    )
      params.set(
        key,
        Array.isArray(value)
          ? value.join(",")
          : typeof value === "object"
            ? JSON.stringify(value)
            : String(value),
      );
  }
  return params;
}
