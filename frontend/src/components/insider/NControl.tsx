import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RotateCcw } from "lucide-react";
import { useInsiderN } from "@/lib/windows";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Quick switch of n: common values, a custom value, and back to the default. */
export function NControl({ className }: { className?: string }) {
  const { t } = useTranslation();
  const { n, setN, defaultN, presets, min, max } = useInsiderN();
  const custom = !presets.includes(n);
  const [draft, setDraft] = useState(custom ? String(n) : "");
  useEffect(() => setDraft(custom ? String(n) : ""), [n, custom]);
  const apply = () => {
    const value = Number(draft);
    if (Number.isInteger(value) && value >= min && value <= max) setN(value);
    else setDraft(custom ? String(n) : "");
  };
  return (
    <div className={cn("inline-flex items-center gap-1 text-xs", className)} role="group" aria-label={t("ui.window.n_label")}>
      <span className="mr-0.5 text-muted-foreground">{t("ui.window.n_label")}</span>
      <div className="inline-flex overflow-hidden rounded-md border bg-card">
        {presets.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={n === value}
            onClick={() => setN(value)}
            className={cn("h-7 min-w-8 border-r px-2 tabular-nums last:border-r-0 hover:bg-accent", n === value && "bg-primary text-primary-foreground hover:bg-primary")}
          >
            {value}
          </button>
        ))}
      </div>
      <input
        inputMode="numeric"
        aria-label={t("ui.window.n_custom")}
        placeholder={t("ui.window.n_custom_short")}
        value={draft}
        onChange={(event) => setDraft(event.target.value.replace(/\D/g, "").slice(0, 2))}
        onBlur={() => draft && apply()}
        onKeyDown={(event) => event.key === "Enter" && apply()}
        className={cn("h-7 w-14 rounded-md border bg-card px-2 text-center tabular-nums outline-none placeholder:text-muted-foreground focus-visible:border-ring", custom && "border-primary font-medium")}
      />
      {n !== defaultN && (
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" onClick={() => setN(null)} className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground" aria-label={t("ui.window.n_reset", { n: defaultN })}>
              <RotateCcw className="size-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent>{t("ui.window.n_reset", { n: defaultN })}</TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}
