import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown } from "lucide-react";
import { useFold } from "@/lib/viewPrefs";
import { cn } from "@/lib/utils";

/**
 * A secondary part of a page that can be folded away. Whether it is open is
 * remembered in this browser under ``id``; ``open`` is the first-visit default.
 */
export function FoldSection({ id, title, meta, actions, open: initial = false, children, className, anchor }: {
  id: string;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  open?: boolean;
  children: ReactNode;
  className?: string;
  anchor?: string;
}) {
  const { t } = useTranslation();
  const [open, toggle] = useFold(id, initial);
  return (
    <section id={anchor} data-contents={anchor ? "" : undefined} className={cn("scroll-mt-28 space-y-2", className)}>
      <header className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => toggle()}
          className="inline-flex min-w-0 items-center gap-1.5 text-left hover:text-foreground"
        >
          <ChevronDown className={cn("size-4 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")} />
          <h2 className="section-title">{title}</h2>
        </button>
        {meta && <span className="text-xs text-muted-foreground">{meta}</span>}
        {!open && <button type="button" onClick={() => toggle(true)} className="text-xs text-muted-foreground hover:text-foreground hover:underline">{t("ui.view.show_section")}</button>}
        {open && actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
      </header>
      {open && children}
    </section>
  );
}
