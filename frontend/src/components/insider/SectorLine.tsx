import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LoaderCircle } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { DEFAULT_STATE, sectorSearch } from "@/lib/sectorState";

/**
 * "Sector: Information Technology · XLK · Semiconductors · SOXX (source: S&P 500 list)"
 * under a company title. The classification is the server's (S&P 500 GICS, else
 * Yahoo's sector, else none). When it waits for an identity refresh that was never
 * asked for, the page asks once and re-reads until it is done.
 */
export function SectorLine({ ticker }: { ticker: string | null | undefined }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const key = ["classification", ticker];
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/securities/{symbol}/classification", { params: { path: { symbol: ticker! } }, signal })),
    enabled: !!ticker,
    staleTime: 60_000,
    refetchInterval: (state) => (state.state.data?.refresh === "pending" ? 3_000 : false),
  });
  const refresh = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/securities/{symbol}/classification/refresh", { params: { path: { symbol: ticker! } } })),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  });
  const asked = useRef<string | null>(null);
  const data = query.data;
  useEffect(() => {
    if (ticker && data?.refresh === "needed" && asked.current !== ticker) {
      asked.current = ticker;
      refresh.mutate();
    }
  }, [data?.refresh, ticker]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!ticker || !data) return null;
  const waiting = data.refresh !== "none" && !data.sector;
  return (
    <p className="flex flex-wrap items-baseline gap-x-1.5 text-xs text-muted-foreground">
      {data.removed_on && <span>{t("ui.long_prices.removed", { date: data.removed_on })}</span>}
      <span>{t("ui.classification.label")}</span>
      {data.sector ? (
        <>
          <Link
            to={`/sectors?${sectorSearch({ ...DEFAULT_STATE, level: "group", sector: data.sector.id })}`}
            className="font-medium text-foreground hover:underline"
          >
            {t(`ui.sectors.name.${data.sector.id}`)}
          </Link>
          <span className="font-mono">{data.sector.etf}</span>
          {data.industry_group && (
            <>
              <span aria-hidden>·</span>
              <span className="text-foreground">{t(`ui.sectors.group.${data.industry_group.id}`)}</span>
              {data.industry_group.etf && <span className="font-mono">{data.industry_group.etf}</span>}
            </>
          )}
          <span>{t(`ui.classification.source.${data.source}`)}</span>
        </>
      ) : waiting ? (
        <span className="inline-flex items-center gap-1">
          <LoaderCircle className="size-3 motion-safe:animate-spin" />
          {t("ui.classification.checking")}
        </span>
      ) : (
        <span>{t("ui.classification.none")}</span>
      )}
    </p>
  );
}
