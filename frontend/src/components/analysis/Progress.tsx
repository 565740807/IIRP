import { AnimatePresence, motion } from "motion/react";
import { useTranslation } from "react-i18next";
import { Check, CircleAlert, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import type { Schemas } from "@/lib/api-client";
import { cn } from "@/lib/utils";

type Item = Schemas["TickerProgress"];
const STEPS = ["download", "compute", "done"] as const;

function position(step: Item["step"]) {
  return step === "queued" ? -1 : step === "failed" ? -2 : STEPS.indexOf(step);
}

/** One ticker's steps: download prices → compute → done. */
function Row({ item }: { item: Item }) {
  const { t } = useTranslation();
  const at = position(item.step);
  return (
    <motion.li layout initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-3 py-1">
      <span className="truncate text-sm font-semibold tabular-nums">{item.symbol}</span>
      {item.step === "failed" ? (
        <span className="flex min-w-0 items-center gap-1.5 text-sm text-destructive">
          <CircleAlert className="size-3.5 shrink-0" />
          <span className="truncate" title={item.reason ? tm(item.reason) : undefined}>
            {t("ui.analysis.progress.failed")}
            {item.reason ? ` · ${tm(item.reason)}` : ""}
          </span>
        </span>
      ) : (
        <ol className="flex items-center gap-1.5 text-xs">
          {STEPS.map((step, index) => {
            const done = at > index || item.step === "done";
            const current = at === index && item.step !== "done";
            return (
              <li key={step} className="flex items-center gap-1.5">
                {index > 0 && (
                  <span className="relative h-px w-6 overflow-hidden bg-border">
                    <motion.span className="absolute inset-0 origin-left bg-primary" initial={false} animate={{ scaleX: at >= index ? 1 : 0 }} transition={{ duration: 0.3 }} />
                  </span>
                )}
                <span
                  className={cn(
                    "inline-flex h-6 items-center gap-1 rounded-full border px-2 transition-colors",
                    done && "border-transparent bg-secondary text-foreground",
                    current && "border-primary text-foreground",
                    !done && !current && "text-muted-foreground",
                  )}
                >
                  {current ? <LoaderCircle className="size-3 motion-safe:animate-spin" /> : done ? <Check className="size-3" /> : null}
                  {t(`ui.analysis.progress.${step}`)}
                </span>
              </li>
            );
          })}
          {item.step === "queued" && <span className="text-muted-foreground">{t("ui.analysis.progress.queued")}</span>}
        </ol>
      )}
    </motion.li>
  );
}

/**
 * Step-by-step progress of a research, one row per ticker. It stays while any
 * ticker is still working or has failed, and leaves once all are done.
 */
export function Progress({ items, onRetry, retrying }: { items: Item[]; onRetry?: () => void; retrying?: boolean }) {
  const { t } = useTranslation();
  const visible = items.some((item) => item.step !== "done");
  const done = items.filter((item) => item.step === "done").length;
  const failed = items.some((item) => item.step === "failed");
  return (
    <AnimatePresence initial={false}>
      {visible && (
        <motion.section
          key="progress"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          className="overflow-hidden rounded-lg border bg-card"
          aria-live="polite"
        >
          <header className="flex items-center gap-2 border-b px-4 py-2">
            <h2 className="text-sm font-semibold">{t("ui.analysis.progress.title")}</h2>
            <span className="text-xs text-muted-foreground tabular-nums">{t("ui.analysis.progress.count", { done, total: items.length })}</span>
            {failed && onRetry && (
              <button type="button" onClick={onRetry} disabled={retrying} className="ml-auto text-xs font-medium underline-offset-2 hover:underline disabled:opacity-60">
                {t("ui.analysis.progress.retry")}
              </button>
            )}
          </header>
          <div className="relative h-0.5 overflow-hidden bg-secondary">
            <motion.div className="absolute inset-y-0 left-0 bg-primary" initial={false} animate={{ width: `${(done / Math.max(items.length, 1)) * 100}%` }} transition={{ duration: 0.4 }} />
          </div>
          <ul className="max-h-72 overflow-y-auto px-4 py-2">
            {items.map((item) => (
              <Row key={item.symbol} item={item} />
            ))}
          </ul>
        </motion.section>
      )}
    </AnimatePresence>
  );
}
