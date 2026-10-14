import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Check, ClipboardCopy, LoaderCircle, Plus, Star, X } from "lucide-react";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatDay } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { tickerKey } from "@/lib/tickerMarks";
import { etDay, refreshWatchViews, useWatchlist, useWatchToggle, WATCHLIST_KEY } from "@/lib/watchlist";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info";
import { ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";

type ImportResult = Schemas["WatchlistImportOutput"];

const inputStyle = "h-8 min-w-0 rounded-md border bg-card px-2 text-sm outline-none focus:border-ring";

/** Copies text; falls back to a selected text area where the clipboard API is not offered. */
async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}

function ResultLine({ label, codes, tone }: { label: string; codes: readonly string[]; tone?: string }) {
  const { t } = useTranslation();
  if (!codes.length) return null;
  const shown = codes.slice(0, 12);
  return (
    <p className={cn("text-xs", tone)}>
      <span className="font-medium">{label} {codes.length}</span>
      <span className="text-muted-foreground"> · {shown.join(", ")}{codes.length > shown.length && t("ui.watchlist.settings.more_codes", { count: codes.length - shown.length })}</span>
    </p>
  );
}

/** The import's outcome: added, already watched, not stock codes, and left out at the limit. */
function ImportOutcome({ result }: { result: ImportResult }) {
  const { t } = useTranslation();
  const nothing = !result.added.length && !result.existing.length && !result.invalid.length && !result.over_limit.length;
  return (
    <div role="status" data-import-result className="space-y-0.5 rounded-md border bg-secondary/40 px-2.5 py-2">
      {nothing && <p className="text-xs text-muted-foreground">{t("ui.watchlist.settings.import_nothing")}</p>}
      <ResultLine label={t("ui.watchlist.settings.import_added")} codes={result.added} tone="text-foreground"/>
      <ResultLine label={t("ui.watchlist.settings.import_existing")} codes={result.existing}/>
      <ResultLine label={t("ui.watchlist.settings.import_invalid")} codes={result.invalid} tone="text-warn"/>
      <ResultLine label={t("ui.watchlist.settings.import_over_limit", { limit: result.limit })} codes={result.over_limit} tone="text-warn"/>
    </div>
  );
}

/**
 * Settings → Watchlist: the watched codes with their local names and the day each
 * was added; add one, remove any, paste many, or copy them all. Nothing here goes
 * online: a code without local data is kept and says so.
 */
export function WatchlistSection({ close }: { close?: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const list = useWatchlist();
  const toggle = useWatchToggle();
  const [code, setCode] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [copied, setCopied] = useState(false);
  const pasted = useMutation({
    mutationFn: (value: string) => unwrap(client.POST("/api/v1/watchlist/import", { body: { text: value } })),
    onSuccess: (data) => {
      queryClient.setQueryData(WATCHLIST_KEY, { items: data.items, limit: data.limit, acknowledged_at: data.acknowledged_at });
      refreshWatchViews(queryClient);
      if (!data.invalid.length) setText("");
    },
  });
  const items = list.data?.items ?? [];
  const limit = list.data?.limit ?? 500;
  const add = (event: FormEvent) => {
    event.preventDefault();
    const key = tickerKey(code).replace(/^\$/, "");
    if (!key) return;
    setAddError(null);
    toggle.mutate({ key, watched: true }, {
      onSuccess: () => setCode(""),
      onError: (error) => setAddError(error.message),
    });
  };
  const copy = async () => {
    await copyText(items.map((item) => item.ticker).join(", "));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };
  return (
    <section className="space-y-3" aria-labelledby="watchlist-settings-title" data-watchlist-settings>
      <div className="flex items-center gap-2">
        <h3 id="watchlist-settings-title" className="text-sm font-medium">{t("ui.watchlist.settings.title")}</h3>
        <span className="text-xs text-muted-foreground tabular-nums">{t("ui.watchlist.settings.count", { count: items.length, limit })}</span>
        <InfoTip>{t("ui.watchlist.settings.rule")}</InfoTip>
        <Button size="xs" variant="ghost" className="ml-auto" disabled={!items.length} onClick={() => void copy()}>
          {copied ? <Check className="text-ok" aria-hidden/> : <ClipboardCopy aria-hidden/>}
          {t(copied ? "ui.watchlist.settings.copied" : "ui.watchlist.settings.copy")}
        </Button>
      </div>

      <form onSubmit={add} className="flex items-center gap-2">
        <input
          value={code}
          onChange={(event) => { setCode(event.target.value); setAddError(null); }}
          placeholder={t("ui.watchlist.settings.add_placeholder")}
          aria-label={t("ui.watchlist.settings.add_label")}
          aria-invalid={!!addError}
          maxLength={32}
          className={cn(inputStyle, "w-44 font-mono uppercase placeholder:font-sans placeholder:normal-case")}
        />
        <Button type="submit" size="sm" variant="outline" disabled={!code.trim() || toggle.isPending}>
          <Plus aria-hidden/>{t("ui.watchlist.settings.add")}
        </Button>
        {addError && <p role="alert" className="min-w-0 truncate text-xs text-destructive" title={addError}>{addError}</p>}
      </form>

      <details className="group rounded-md border px-2.5 py-1.5" open={!items.length || undefined}>
        <summary className="cursor-pointer text-xs font-medium text-muted-foreground select-none hover:text-foreground">
          {t("ui.watchlist.settings.import_title")}
        </summary>
        <div className="mt-2 space-y-2">
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            maxLength={10_000}
            placeholder={t("ui.watchlist.settings.import_placeholder")}
            aria-label={t("ui.watchlist.settings.import_title")}
            className="w-full resize-y rounded-md border bg-card px-2 py-1.5 font-mono text-xs outline-none focus:border-ring"
          />
          <div className="flex items-center gap-2">
            <Button size="xs" variant="outline" disabled={!text.trim() || pasted.isPending} onClick={() => pasted.mutate(text)}>
              {pasted.isPending && <LoaderCircle className="motion-safe:animate-spin" aria-hidden/>}
              {t("ui.watchlist.settings.import")}
            </Button>
            <span className="text-xs text-muted-foreground">{t("ui.watchlist.settings.import_hint")}</span>
          </div>
          {pasted.error && <p role="alert" className="text-xs text-destructive">{pasted.error.message}</p>}
          {pasted.data && <ImportOutcome result={pasted.data}/>}
        </div>
      </details>

      {list.error ? <ErrorState error={list.error} retry={() => void list.refetch()}/>
        : !list.data ? <div className="space-y-1.5" aria-busy>{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-6 w-full"/>)}</div>
        : !items.length ? (
          <p className="flex items-start gap-2 rounded-md border border-dashed px-3 py-4 text-xs text-muted-foreground">
            <Star className="mt-0.5 size-3.5 shrink-0" aria-hidden/>{t("ui.watchlist.settings.empty")}
          </p>
        ) : (
          <div className="max-h-72 overflow-y-auto rounded-md border">
            <table className="w-full table-fixed text-xs" data-watchlist-table>
              <colgroup><col className="w-24"/><col/><col className="w-36"/><col className="w-9"/></colgroup>
              <thead className="sticky top-0 bg-secondary/80 backdrop-blur">
                <tr className="text-left text-muted-foreground">
                  <th className="px-2.5 py-1.5 font-medium">{t("ui.watchlist.settings.column.ticker")}</th>
                  <th className="px-2.5 py-1.5 font-medium">{t("ui.watchlist.settings.column.company")}</th>
                  <th className="px-2.5 py-1.5 text-right font-medium">{t("ui.watchlist.settings.column.added")}</th>
                  <th><span className="sr-only">{t("ui.watchlist.settings.remove_column")}</span></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const name = item.name ? readableCase(item.name) : null;
                  return (
                    <tr key={item.ticker} className="border-t hover:bg-accent/40">
                      <td className="px-2.5 py-1 font-mono font-semibold">{item.ticker}</td>
                      <td className="truncate px-2.5 py-1" title={name ?? undefined}>
                        {!name ? <span className="text-muted-foreground">{t(item.local ? "ui.watchlist.settings.no_name" : "ui.watchlist.no_local_data")}</span>
                          : item.issuer_id ? <Link to={`/companies/${item.issuer_id}`} onClick={close} className="hover:underline">{name}</Link>
                          : name}
                      </td>
                      <td className="px-2.5 py-1 text-right whitespace-nowrap text-muted-foreground tabular-nums">{formatDay(etDay(item.added_at), { year: true })}</td>
                      <td className="py-0.5 pr-1 text-right">
                        <Button size="icon-xs" variant="ghost" aria-label={t("ui.watchlist.settings.remove", { ticker: item.ticker })}
                          title={t("ui.watchlist.settings.remove", { ticker: item.ticker })}
                          onClick={() => toggle.mutate({ key: item.ticker, watched: false })}>
                          <X aria-hidden/>
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      {toggle.error && !addError && <p role="alert" className="text-xs text-destructive">{toggle.error.message}</p>}
    </section>
  );
}
