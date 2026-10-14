import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Star } from "lucide-react";
import { tickerKey } from "@/lib/tickerMarks";
import { useIsWatched, useWatchlist, useWatchToggle } from "@/lib/watchlist";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * The star beside a page title: adds the code to the watchlist or removes it.
 * The change shows at once; if the server refuses it, the star returns to what
 * it was and the reason shows beside it for a few seconds.
 */
export function WatchStar({ ticker, className }: { ticker: string | null | undefined; className?: string }) {
  const { t } = useTranslation();
  const list = useWatchlist();
  const toggle = useWatchToggle();
  const key = tickerKey(ticker).replace(/^\$/, "");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(null), 5000);
    return () => window.clearTimeout(timer);
  }, [error]);
  if (!key) return null;
  const watched = !!list.data?.items.some((item) => item.ticker === key);
  const label = t(watched ? "ui.watchlist.star.remove" : "ui.watchlist.star.add", { ticker: key });
  const flip = () => {
    setError(null);
    toggle.mutate({ key, watched: !watched }, { onError: (failure) => setError(failure.message) });
  };
  return (
    <Tooltip open={error ? true : undefined}>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-pressed={watched}
          aria-label={label}
          disabled={!list.data}
          onClick={flip}
          data-watch-star={key}
          className={cn("text-muted-foreground hover:text-foreground", watched && "text-watch hover:text-watch", className)}
        >
          <Star className={cn("size-4", watched && "fill-current")} aria-hidden/>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className={cn(error && "bg-destructive")} role={error ? "alert" : undefined}>
        {error ?? label}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * A small star for a watched code in tables; nothing for other codes. Every row reads the one
 * list query. ``corner`` pins it to the top-right corner of the code's badge (the badge needs
 * ``relative``), so a tight company column loses no width.
 */
export function WatchMark({ ticker, corner = false, className }: { ticker: string | null | undefined; corner?: boolean; className?: string }) {
  const { t } = useTranslation();
  if (!useIsWatched(ticker)) return null;
  return (
    <span data-watch-mark role="img" aria-label={t("ui.watchlist.mark")} title={t("ui.watchlist.mark")}
      className={cn("inline-flex shrink-0 self-center text-watch",
        corner && "absolute -top-1.5 -right-1.5 rounded-full bg-card p-px", className)}>
      <Star className={cn("fill-current", corner ? "size-2.5" : "size-3")} aria-hidden/>
    </span>
  );
}
