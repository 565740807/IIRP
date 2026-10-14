import type { ReactNode } from "react";
import { flexRender, type Table, type VisibilityState } from "@tanstack/react-table";
import { columnVisibility, useColumnChoices } from "@/lib/viewPrefs";
import { ColumnSettings } from "./column-settings";

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
