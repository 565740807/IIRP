import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "motion/react";
import { ArrowDown, ArrowUp, ChevronRight } from "lucide-react";
import { formatDay } from "@/lib/format";
import { PERIODS, periodChange, periodRank, type EtfPerformance, type SectorGroupItem, type SortColumn } from "@/lib/sectors";
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
          <span className={cn("cursor-default rounded border px-1.5 py-px text-[11px] whitespace-nowrap",
            coverage === "complete" && "border-foreground/30 text-foreground",
            coverage === "partial" && "border-foreground/20 text-muted-foreground",
            coverage === "reference" && "border-dashed border-foreground/25 text-muted-foreground",
            coverage === "pending" && "border-transparent bg-secondary text-muted-foreground")}>
            {t(`ui.sectors.coverage.${coverage}`)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{t(`ui.sectors.coverage_rule.${coverage}`)}</TooltipContent>
      </Tooltip>
      {same && <span className="rounded bg-secondary px-1.5 py-px text-[11px] whitespace-nowrap text-muted-foreground">{t("ui.sectors.same_as_sector")}</span>}
    </span>
  );
}

/**
 * Ranked groups first, in the chosen order: a period column follows the
 * backend's rank, which only primaries have; groups without a value follow,
 * and pending groups always come last.
 */
export function sortGroups(items: readonly SectorGroupItem[], sort: SortColumn, desc: boolean, name: (id: string) => string) {
  const ranked = items.filter((item) => item.periods != null);
  const pending = items.filter((item) => item.periods == null);
  const direction = desc ? 1 : -1;
  const ordered = [...ranked].sort((a, b) => {
    if (sort === "name") return -direction * name(a.id).localeCompare(name(b.id));
    if (sort === "etf") return -direction * (a.etf ?? "").localeCompare(b.etf ?? "");
    const left = periodRank(a, sort), right = periodRank(b, sort);
    if (left == null || right == null) return left == null ? (right == null ? 0 : 1) : -1;
    return direction * (left - right);
  });
  return [...ordered, ...pending];
}

function Check({ etf, label, selected, toggle }: { etf: string; label: string; selected: readonly string[]; toggle: (etf: string) => void }) {
  const { t } = useTranslation();
  return <input type="checkbox" aria-label={t("ui.sectors.select_one", { name: label })} checked={selected.includes(etf)} onChange={() => toggle(etf)}/>;
}

function ReferenceRow({ row, group, selected, toggle, historyYears }: {
  row: EtfPerformance; group: string; selected: readonly string[]; toggle: (etf: string) => void; historyYears: number;
}) {
  const { t } = useTranslation();
  const note = t(`ui.sectors.etf_note.${row.etf}`, { defaultValue: "" });
  return (
    <tr className={cn("border-b border-dashed bg-muted/40 text-xs text-muted-foreground", selected.includes(row.etf) && "bg-up-soft/40")}>
      <td className="py-1.5 pl-3"><Check etf={row.etf} label={`${group} · ${row.etf}`} selected={selected} toggle={toggle}/></td>
      <td className="py-1.5 pr-2 pl-6">
        <span className="flex flex-col border-l-2 border-foreground/15 pl-2">
          <span>{t("ui.sectors.reference_row")}{note && <> · {note}</>}</span>
          <SinceNote item={row} years={historyYears}/>
        </span>
      </td>
      <td className="px-2 py-1.5"><span className="inline-flex items-center gap-1 font-mono">{row.etf}<FetchMark fetch={row.fetch}/></span></td>
      <td className="px-2 py-1.5"><CoverageBadge coverage="reference"/></td>
      {PERIODS.map((period) => (
        <td key={period} className="px-2 py-1.5 text-right opacity-80"><ChangeCell change={periodChange(row, period)} period={period}/></td>
      ))}
    </tr>
  );
}

/**
 * Industry groups with their primary ETF, coverage and every period. Only
 * primaries are ranked; reference rows open under their group and pending
 * groups stay at the end. With ``showSector`` each group names its sector.
 */
export function SectorGroupTable({ items, historyYears, sort, desc, setSort, selected, toggle, onlySelected, showSector }: {
  items: SectorGroupItem[];
  historyYears: number;
  sort: SortColumn;
  desc: boolean;
  setSort: (column: SortColumn, desc: boolean) => void;
  selected: readonly string[];
  toggle: (etf: string) => void;
  onlySelected: boolean;
  showSector: boolean;
}) {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const name = (id: string) => t(`ui.sectors.group.${id}`);
  const rows = useMemo(() => sortGroups(items, sort, desc, (id) => t(`ui.sectors.group.${id}`)), [items, sort, desc, t]);
  const starts = useMemo(() => Object.fromEntries(PERIODS.map((period) => [period, commonStart(items, period)])), [items]);
  const header = (column: SortColumn, label: React.ReactNode, numeric = false) => (
    <th className={cn("px-2 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground", numeric ? "text-right" : "text-left")}>
      <button type="button" onClick={() => setSort(column, sort === column ? !desc : column !== "name" && column !== "etf")}
        className={cn("inline-flex items-center gap-0.5 hover:text-foreground", sort === column && "text-foreground")}>
        {label}
        {sort === column ? desc ? <ArrowDown className="size-3"/> : <ArrowUp className="size-3"/> : null}
      </button>
    </th>
  );
  const flip = (id: string) => setOpen((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b">
            <th className="w-8 py-2 pl-3"><span className="sr-only">{t("ui.sectors.select")}</span></th>
            {header("name", t("ui.sectors.column.group"))}
            {header("etf", t("ui.sectors.column.primary"))}
            <th className="px-2 py-2 text-left text-xs font-medium whitespace-nowrap text-muted-foreground">{t("ui.sectors.column.coverage")}</th>
            {PERIODS.map((period) => <Fragment key={period}>{header(period, (
              <span className="flex flex-col items-end leading-tight">
                <span>{t(`ui.sectors.period.${period}`)}</span>
                {starts[period] && <span className="text-[10px] font-normal text-muted-foreground">{t("ui.sectors.versus_close", { day: formatDay(starts[period]) })}</span>}
              </span>
            ), true)}</Fragment>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const references = onlySelected ? item.alternates.filter((row) => selected.includes(row.etf)) : item.alternates;
            const expanded = open.has(item.id) || (onlySelected && references.length > 0);
            const label = name(item.id);
            return (
              <Fragment key={item.id}>
                <motion.tr layout={reduce ? false : "position"} transition={{ duration: 0.25, ease: "easeOut" }}
                  className={cn("border-b hover:bg-accent/40", item.etf && selected.includes(item.etf) && "bg-up-soft/60", !item.etf && "text-muted-foreground")}>
                  <td className="py-2 pl-3">{item.etf && <Check etf={item.etf} label={label} selected={selected} toggle={toggle}/>}</td>
                  <td className="px-2 py-2">
                    <span className="flex flex-col">
                      <span className={cn("font-medium", !item.etf && "font-normal")}>{label}</span>
                      {showSector && <span className="text-[11px] text-muted-foreground">{t(`ui.sectors.name.${item.sector}`)}</span>}
                      {item.etf && <SinceNote item={item} years={historyYears}/>}
                      {item.alternates.length > 0 && !onlySelected && (
                        <button type="button" onClick={() => flip(item.id)} aria-expanded={expanded}
                          className="inline-flex w-fit items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground">
                          <ChevronRight className={cn("size-3 transition-transform duration-200", expanded && "rotate-90")}/>
                          {t("ui.sectors.references", { count: item.alternates.length, etfs: item.alternates.map((row) => row.etf).join(", ") })}
                        </button>
                      )}
                    </span>
                  </td>
                  <td className="px-2 py-2">
                    {item.etf
                      ? <span className="inline-flex items-center gap-1 font-mono text-xs">{item.etf}<FetchMark fetch={item.fetch}/></span>
                      : <span className="text-xs">—</span>}
                  </td>
                  <td className="px-2 py-2"><CoverageBadge coverage={item.coverage} same={item.same_as_sector}/></td>
                  {item.periods
                    ? PERIODS.map((period) => (
                      <td key={period} className="px-2 py-2 text-right">
                        <ChangeCell change={periodChange({ periods: item.periods! }, period)} period={period}/>
                      </td>
                    ))
                    : <td colSpan={PERIODS.length} className="px-2 py-2 text-right text-xs">{t("ui.sectors.data_pending")}</td>}
                </motion.tr>
                {expanded && references.map((row) => (
                  <ReferenceRow key={row.etf} row={row} group={label} selected={selected} toggle={toggle} historyYears={historyYears}/>
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
