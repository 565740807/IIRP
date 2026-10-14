import { useTranslation } from "react-i18next";
import { Star } from "lucide-react";
import { useWatchlist } from "@/lib/watchlist";
import { cn } from "@/lib/utils";
import { Toggle } from "@/components/ui/toggle";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Whether a "watchlist only" choice can apply: it is chosen and the list (once read) is not empty. */
export function useWatchFilter(chosen: boolean) {
  const { data } = useWatchlist();
  const count = data?.items.length ?? 0;
  const empty = !!data && count === 0;
  return { active: chosen && !empty, empty, count };
}

/**
 * "Watchlist only", beside a page's other filters. With no watched codes it is
 * unavailable and its hover says where codes are added. ``collapsible``: inside a
 * container narrower than 80rem (an ``@container`` ancestor) only the star and the
 * count show, and the hover names the choice.
 */
export function WatchFilter({ chosen, choose, collapsible = false, className }: {
  chosen: boolean; choose: (on: boolean) => void; collapsible?: boolean; className?: string;
}) {
  const { t } = useTranslation();
  const { active, empty, count } = useWatchFilter(chosen);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A disabled button gets no hover: the wrapper carries the hint. */}
        <span tabIndex={empty ? 0 : -1} className={cn("inline-flex rounded-lg focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none", className)}>
          <Toggle
            size="sm"
            variant="outline"
            pressed={active}
            disabled={empty}
            onPressedChange={choose}
            aria-label={t("ui.watchlist.filter.label")}
            data-watch-filter={active ? "on" : "off"}
            className="gap-1 px-2.5"
          >
            <Star className={cn(active && "fill-current text-watch")} aria-hidden/>
            <span className={cn(collapsible && "hidden @min-[80rem]:inline")}>{t("ui.watchlist.filter.label")}</span>
            {count > 0 && <span className="text-xs tabular-nums opacity-70">{count}</span>}
          </Toggle>
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs flex-col items-start">
        {collapsible && <span className="font-medium">{t("ui.watchlist.filter.label")}</span>}
        <span>{empty ? t("ui.watchlist.filter.empty") : t(active ? "ui.watchlist.filter.on" : "ui.watchlist.filter.off", { count })}</span>
      </TooltipContent>
    </Tooltip>
  );
}
