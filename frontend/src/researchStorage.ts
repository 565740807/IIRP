import {
  useCallback,
  useEffect,
  useRef,
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

// Large source documents exceed localStorage quotas. IndexedDB stores drafts
// independently of lightweight recent-research URLs, with visible write errors.
let database: Promise<IDBDatabase> | undefined;
const memoryDrafts = new Map<string, unknown>();
const memoryWrites = new Map<string, object>();
function rememberDraft(key: string, value: unknown) {
  const token = {};
  memoryDrafts.set(key, value);
  memoryWrites.set(key, token);
  return token;
}
function preserveDraftFields(
  value: unknown,
  previous: unknown,
  fields: readonly string[],
) {
  if (!fields.length || !value || typeof value !== "object" || Array.isArray(value) ||
      !previous || typeof previous !== "object" || Array.isArray(previous)) return value;
  return {
    ...value,
    ...Object.fromEntries(fields.filter(field => Object.prototype.hasOwnProperty.call(previous, field))
      .map(field => [field, (previous as Record<string, unknown>)[field]])),
  };
}
const draftTab = (() => {
  try {
    const value =
      sessionStorage.getItem("iirp:draft-tab") ?? crypto.randomUUID();
    sessionStorage.setItem("iirp:draft-tab", value);
    return value;
  } catch {
    return crypto.randomUUID();
  }
})();
function openDrafts() {
  return (database ??= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("iirp-research", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("drafts");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  }));
}
export async function writeDraft(key: string, value: unknown, preserveFields: readonly string[] = []) {
  const token = rememberDraft(key, preserveDraftFields(value, memoryDrafts.get(key), preserveFields));
  const db = await openDrafts();
  return new Promise<void>((resolve, reject) => {
    const transaction = db.transaction("drafts", "readwrite");
    const store = transaction.objectStore("drafts");
    let saved = value;
    const put = () => {
      store.put(saved, `${key}:${draftTab}`);
      store.put({ draftReference: `${key}:${draftTab}` }, key);
    };
    if (preserveFields.length) {
      // Read and merge in the same write transaction as the save. A completed
      // command may have changed these fields since this render's snapshot.
      const request = store.get(`${key}:${draftTab}`);
      request.onsuccess = () => {
        saved = preserveDraftFields(value, request.result, preserveFields);
        put();
      };
    } else put();
    transaction.oncomplete = () => {
      // A later optimistic edit/command already owns the memory view. An older
      // transaction completing must not replace that newer pending write.
      if (memoryWrites.get(key) === token) memoryDrafts.set(key, saved);
      resolve();
    };
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}
/** Prepare a destination only when this tab has no saved draft there yet. */
export async function initializeDraft(key: string, value: unknown) {
  const token = memoryDrafts.has(key) ? undefined : rememberDraft(key, value);
  const db = await openDrafts();
  return new Promise<void>((resolve, reject) => {
    const transaction = db.transaction("drafts", "readwrite");
    const store = transaction.objectStore("drafts");
    const request = store.openCursor(`${key}:${draftTab}`);
    let saved: unknown;
    request.onsuccess = () => {
      saved = request.result?.value;
      if (!request.result) {
        // Also respect a newer in-memory edit waiting for its own transaction.
        saved = memoryDrafts.has(key) ? memoryDrafts.get(key) : value;
        store.put(saved, `${key}:${draftTab}`);
        store.put({ draftReference: `${key}:${draftTab}` }, key);
      }
    };
    transaction.oncomplete = () => {
      if (token && memoryWrites.get(key) === token) memoryDrafts.set(key, saved);
      resolve();
    };
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}
// Async commands update only their own state, never an old whole-page snapshot.
export async function patchDraft<T>(key: string, update: (value: T) => T) {
  const token = memoryDrafts.has(key)
    ? rememberDraft(key, update(memoryDrafts.get(key) as T)) : undefined;
  const db = await openDrafts();
  return new Promise<void>((resolve, reject) => {
    const transaction = db.transaction("drafts", "readwrite");
    const store = transaction.objectStore("drafts");
    const request = store.get(`${key}:${draftTab}`);
    let updated: T | undefined;
    request.onsuccess = () => {
      if (request.result) {
        updated = update(request.result);
        store.put(updated, `${key}:${draftTab}`);
      }
    };
    transaction.oncomplete = () => {
      if (token && memoryWrites.get(key) === token && updated !== undefined)
        memoryDrafts.set(key, updated);
      // A newer optimistic command may already own this workspace while its
      // write waits behind this transaction. Notify that current view, rather
      // than letting an older commit restore/clear the previous command in UI.
      const visible = memoryDrafts.has(key) ? memoryDrafts.get(key) : updated;
      window.dispatchEvent(
        new CustomEvent("iirp:draft-command", {
          detail: { key, value: visible },
        }),
      );
      resolve();
    };
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}
export function useStoredDraft<T>(
  key: string,
  value: T,
  restore: (value: T) => void,
  preserveFields: readonly string[] = [],
) {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const restoreRef = useRef(restore);
  const serialized = JSON.stringify(value);
  const preservedFields = JSON.stringify(preserveFields);
  restoreRef.current = restore;
  useEffect(() => {
    let active = true;
    if (memoryDrafts.has(key)) {
      restoreRef.current(memoryDrafts.get(key) as T);
      setReady(true);
      return () => {
        active = false;
      };
    }
    void openDrafts()
      .then(
        (db) =>
          new Promise<T | undefined>((resolve, reject) => {
            const store = db.transaction("drafts").objectStore("drafts");
            const request = store.get(`${key}:${draftTab}`);
            request.onsuccess = () => {
              if (request.result) {
                resolve(request.result);
                return;
              }
              const fallback = store.get(key);
              fallback.onsuccess = () => {
                if (fallback.result?.draftReference) {
                  const latest = store.get(fallback.result.draftReference);
                  latest.onsuccess = () => resolve(latest.result);
                  latest.onerror = () => reject(latest.error);
                } else resolve(fallback.result);
              };
              fallback.onerror = () => reject(fallback.error);
            };
            request.onerror = () => reject(request.error);
          }),
      )
      .then((stored) => {
        if (!active) return;
        if (stored) {
          memoryDrafts.set(key, stored);
          restoreRef.current(stored);
        }
        setReady(true);
      })
      .catch(() => {
        if (!active) return;
        if (memoryDrafts.has(key))
          restoreRef.current(memoryDrafts.get(key) as T);
        setError(
          new Error(
            "浏览器无法恢复草稿。当前输入仍可使用，请保留原始资料并检查浏览器存储空间。",
          ),
        );
        setReady(true);
      });
    return () => {
      active = false;
    };
  }, [key]);
  useEffect(() => {
    if (ready)
      void writeDraft(key, JSON.parse(serialized), JSON.parse(preservedFields)).catch(() =>
        setError(
          (previous) =>
            previous ??
            new Error(
              "草稿未能写入浏览器存储。请保留原文，释放浏览器存储空间后刷新重试。",
            ),
        ),
      );
  }, [key, serialized, preservedFields, ready]);
  return { ready, error };
}
