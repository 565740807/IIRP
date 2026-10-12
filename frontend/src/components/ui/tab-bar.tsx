import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type TabItem<T extends string> = { id: T; label: ReactNode; count?: number | null };

/** One row of tabs, each with an optional count; one table or view shows at a time. */
export function TabBar<T extends string>({ items, value, onChange, label, className }: {
  items: TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  return (
    <div role="tablist" aria-label={label} className={cn("flex flex-wrap gap-x-0.5 border-b", className)}>
      {items.map((item) => {
        const active = item.id === value;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={active}
            data-tab={item.id}
            onClick={() => onChange(item.id)}
            className={cn(
              "-mb-px inline-flex shrink-0 items-center gap-1.5 border-b-2 border-transparent px-2.5 py-2 text-sm whitespace-nowrap text-muted-foreground transition-colors hover:text-foreground",
              active && "border-primary font-medium text-foreground",
            )}
          >
            {item.label}
            {item.count != null && (
              <span className={cn("rounded-full bg-secondary px-1.5 text-xs tabular-nums", active && "bg-primary text-primary-foreground")}>{item.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
