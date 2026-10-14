import { lazy, Suspense, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Columns3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

const ColumnMenu = lazy(() => import("./column-menu").then(module => ({ default: module.ColumnMenu })));

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
        <Suspense fallback={<span className="block h-32 animate-pulse rounded bg-muted" />}>
          <ColumnMenu columns={columns} onChange={onChange} onReset={onReset} changed={changed} />
        </Suspense>
      </PopoverContent>
    </Popover>
  );
}
