import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Star } from "lucide-react";
import { useFollowToggle, useNotablePeople } from "@/lib/notable";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * The ☆ that follows a person or stops following them. The change shows at
 * once; if the server refuses it, the star returns and the reason shows beside
 * it for a few seconds. Every star on a page reads the one list query.
 */
export function FollowStar({ personKey, name, size = "sm", className }: {
  personKey: string | null | undefined; name: string; size?: "sm" | "xs"; className?: string;
}) {
  const { t } = useTranslation();
  const list = useNotablePeople();
  const toggle = useFollowToggle();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(null), 5000);
    return () => window.clearTimeout(timer);
  }, [error]);
  if (!personKey) return null;
  const followed = !!list.data?.items.some((item) => item.key === personKey);
  const label = t(followed ? "ui.notable.star.remove" : "ui.notable.star.add", { name });
  const flip = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setError(null);
    toggle.mutate({ key: personKey, follow: !followed, name }, { onError: (failure) => setError(failure.message) });
  };
  return (
    <Tooltip open={error ? true : undefined}>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size={size === "xs" ? "icon-xs" : "icon-sm"}
          aria-pressed={followed}
          aria-label={label}
          disabled={!list.data}
          onClick={flip}
          onMouseDown={(event) => event.preventDefault()}
          data-follow-star={personKey}
          className={cn("shrink-0 text-muted-foreground hover:text-foreground", followed && "text-watch hover:text-watch", className)}
        >
          <Star className={cn(size === "xs" ? "size-3.5" : "size-4", followed && "fill-current")} aria-hidden/>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className={cn(error && "bg-destructive")} role={error ? "alert" : undefined}>
        {error ?? label}
      </TooltipContent>
    </Tooltip>
  );
}
