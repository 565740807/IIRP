import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Inbox, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Loading, empty and error states shared by every page: skeletons that hold
 * the final layout's height (nothing jumps when data arrives), an empty state
 * that says why, and an error state that says what to do.
 */
export function TableSkeleton({ rows = 8, columns = 6, className }: { rows?: number; columns?: number; className?: string }) {
  const { t } = useTranslation();
  return (
    <div className={cn("overflow-hidden rounded-lg border bg-card", className)} aria-busy role="status" aria-label={t("ui.loading")}>
      <div className="flex gap-4 border-b px-3 py-2.5">
        {Array.from({ length: columns }, (_, index) => <Skeleton key={index} className={cn("h-3", index ? "w-14" : "w-28")} />)}
      </div>
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="flex items-center gap-4 border-b px-3 py-2.5 last:border-b-0">
          {Array.from({ length: columns }, (_, index) => <Skeleton key={index} className={cn("h-3.5", index ? "w-14" : "w-40")} />)}
        </div>
      ))}
    </div>
  );
}

export function BlockSkeleton({ height = "h-64", className }: { height?: string; className?: string }) {
  const { t } = useTranslation();
  return <Skeleton className={cn("w-full rounded-lg", height, className)} role="status" aria-label={t("ui.loading")} />;
}

export function EmptyState({ title, reason, action, className }: { title: ReactNode; reason?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-lg border border-dashed bg-card px-6 py-10 text-center", className)}>
      <Inbox className="mx-auto size-8 text-muted-foreground" strokeWidth={1.3} aria-hidden />
      <p className="mt-2 text-sm font-medium">{title}</p>
      {reason && <p className="mx-auto mt-1 max-w-xl text-sm text-muted-foreground">{reason}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

/** An error with the message from the server, what to do about it, and a retry when one helps. */
export function ErrorState({ error, hint, retry, compact = false, className }: {
  error: { message: string } | string;
  hint?: ReactNode;
  retry?: () => void;
  compact?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const message = typeof error === "string" ? error : error.message;
  return (
    <div role="alert" className={cn("flex items-start gap-2.5 rounded-lg border border-destructive/30 bg-card text-sm", compact ? "px-3 py-2" : "px-4 py-3", className)}>
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1">
        <p className="text-destructive">{message}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">{hint ?? t("ui.view.error_hint")}</p>
      </div>
      {retry && (
        <Button size="xs" variant="outline" onClick={retry} className="shrink-0">
          <RotateCcw />
          {t("ui.view.retry")}
        </Button>
      )}
    </div>
  );
}
