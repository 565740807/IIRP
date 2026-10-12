import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { flexRender, type Table, type VisibilityState } from "@tanstack/react-table";
import { Columns3, RotateCcw } from "lucide-react";
import { columnVisibility, useColumnChoices } from "@/lib/viewPrefs";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

export type ColumnOption = { id: string; label: ReactNode; shown: boolean };

/**
 * The "Columns" button at the right of a table header: every optional column
 * with a check box, and a way back to the default set. Choices are kept in
 * this browser per table (``useTableColumns``).
 */
export function ColumnSettings({ columns, onChange, onReset, changed, className }: {
  columns: ColumnOption[];
  onChange: (id: string, shown: boolean) => void;
  onReset: () => void;
  changed: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const hidden = columns.filter((column) => !column.shown).length;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-column-settings
          className={cn(
            "inline-flex h-7 shrink-0 items-center gap-1 rounded-md border bg-card px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground",
            className,
          )}
        >
          <Columns3 className="size-3.5" />
          {t("ui.view.columns")}
          {hidden > 0 && <span className="rounded bg-secondary px-1 tabular-nums">+{hidden}</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent data-column-menu className="w-60 p-2">
        <p className="px-1.5 pb-1.5 text-xs text-muted-foreground">{t("ui.view.columns_hint")}</p>
        <ul className="max-h-80 overflow-y-auto">
          {columns.map((column) => (
            <li key={column.id}>
              <label className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-accent">
                <input
                  type="checkbox"
                  className="accent-primary"
                  checked={column.shown}
                  onChange={(event) => onChange(column.id, event.target.checked)}
                />
                <span className="min-w-0 flex-1">{column.label}</span>
              </label>
            </li>
          ))}
        </ul>
        <button
          type="button"
          disabled={!changed}
          onClick={onReset}
          className="mt-1 inline-flex w-full items-center gap-1.5 rounded border-t px-1.5 pt-2 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          <RotateCcw className="size-3" />
          {t("ui.view.columns_reset")}
        </button>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Column visibility of a TanStack table, remembered in this browser under
 * ``table``. ``hidden`` lists the columns that start in the column settings.
 * Returns the visibility state for ``useReactTable`` and a ready settings button.
 */
export function useTableColumns(table: string, hidden: readonly string[]) {
  const choices = useColumnChoices(table, hidden);
  const state = (ids: readonly string[]): VisibilityState => columnVisibility(ids, hidden, choices.choices);
  const settings = <T,>(instance: Table<T>, className?: string) => {
    const columns = instance.getAllLeafColumns().filter((column) => column.getCanHide());
    const headers = new Map(instance.getFlatHeaders().map((header) => [header.column.id, header]));
    // The menu names a column by its meta label, else its header as the table draws it.
    const label = (column: (typeof columns)[number]) => {
      const meta = (column.columnDef.meta as { label?: ReactNode } | undefined)?.label;
      if (meta != null) return meta;
      const header = headers.get(column.id);
      return header && column.columnDef.header ? flexRender(column.columnDef.header, header.getContext()) : column.id;
    };
    return (
      <ColumnSettings
        className={className}
        changed={choices.changed}
        onReset={choices.reset}
        onChange={choices.set}
        columns={columns.map((column) => ({
          id: column.id,
          shown: column.getIsVisible(),
          label: label(column),
        }))}
      />
    );
  };
  return { ...choices, state, settings };
}
