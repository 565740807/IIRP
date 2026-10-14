import { useTranslation } from "react-i18next";
import { RotateCcw } from "lucide-react";
import type { ColumnOption } from "./column-settings";

export function ColumnMenu({ columns, onChange, onReset, changed }: {
  columns: ColumnOption[]; onChange: (id: string, shown: boolean) => void;
  onReset: () => void; changed: boolean;
}) {
  const { t } = useTranslation();
  return <>
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
  </>;
}
