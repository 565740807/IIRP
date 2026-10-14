import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";
import { ArrowDown, ArrowUp, ChevronRight } from "lucide-react";
import { formatDay } from "@/lib/format";
import { createPortal } from "react-dom";
import { PERIODS, periodChange, periodRank, type EtfPerformance, type Period, type SectorGroupItem, type SortColumn } from "@/lib/sectors";
import { columnVisibility, useColumnChoices } from "@/lib/viewPrefs";
import { ColumnSettings } from "@/components/ui/column-settings";
import { INSIDER_DAYS, InsiderHeader, InsiderInfo, InsiderNetCell, insiderNet, type InsiderSectors } from "./InsiderNet";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ChangeCell, commonStart } from "./SectorTable";
import { FetchMark, SinceNote } from "./SectorNotes";

type Coverage = SectorGroupItem["coverage"];

/** Complete / partial / reference / pending, with what the level means on hover. */
export function CoverageBadge({ coverage, same }: { coverage: Coverage; same?: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn("cursor-default rounded border px-1.5 py-px text-xs whitespace-nowrap",
            coverage === "complete" && "border-foreground/30 text-foreground",
            coverage === "partial" && "border-foreground/20 text-muted-foreground",
            coverage === "reference" && "border-dashed border-foreground/25 text-muted-foreground",
            coverage === "pending" && "border-transparent bg-secondary text-muted-foreground")}>
            {t(`ui.sectors.coverage.${coverage}`)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{t(`ui.sectors.coverage_rule.${coverage}`)}</TooltipContent>
      </Tooltip>
      {same && <span className="rounded bg-secondary px-1.5 py-px text-xs whitespace-nowrap text-muted-foreground">{t("ui.sectors.same_as_sector")}</span>}
    </span>
  );
}

/**
 * Ranked groups first, in the chosen order: a period column follows the
 * backend's rank, which only primaries have; groups without a value follow,
 * and pending groups always come last.
 */
export function sortGroups(items: readonly SectorGroupItem[], sort: SortColumn, desc: boolean, name: (id: string) => string,
  insider: (id: string) => number | undefined = () => undefined) {
  if (sort === "insider") {
    // Every group has companies, priced or not: all of them by the net amount, groups without trades last.
    const direction = desc ? -1 : 1;
    return [...items].sort((a, b) => {
      const left = insider(a.id), right = insider(b.id);
      if (left == null || right == null) return left == null ? (right == null ? 0 : 1) : -1;
      return direction * (left - right);
    });
  }
  const ranked = items.filter((item) => item.periods != null);
  const pending = items.filter((item) => item.periods == null);
  const direction = desc ? 1 : -1;
  const ordered = [...ranked].sort((a, b) => {
    if (sort === "name") return -direction * name(a.id).localeCompare(name(b.id));
    if (sort === "etf") return -direction * (a.etf ?? "").localeCompare(b.etf ?? "");
    const left = periodRank(a, sort as Period), right = periodRank(b, sort as Period);
    if (left == null || right == null) return left == null ? (right == null ? 0 : 1) : -1;
    return direction * (left - right);
  });
  return [...ordered, ...pending];
}

function Check({ etf, label, selected, toggle }: { etf: string; label: string; selected: readonly string[]; toggle: (etf: string) => void }) {
  const { t } = useTranslation();
  return <input type="checkbox" aria-label={t("ui.sectors.select_one", { name: label })} checked={selected.includes(etf)} onChange={() => toggle(etf)}/>;
}

function ReferenceRow({ row, group, selected, toggle, historyYears, shown, periods }: {
  row: EtfPerformance; group: string; selected: readonly string[]; toggle: (etf: string) => void; historyYears: number;
  shown: Record<string, boolean>; periods: readonly Period[];
}) {
  const { t } = useTranslation();
  const note = t(`ui.sectors.etf_note.${row.etf}`, { defaultValue: "" });
  return (
    <tr className={cn("border-b border-dashed bg-muted/40 text-xs text-muted-foreground", selected.includes(row.etf) && "bg-selected/60")}>
      <td className="py-1.5 pl-3"><Check etf={row.etf} label={`${group} · ${row.etf}`} selected={selected} toggle={toggle}/></td>
      <td className="py-1.5 pr-2 pl-6">
        <span className="flex flex-col border-l-2 border-foreground/15 pl-2">
          <span>{t("ui.sectors.reference_row")}{!shown.etf && <> · <span className="font-mono">{row.etf}</span><FetchMark fetch={row.fetch}/></>}{note && <> · {note}</>}</span>
          <SinceNote item={row} years={historyYears}/>
        </span>
      </td>
      {shown.etf && <td className="px-2 py-1.5">
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
          <span className="inline-flex items-center gap-1 font-mono">{row.etf}<FetchMark fetch={row.fetch}/></span>
          <CoverageBadge coverage="reference"/>
        </span>
      </td>}
      {periods.map((period) => (
        <td key={period} className="px-2 py-1.5 text-right opacity-80"><ChangeCell change={periodChange(row, period)} period={period}/></td>
      ))}
      {shown.insider && <td/>}
    </tr>
  );
}

/**
 * Industry groups with their primary ETF, coverage and every period. Only
 * primaries are ranked; reference rows open under their group and pending
 * groups stay at the end. With ``showSector`` each group names its sector.
 */
/** The primary ETF and coverage start in the column settings; the group cell shows them while hidden. */
const HIDDEN_COLUMNS = ["etf"];
const OPTIONAL = ["etf", ...PERIODS, "insider"] as const;

export function SectorGroupTable({ items, historyYears, sort, desc, setSort, selected, toggle, onlySelected, showSector, insider, columnsSlot }: {
  items: SectorGroupItem[];
  historyYears: number;
  sort: SortColumn;
  desc: boolean;
  setSort: (column: SortColumn, desc: boolean) => void;
  selected: readonly string[];
  toggle: (etf: string) => void;
  onlySelected: boolean;
  showSector: boolean;
  /** Open-market Insider totals by industry group (the last column). */
  insider: InsiderSectors | undefined;
  /** Where the column settings button goes (the page's toolbar). */
  columnsSlot: HTMLElement | null;
}) {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const name = (id: string) => t(`ui.sectors.group.${id}`);
  const insiderBy = useMemo(() => new Map((insider?.groups ?? []).map((row) => [row.id, row])), [insider]);
  const rows = useMemo(() => sortGroups(items, sort, desc, (id) => t(`ui.sectors.group.${id}`), (id) => insiderNet(insiderBy.get(id))),
    [items, sort, desc, t, insiderBy]);
  const choices = useColumnChoices("sectors.groups", HIDDEN_COLUMNS);
  const shown = columnVisibility([...OPTIONAL], HIDDEN_COLUMNS, choices.choices);
  const periods = PERIODS.filter((period) => shown[period]);
  const columnLabel = (id: (typeof OPTIONAL)[number]) => id === "etf" ? `${t("ui.sectors.column.primary")} · ${t("ui.sectors.column.coverage")}`
    : id === "insider" ? t("ui.sectors.column.insider", { count: INSIDER_DAYS }) : t(`ui.sectors.period.${id}`);
  const starts = useMemo(() => Object.fromEntries(PERIODS.map((period) => [period, commonStart(items, period)])), [items]);
  const header = (column: SortColumn, label: React.ReactNode, numeric = false, info?: React.ReactNode) => (
    <th className={cn("px-2 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground", numeric ? "text-right" : "text-left")}>
      <button type="button" onClick={() => setSort(column, sort === column ? !desc : column !== "name" && column !== "etf")}
        className={cn("inline-flex items-center gap-0.5 hover:text-foreground", sort === column && "text-foreground")}>
        {label}
        {sort === column ? desc ? <ArrowDown className="size-3"/> : <ArrowUp className="size-3"/> : null}
      </button>
      {info}
    </th>
  );
  const flip = (id: string) => setOpen((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      {columnsSlot && createPortal(
        <ColumnSettings changed={choices.changed} onReset={choices.reset} onChange={choices.set}
          columns={OPTIONAL.map((id) => ({ id, label: columnLabel(id), shown: shown[id] }))}/>, columnsSlot)}
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b">
            <th className="w-8 py-2 pl-3"><span className="sr-only">{t("ui.sectors.select")}</span></th>
            {header("name", t("ui.sectors.column.group"))}
            {shown.etf && header("etf", columnLabel("etf"))}
            {periods.map((period) => <Fragment key={period}>{header(period, (
              <span className="flex flex-col items-end leading-tight">
                <span>{t(`ui.sectors.period.${period}`)}</span>
                {starts[period] && <span className="text-xs font-normal text-muted-foreground">{t("ui.sectors.versus_close", { day: formatDay(starts[period]) })}</span>}
              </span>
            ), true)}</Fragment>)}
            {shown.insider && header("insider", <InsiderHeader/>, true, <InsiderInfo/>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const references = onlySelected ? item.alternates.filter((row) => selected.includes(row.etf)) : item.alternates;
            const expanded = open.has(item.id) || (onlySelected && references.length > 0);
            const label = name(item.id);
            return (
              <Fragment key={item.id}>
                <m.tr layout={reduce ? false : "position"} transition={{ duration: 0.25, ease: "easeOut" }}
                  className={cn("border-b hover:bg-accent/40", item.etf && selected.includes(item.etf) && "bg-selected", !item.etf && "text-muted-foreground")}>
                  <td className="py-2 pl-3">{item.etf && <Check etf={item.etf} label={label} selected={selected} toggle={toggle}/>}</td>
                  <td className="px-2 py-2">
                    <span className="flex flex-col">
                      <span className={cn("font-medium", !item.etf && "font-normal")}>{label}</span>
                      {(showSector || !shown.etf) && (
                        // The sector, then (with the ETF column hidden) the primary ETF and its coverage, on one line.
                        <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
                          {showSector && <span>{t(`ui.sectors.name.${item.sector}`)}</span>}
                          {showSector && !shown.etf && <span aria-hidden>·</span>}
                          {!shown.etf && <>
                            {item.etf ? <span className="inline-flex items-center gap-1 font-mono">{item.etf}<FetchMark fetch={item.fetch}/></span> : <span>—</span>}
                            <CoverageBadge coverage={item.coverage} same={item.same_as_sector}/>
                          </>}
                        </span>
                      )}
                      {item.etf && <SinceNote item={item} years={historyYears}/>}
                      {item.alternates.length > 0 && !onlySelected && (
                        <button type="button" onClick={() => flip(item.id)} aria-expanded={expanded}
                          className="inline-flex w-fit items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground">
                          <ChevronRight className={cn("size-3 transition-transform duration-200", expanded && "rotate-90")}/>
                          {t("ui.sectors.references", { count: item.alternates.length, etfs: item.alternates.map((row) => row.etf).join(", ") })}
                        </button>
                      )}
                    </span>
                  </td>
                  {/* The primary ETF and how well it covers the group share one cell. */}
                  {shown.etf && <td className="px-2 py-2">
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                      {item.etf
                        ? <span className="inline-flex items-center gap-1 font-mono text-xs">{item.etf}<FetchMark fetch={item.fetch}/></span>
                        : <span className="text-xs">—</span>}
                      <CoverageBadge coverage={item.coverage} same={item.same_as_sector}/>
                    </span>
                  </td>}
                  {item.periods
                    ? periods.map((period) => (
                      <td key={period} className="px-2 py-2 text-right">
                        <ChangeCell change={periodChange({ periods: item.periods! }, period)} period={period}/>
                      </td>
                    ))
                    : periods.length > 0 && <td colSpan={periods.length} className="px-2 py-2 text-right text-xs">{t("ui.sectors.data_pending")}</td>}
                  {shown.insider && (
                    <td className="px-2 py-2 text-right">
                      <InsiderNetCell totals={insiderBy.get(item.id)} data={insider} group={{ sector: item.sector }}/>
                    </td>
                  )}
                </m.tr>
                {expanded && references.map((row) => (
                  <ReferenceRow key={row.etf} row={row} group={label} selected={selected} toggle={toggle} historyYears={historyYears} shown={shown} periods={periods}/>
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
