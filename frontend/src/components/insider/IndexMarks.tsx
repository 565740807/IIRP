import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Small SPX / NDX marks for a company on the S&P 500 or Nasdaq-100 list (both
 * when on both). The lists are unofficial weekly copies; the tooltip says so.
 */
export function IndexMarks({ indexes, className }: { indexes?: readonly string[] | null; className?: string }) {
  const { t } = useTranslation();
  if (!indexes?.length) return null;
  return (
    <span className={cn("inline-flex shrink-0 items-baseline gap-0.5", className)}>
      {indexes.map((index) => (
        <span
          key={index}
          title={t(`ui.index_mark.${index}_tip`)}
          className="rounded-sm border px-1 text-[10px] leading-4 font-semibold tracking-wide text-muted-foreground"
        >
          {t(`ui.index_mark.${index}`)}
        </span>
      ))}
    </span>
  );
}
