/**
 * Display choices remembered in this browser only: which columns a table shows
 * and which page sections are folded. Nothing here reaches the server; a
 * restricted storage simply falls back to the defaults.
 */
import { useCallback, useEffect, useState } from "react";

type Store = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function browserStore(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function read(key: string, store: Store | null): unknown {
  try {
    return JSON.parse(store?.getItem(key) ?? "null");
  } catch {
    return null;
  }
}

function write(key: string, value: unknown, store: Store | null) {
  try {
    if (value == null) store?.removeItem(key);
    else store?.setItem(key, JSON.stringify(value));
  } catch {
    /* Optional convenience. */
  }
}

export const columnsKey = (table: string) => `iirp.columns.${table}`;
export const foldKey = (section: string) => `iirp.fold.${section}`;

/** Column id → shown, for the columns a reader changed; the rest follow the table's defaults. */
export type ColumnChoices = Record<string, boolean>;

export function readColumnChoices(table: string, store: Store | null = browserStore()): ColumnChoices {
  const value = read(columnsKey(table), store);
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([, shown]) => typeof shown === "boolean")) as ColumnChoices;
}

/** Saves only the choices that differ from the defaults, so later defaults still apply to untouched columns. */
export function writeColumnChoices(table: string, choices: ColumnChoices, hiddenByDefault: readonly string[], store: Store | null = browserStore()) {
  const changed = Object.fromEntries(Object.entries(choices).filter(([id, shown]) => shown === hiddenByDefault.includes(id)));
  write(columnsKey(table), Object.keys(changed).length ? changed : null, store);
}

/** Visibility of every column: the reader's choice, else shown unless hidden by default. */
export function columnVisibility(ids: readonly string[], hiddenByDefault: readonly string[], choices: ColumnChoices) {
  return Object.fromEntries(ids.map((id) => [id, choices[id] ?? !hiddenByDefault.includes(id)])) as Record<string, boolean>;
}

/**
 * The visibility state of one table, remembered under its id. ``hiddenByDefault``
 * lists the columns that start in the column settings instead of the table.
 */
export function useColumnChoices(table: string, hiddenByDefault: readonly string[]) {
  const [choices, setChoices] = useState<ColumnChoices>(() => readColumnChoices(table));
  useEffect(() => setChoices(readColumnChoices(table)), [table]);
  const defaults = hiddenByDefault.join(",");
  const visible = useCallback(
    (id: string) => choices[id] ?? !defaults.split(",").includes(id),
    [choices, defaults],
  );
  const set = useCallback((id: string, shown: boolean) => {
    setChoices((current) => {
      const next = { ...current, [id]: shown };
      writeColumnChoices(table, next, defaults.split(","));
      return next;
    });
  }, [table, defaults]);
  const reset = useCallback(() => {
    writeColumnChoices(table, {}, []);
    setChoices({});
  }, [table]);
  return { choices, visible, set, reset, changed: Object.keys(choices).some((id) => choices[id] === defaults.split(",").includes(id)) };
}

export function readFold(section: string, fallback: boolean, store: Store | null = browserStore()) {
  const value = read(foldKey(section), store);
  return typeof value === "boolean" ? value : fallback;
}

export function writeFold(section: string, open: boolean, fallback: boolean, store: Store | null = browserStore()) {
  write(foldKey(section), open === fallback ? null : open, store);
}

/** Whether a foldable section is open, remembered under its id (``fallback`` when never changed). */
export function useFold(section: string, fallback: boolean) {
  const [open, setOpen] = useState(() => readFold(section, fallback));
  const toggle = useCallback((next?: boolean) => {
    setOpen((current) => {
      const value = next ?? !current;
      writeFold(section, value, fallback);
      return value;
    });
  }, [section, fallback]);
  return [open, toggle] as const;
}
