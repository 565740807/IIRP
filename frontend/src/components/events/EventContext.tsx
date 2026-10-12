import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ExternalLink, Sparkles, TriangleAlert } from "lucide-react";
import { distribution, pct, type Basis } from "@/lib/analysis";
import { WINDOWS, fiscalLabel, windowLabel, type EventContextItem, type EventResult, type EventRow, type WindowKey } from "@/lib/events";
import { formatDay } from "@/lib/format";
import { cn } from "@/lib/utils";
import { BoxPlot } from "@/components/analysis/BoxPlot";
import { Signed } from "@/components/analysis/Tables";
import { UpShare } from "@/components/analysis/UpShare";

/** Background and expectations come from the pasted AI answer; every view of them says so. */
export function AiNote({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <p className={cn("inline-flex items-center gap-1 text-xs text-muted-foreground", className)}>
      <Sparkles className="size-3.5 shrink-0" />
      {t("ui.events.ai_unverified")}
    </p>
  );
}

export function CategoryBadge({ category }: { category: string }) {
  const { t } = useTranslation();
  return <span className="inline-flex h-5 shrink-0 items-center rounded border bg-muted/60 px-1.5 text-xs font-medium text-muted-foreground">{t(`ui.events.category.${category}`)}</span>;
}

export function MajorBadge() {
  const { t } = useTranslation();
  return <span className="inline-flex h-5 shrink-0 items-center rounded border border-foreground/30 px-1.5 text-xs font-semibold">{t("ui.events.context.major")}</span>;
}

function host(url: string) {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** One background item: date, category, title, summary and the source link (new window). */
function ContextEntry({ item }: { item: EventContextItem }) {
  return (
    <li className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-2 gap-y-0.5 py-1.5">
      <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">{formatDay(item.date, { year: true })}</span>
      <div className="min-w-0 space-y-0.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <CategoryBadge category={item.category} />
          {item.major && <MajorBadge />}
          <span className="text-sm font-medium">{item.title}</span>
        </div>
        {item.summary && <p className="text-xs text-muted-foreground">{item.summary}</p>}
        {(item.source || item.url) && (
          <p className="text-xs">
            {item.url ? (
              <a href={item.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 underline-offset-2 hover:underline">
                {item.source || host(item.url)}
                <ExternalLink className="size-3" />
              </a>
            ) : (
              <span className="text-muted-foreground">{item.source}</span>
            )}
          </p>
        )}
      </div>
    </li>
  );
}

/** The background of one event, in date order. */
export function ContextList({ items }: { items: EventContextItem[] }) {
  const sorted = useMemo(() => [...items].sort((a, b) => a.date.localeCompare(b.date)), [items]);
  return <ul className="divide-y">{sorted.map((item, index) => <ContextEntry key={`${item.date}-${index}`} item={item} />)}</ul>;
}

/** Background shown under an event row: the list, its AI label and the doubt the AI flagged. */
export function RowContext({ row }: { row: EventRow }) {
  const { t } = useTranslation();
  if (!row.context.length && !row.uncertain && !row.macro_events.length) return null;
  return (
    <div className="space-y-1.5">
      {row.uncertain && (
        <p className="inline-flex items-center gap-1 rounded bg-doubt-soft px-1.5 py-0.5 text-xs text-doubt">
          <TriangleAlert className="size-3.5" />
          {t("ui.events.preview.uncertain")}
          {row.note ? ` · ${row.note}` : ""}
        </p>
      )}
      {row.macro_events.length > 0 && <p className="text-xs font-medium">{t("ui.events.macro.near", { count: row.macro_events.length, sessions: row.macro_window?.sessions ?? 2 })}</p>}
      {row.context.length > 0 && (
        <>
          <h3 className="text-xs font-semibold">{t("ui.events.context.title", { count: row.context.length })}</h3>
          <ContextList items={row.context} />
          <AiNote />
        </>
      )}
    </div>
  );
}

type TimelineEntry =
  | { kind: "event"; date: string; row: EventRow }
  | { kind: "context"; date: string; item: EventContextItem };

/** Every event of one ticker and all their background, in one column by date. */
export function Timeline({ result, onSelect }: { result: EventResult; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const entries = useMemo(() => {
    const seen = new Set<string>();
    const list: TimelineEntry[] = result.rows.map((row) => ({ kind: "event", date: row.date, row }));
    for (const row of result.rows) {
      for (const item of row.context) {
        const key = `${item.date}|${item.title}`;
        if (seen.has(key)) continue;
        seen.add(key);
        list.push({ kind: "context", date: item.date, item });
      }
    }
    // Events sort after the background of the same day.
    return list.sort((a, b) => a.date.localeCompare(b.date) || (a.kind === "event" ? 1 : 0) - (b.kind === "event" ? 1 : 0));
  }, [result]);
  return (
    <div className="rounded-lg border bg-card px-3 py-2">
      <ol className="relative space-y-0 border-l pl-4">
        {entries.map((entry, index) =>
          entry.kind === "event" ? (
            <li key={`e${index}`} className="relative py-2">
              <span className="absolute top-3.5 -left-[1.3rem] size-2.5 rounded-full bg-primary" aria-hidden />
              <button type="button" onClick={() => onSelect(entry.row.id)} className={cn("w-full rounded-md px-2 py-1 text-left hover:bg-accent", entry.row.uncertain && "bg-doubt-soft")}>
                <span className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-xs text-muted-foreground tabular-nums">{formatDay(entry.row.date, { year: true })}</span>
                  <span className="text-sm font-semibold">{entry.row.name}</span>
                  {fiscalLabel(entry.row) && <span className="text-xs text-muted-foreground">{fiscalLabel(entry.row)}</span>}
                  <span className="text-xs">
                    {t("ui.events.window.reaction")} <Signed value={entry.row.windows.reaction?.value} digits={2} />
                  </span>
                  {entry.row.uncertain && <span className="text-xs text-doubt">{t("ui.events.preview.uncertain")}</span>}
                </span>
              </button>
            </li>
          ) : (
            <li key={`c${index}`} className="relative">
              <span className="absolute top-3.5 -left-[1.2rem] size-2 rounded-full border bg-card" aria-hidden />
              <ul><ContextEntry item={entry.item} /></ul>
            </li>
          ),
        )}
      </ol>
      <AiNote className="mt-1" />
    </div>
  );
}

/** EPS with two decimals (USD per share), revenue in whole USD millions. */
function figure(value: number | null | undefined, digits: number) {
  return value == null ? "—" : new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

/** Earnings against expectations, one row per quarter; surprise = (actual − estimate) / |estimate| from the server. */
export function ExpectationsTable({ result, onSelect }: { result: EventResult; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const rows = [...result.rows].sort((a, b) => b.date.localeCompare(a.date));
  return (
    <div className="space-y-1">
      <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="px-2 py-1.5 pl-3 font-medium">{t("ui.events.col.quarter")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.eps_actual")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.eps_estimate")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.surprise")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.revenue_actual")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.revenue_estimate")}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t("ui.events.expect.surprise")}</th>
              <th className="px-2 py-1.5 font-medium">{t("ui.events.expect.guidance")}</th>
              <th className="px-2 py-1.5 pr-3 text-right font-medium">{t("ui.events.window.reaction")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const figures = row.earnings;
              const expected = row.expectations;
              return (
                <tr key={row.id} onClick={() => onSelect(row.id)} className={cn("cursor-pointer border-b last:border-b-0 hover:bg-accent/50", row.uncertain && "bg-doubt-soft")}>
                  <td className="px-2 py-1.5 pl-3 whitespace-nowrap">
                    <span className="font-medium">{fiscalLabel(row) || row.name}</span>
                    <span className="ml-1.5 text-xs text-muted-foreground tabular-nums">{formatDay(row.date, { year: true })}</span>
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{figure(figures?.eps_actual, 2)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{figure(figures?.eps_estimate, 2)}</td>
                  <td className="px-2 py-1.5 text-right whitespace-nowrap tabular-nums">
                    {expected.eps_surprise != null ? <Signed value={expected.eps_surprise} digits={1} /> : "—"}
                    {expected.surprise_group !== "no_data" && <span className="ml-1 text-xs text-muted-foreground">{t(`ui.events.group.surprise.${expected.surprise_group}`)}</span>}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{figure(figures?.revenue_actual, 0)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{figure(figures?.revenue_estimate, 0)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{expected.revenue_surprise != null ? <Signed value={expected.revenue_surprise} digits={1} /> : "—"}</td>
                  <td className="px-2 py-1.5">{figures?.guidance ? t(`ui.events.group.guidance.${figures.guidance}`) : "—"}</td>
                  <td className="px-2 py-1.5 pr-3 text-right tabular-nums"><Signed value={row.windows.reaction?.value} digits={2} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-muted-foreground">{t("ui.events.expect.units")}</p>
      <AiNote />
    </div>
  );
}

type GroupKind = "surprise" | "guidance";

/** Earnings grouped by EPS surprise or by guidance: N, median and up x/N for each window, and box plots. */
export function GroupStats({ result, basis, selected, onSelect }: { result: EventResult; basis: Basis; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<GroupKind>("surprise");
  const [window, setWindow] = useState<WindowKey>("reaction");
  const groups = result.groups?.[kind] ?? [];
  const label = (key: string) => t(`ui.events.group.${kind}.${key}`);
  const member = (row: EventRow, key: string) => (kind === "surprise" ? row.expectations.surprise_group : row.expectations.guidance_group) === key;
  const threshold = pct(result.groups?.inline_threshold ?? "0.01", 0).replace(/^\+/, "");
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-md border text-xs" role="group" aria-label={t("ui.events.group.by")}>
          {(["surprise", "guidance"] as const).map((value) => (
            <button key={value} type="button" aria-pressed={kind === value} onClick={() => setKind(value)}
              className={cn("h-7 border-r px-2.5 last:border-r-0 hover:bg-accent", kind === value && "bg-primary text-primary-foreground hover:bg-primary")}>
              {t(`ui.events.group.by_${value}`)}
            </button>
          ))}
        </div>
        {kind === "surprise" && <span className="text-xs text-muted-foreground">{t("ui.events.group.threshold", { threshold })}</span>}
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="px-2 py-1.5 pl-3 font-medium">{t("ui.events.group.group")}</th>
              {WINDOWS.map((key) => <th key={key} className="px-2 py-1.5 text-right font-medium" colSpan={2}>{windowLabel(key, result.n)}</th>)}
            </tr>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th />
              {WINDOWS.map((key) => [
                <th key={`${key}m`} className="px-2 py-1 text-right font-normal">{t("ui.events.group.n_median")}</th>,
                <th key={`${key}u`} className="px-2 py-1 text-right font-normal">{t("ui.events.col.up")}</th>,
              ])}
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.key} className={cn("border-b last:border-b-0", group.key === "no_data" && "text-muted-foreground")}>
                <td className="px-2 py-1.5 pl-3 font-medium whitespace-nowrap">{label(group.key)}</td>
                {WINDOWS.map((key) => {
                  const stats = distribution(group.summary[key], basis);
                  return [
                    <td key={`${key}m`} className="px-2 py-1.5 text-right whitespace-nowrap tabular-nums">
                      <span className="mr-1.5 text-xs text-muted-foreground">N {stats.n}</span>
                      <Signed value={stats.median} />
                    </td>,
                    <td key={`${key}u`} className="px-2 py-1.5 text-right tabular-nums"><UpShare stats={group.summary[key]} /></td>,
                  ];
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {groups.length > 0 && (
        <>
          <label className="text-xs text-muted-foreground">
            {t("ui.events.col.window")}{" "}
            <select aria-label={t("ui.events.col.window")} value={window} onChange={(e) => setWindow(e.target.value as WindowKey)} className="h-7 rounded border bg-card px-2 text-foreground">
              {WINDOWS.map((key) => <option key={key} value={key}>{windowLabel(key, result.n)}</option>)}
            </select>
          </label>
          <div className="rounded-lg border bg-card">
            <BoxPlot
              title={`${t(`ui.events.group.by_${kind}`)} · ${windowLabel(window, result.n)}`}
              selected={selected}
              onSelect={onSelect}
              groups={groups.map((group) => ({
                key: group.key,
                label: `${label(group.key)} (${group.event_count})`,
                stats: distribution(group.summary[window], basis),
                points: result.rows.filter((row) => member(row, group.key)).map((row) => ({
                  id: row.id,
                  label: `${row.name} · ${formatDay(row.date, { year: true })}`,
                  value: basis === "market" ? row.windows[window].excess : basis === "sector" ? row.windows[window].sector_excess : row.windows[window].value,
                })),
              }))}
            />
          </div>
        </>
      )}
      <AiNote />
    </div>
  );
}
