import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { activeSection } from "@/lib/sectionNav";
import { cn } from "@/lib/utils";

export type ContentsItem = { id: string; label: string };

// The sticky page header (3.5rem) plus this bar: where a section counts as "being read".
const READING_LINE = 120;

/**
 * Contents of a long page, sticky under the header: one link per section and
 * the section being read highlighted. Sections are elements with these ids;
 * missing ones (not rendered yet) are skipped.
 */
export function PageContents({ items, className }: { items: ContentsItem[]; className?: string }) {
  const { t } = useTranslation();
  const present = items.filter((item) => typeof document === "undefined" || document.getElementById(item.id));
  const [active, setActive] = useState<string | null>(items[0]?.id ?? null);
  const signature = items.map((item) => item.id).join(",");
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      const tops = signature.split(",").flatMap((id) => {
        const element = document.getElementById(id);
        return element ? [{ id, top: element.getBoundingClientRect().top }] : [];
      });
      const end = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      setActive(activeSection(tops, READING_LINE, end && window.scrollY > 0));
    };
    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    const observer = new ResizeObserver(schedule);
    observer.observe(document.body);
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [signature]);
  if (present.length < 2 && items.length < 2) return null;
  const go = (id: string) => {
    const element = document.getElementById(id);
    if (!element) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    setActive(id);
  };
  return (
    <nav
      aria-label={t("ui.view.contents")}
      data-page-contents
      className={cn("sticky top-14 z-20 -mx-1 flex items-center gap-1 overflow-x-auto border-b bg-background/95 px-1 py-1.5 backdrop-blur", className)}
    >
      <span className="mr-1 shrink-0 text-xs text-muted-foreground">{t("ui.view.contents")}</span>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => go(item.id)}
          aria-current={active === item.id ? "location" : undefined}
          className={cn(
            "h-7 shrink-0 rounded-md px-2.5 text-xs whitespace-nowrap text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
            active === item.id && "bg-accent font-medium text-foreground",
          )}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}
