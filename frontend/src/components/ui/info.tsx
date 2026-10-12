import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Info } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** A small ⓘ next to a title or control; the explanation opens on hover or focus. */
export function InfoTip({ children, label, className, side = "top" }: {
  children: ReactNode;
  label?: string;
  className?: string;
  side?: "top" | "bottom" | "left" | "right";
}) {
  const { t } = useTranslation();
  return (
    <Tooltip>
      <TooltipTrigger
        type="button"
        aria-label={label ?? t("ui.view.more_info")}
        className={cn("inline-grid size-4 shrink-0 cursor-help place-items-center rounded-full align-middle text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none", className)}
      >
        <Info className="size-3.5" strokeWidth={1.8} />
      </TooltipTrigger>
      <TooltipContent side={side} className="max-w-sm flex-col items-start gap-1 text-left leading-relaxed">
        {children}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Explanatory text kept to one line. With ``children`` the first line shows
 * and the rest opens with "More"; without, a summary that does not fit opens
 * in full with "More".
 */
export function Explain({ summary, children, className }: { summary: ReactNode; children?: ReactNode; className?: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const line = useRef<HTMLSpanElement>(null);
  const [clipped, setClipped] = useState(false);
  useLayoutEffect(() => {
    const element = line.current;
    if (!element) return;
    const measure = () => setClipped(element.scrollWidth > element.clientWidth + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [summary]);
  const more = !!children || clipped || open;
  return (
    // inline-size containment: the one-line text never widens a dialog or grid around it.
    <div className={cn("min-w-0 text-xs text-muted-foreground [contain:inline-size]", className)}>
      <p className="flex min-w-0 items-baseline gap-1.5">
        <span ref={line} className={cn("min-w-0", !open && "truncate")} title={!open && clipped && typeof summary === "string" ? summary : undefined}>{summary}</span>
        {more && (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex shrink-0 items-center gap-0.5 font-medium text-foreground/70 hover:text-foreground"
          >
            {t(open ? "ui.view.less" : "ui.view.more")}
            <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
          </button>
        )}
      </p>
      {open && children && <div className="mt-1 space-y-1 leading-relaxed">{children}</div>}
    </div>
  );
}
