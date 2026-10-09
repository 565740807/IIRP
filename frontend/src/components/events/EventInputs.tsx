import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Check, ChevronRight, CircleAlert, Copy, LoaderCircle, Pencil, Play, RotateCcw, Table2, Trash2 } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { BENCHMARKS, benchmarkName } from "@/lib/analysis";
import { SESSIONS, eventsJson, fiscalLabel, sessionLabel, useEventSets, type EventItem, type EventKind } from "@/lib/events";
import { formatDay, formatLocal } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const input = "h-8 w-full rounded-md border bg-background px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30";

/** A titled step of the conditions panel; folded steps keep the panel short. */
function Step({ title, open, children }: { title: ReactNode; open?: boolean; children: ReactNode }) {
  return (
    <details className="group border-b last:border-b-0" open={open}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2.5 text-sm font-semibold">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" />
        {title}
      </summary>
      <div className="space-y-2.5 px-3 pb-3">{children}</div>
    </details>
  );
}

function Label({ children }: { children: ReactNode }) {
  return <div className="text-xs font-medium text-muted-foreground">{children}</div>;
}

function fill(template: string, values: Record<string, string>) {
  return Object.entries(values).reduce((text, [key, value]) => (value.trim() ? text.split(`{{${key}}}`).join(value.trim()) : text), template);
}

/** ① The prompt template (zh and en): fill in, copy, edit, or restore the default. */
function PromptStep({ kind, open }: { kind: EventKind; open: boolean }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [language, setLanguage] = useState<"zh" | "en">(i18n.language === "zh" ? "zh" : "en");
  const [tickers, setTickers] = useState("AAPL");
  const [years, setYears] = useState("8");
  const [event, setEvent] = useState("");
  const [copied, setCopied] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const key = ["event-prompt", kind, language];
  const prompt = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/events/prompts/{kind}", { params: { path: { kind }, query: { language } }, signal })),
  });
  const save = useMutation({
    mutationFn: (text: string | null) =>
      text === null
        ? unwrap(client.DELETE("/api/v1/events/prompts/{kind}", { params: { path: { kind }, query: { language } } }))
        : unwrap(client.PUT("/api/v1/events/prompts/{kind}", { params: { path: { kind }, query: { language } }, body: { text } })),
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
      setDraft(null);
    },
  });
  const filled = prompt.data ? fill(prompt.data.text, { tickers, years, event }) : "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(filled);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setDialog(true);
    }
  };
  return (
    <Step title={t("ui.events.step.prompt")} open={open}>
      <div className="flex items-center justify-between gap-2">
        <Label>{t("ui.events.prompt.language")}</Label>
        <div className="inline-flex overflow-hidden rounded-md border text-xs">
          {(["en", "zh"] as const).map((value) => (
            <button key={value} type="button" aria-pressed={language === value} onClick={() => setLanguage(value)}
              className={cn("h-6 border-r px-2 last:border-r-0 hover:bg-accent", language === value && "bg-primary text-primary-foreground hover:bg-primary")}>
              {value === "en" ? "English" : "中文"}
            </button>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_4rem] gap-1.5">
        <label className="space-y-1">
          <Label>{t("ui.analysis.form.tickers")}</Label>
          <input value={tickers} onChange={(e) => setTickers(e.target.value.toUpperCase())} className={cn(input, "uppercase")} />
        </label>
        <label className="space-y-1">
          <Label>{t("ui.events.prompt.years")}</Label>
          <input type="number" min={1} max={30} value={years} onChange={(e) => setYears(e.target.value)} className={cn(input, "text-center tabular-nums")} />
        </label>
      </div>
      {kind === "custom" && (
        <label className="block space-y-1">
          <Label>{t("ui.events.prompt.event")}</Label>
          <input value={event} placeholder={t("ui.events.prompt.event_placeholder")} onChange={(e) => setEvent(e.target.value)} className={input} />
        </label>
      )}
      <div className="flex gap-1.5">
        <Button size="sm" className="flex-1" onClick={() => void copy()} disabled={!prompt.data}>
          {copied ? <Check /> : <Copy />}
          {copied ? t("ui.events.prompt.copied") : t("ui.events.prompt.copy")}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setDialog(true)} disabled={!prompt.data}>
          <Pencil />
          {t("ui.events.prompt.view_edit")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        {prompt.data?.is_default ? t("ui.events.prompt.default") : t("ui.events.prompt.custom", { time: formatLocal(prompt.data?.updated_at) })}
      </p>
      {prompt.error && <p className="text-xs text-destructive">{prompt.error.message}</p>}
      <Dialog open={dialog} onOpenChange={(value) => { setDialog(value); if (!value) setDraft(null); }}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t(`ui.events.prompt.title_${kind}`)} · {language === "en" ? "English" : "中文"}</DialogTitle>
            <DialogDescription>
              {t(kind === "custom" ? "ui.events.prompt.placeholders_custom" : "ui.events.prompt.placeholders", { tickers: "{{tickers}}", years: "{{years}}", event: "{{event}}" })}
            </DialogDescription>
          </DialogHeader>
          {draft === null ? (
            <textarea readOnly rows={16} value={filled} aria-label={t("ui.events.prompt.filled")} className="w-full resize-y rounded-md border bg-muted/30 p-2 font-mono text-xs" />
          ) : (
            <textarea rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={t("ui.events.prompt.edit")} className="w-full resize-y rounded-md border bg-background p-2 font-mono text-xs" />
          )}
          {save.error && <p className="text-xs text-destructive">{save.error.message}</p>}
          <DialogFooter>
            {draft === null ? (
              <>
                <Button variant="outline" onClick={() => setDraft(prompt.data?.text ?? "")}><Pencil />{t("ui.events.prompt.edit")}</Button>
                <Button onClick={() => void copy()}>{copied ? <Check /> : <Copy />}{copied ? t("ui.events.prompt.copied") : t("ui.events.prompt.copy")}</Button>
              </>
            ) : (
              <>
                <Button variant="ghost" disabled={prompt.data?.is_default || save.isPending} onClick={() => save.mutate(null)}><RotateCcw />{t("ui.events.prompt.reset")}</Button>
                <Button variant="outline" onClick={() => setDraft(null)}>{t("ui.cancel")}</Button>
                <Button disabled={!draft.trim() || save.isPending} onClick={() => save.mutate(draft)}>
                  {save.isPending && <LoaderCircle className="motion-safe:animate-spin" />}
                  {t("ui.events.prompt.save")}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Step>
  );
}

/** The events of a pasted list or a saved set; the session of each can be changed here. */
function PreviewDialog({
  kind,
  open,
  onOpenChange,
  title,
  events,
  saving,
  error,
  onSave,
}: {
  kind: EventKind;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  events: EventItem[];
  saving?: boolean;
  error?: Error | null;
  onSave?: (events: EventItem[]) => void;
}) {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<Record<number, EventItem["session"]>>({});
  useEffect(() => setSessions({}), [events, open]);
  const changed = Object.entries(sessions).filter(([index, session]) => events[Number(index)]?.session !== session).length;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{t("ui.events.preview.hint")}</DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card">
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="px-2 py-1.5 pl-3 font-medium">#</th>
                <th className="px-2 py-1.5 font-medium">{t("ui.analysis.col.ticker")}</th>
                <th className="px-2 py-1.5 font-medium">{t("ui.events.col.event")}</th>
                <th className="px-2 py-1.5 font-medium">{t("ui.events.col.date")}</th>
                <th className="px-2 py-1.5 font-medium">{t("ui.events.col.session")}</th>
                <th className="px-2 py-1.5 pr-3 font-medium">R</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event, index) => {
                const session = sessions[index] ?? event.session;
                return (
                  <tr key={index} className="border-b last:border-b-0">
                    <td className="px-2 py-1 pl-3 text-muted-foreground tabular-nums">{index + 1}</td>
                    <td className="px-2 py-1 font-semibold">{event.ticker}</td>
                    <td className="px-2 py-1">
                      {event.name}
                      {kind === "earnings" && <span className="ml-1 text-xs text-muted-foreground">{fiscalLabel(event)}</span>}
                      {event.note && <span className="block text-xs text-muted-foreground">{event.note}</span>}
                    </td>
                    <td className="px-2 py-1 whitespace-nowrap tabular-nums">{formatDay(event.date, { year: true })}</td>
                    <td className="px-2 py-1">
                      {onSave ? (
                        <select
                          value={session}
                          aria-label={t("ui.events.col.session")}
                          onChange={(e) => setSessions({ ...sessions, [index]: e.target.value as EventItem["session"] })}
                          className={cn("h-7 rounded-md border bg-background px-1 text-xs", session !== event.session && "border-primary font-medium")}
                        >
                          {SESSIONS.map((value) => <option key={value} value={value}>{sessionLabel(value)}</option>)}
                        </select>
                      ) : (
                        sessionLabel(event.session)
                      )}
                    </td>
                    <td className="px-2 py-1 pr-3 whitespace-nowrap tabular-nums">
                      {session === event.session ? (event.reaction_date ? formatDay(event.reaction_date, { year: true }) : "—") : t("ui.events.preview.after_save")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {error && <p className="text-xs text-destructive">{error.message}</p>}
        {onSave && (
          <DialogFooter>
            <span className="mr-auto self-center text-xs text-muted-foreground">{t("ui.events.preview.changed", { count: changed })}</span>
            <Button variant="outline" onClick={() => onOpenChange(false)}>{t("ui.cancel")}</Button>
            <Button disabled={!changed || saving} onClick={() => onSave(events.map((event, index) => ({ ...event, session: sessions[index] ?? event.session })))}>
              {saving && <LoaderCircle className="motion-safe:animate-spin" />}
              {t("ui.events.preview.save")}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

type Validation = Schemas["EventValidateOutput"];

/** ② Paste the JSON from the other AI: checked as it is pasted, each problem listed. */
function PasteStep({ kind, open, text, setText, checked }: { kind: EventKind; open: boolean; text: string; setText: (text: string) => void; checked: Validation | null }) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState(false);
  return (
    <Step title={t("ui.events.step.paste")} open={open}>
      <textarea
        rows={5}
        value={text}
        spellCheck={false}
        placeholder={'{"events":[{"ticker":"AAPL","date":"2025-10-30","session":"after_close", …}]}'}
        onChange={(e) => setText(e.target.value)}
        aria-label={t("ui.events.paste.label")}
        className="w-full resize-y rounded-md border bg-background p-2 font-mono text-xs outline-none focus-visible:border-ring"
      />
      {checked && !checked.valid && (
        <div role="alert" className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
          <p className="flex items-center gap-1 font-medium"><CircleAlert className="size-3.5" />{t("ui.events.paste.errors", { count: checked.errors.length })}</p>
          <ul className="list-disc space-y-0.5 pl-4">
            {checked.errors.map((issue, index) => (
              <li key={index}>
                {issue.index ? t("ui.events.paste.item", { index: issue.index }) : t("ui.events.paste.whole")}
                {issue.field ? <code className="mx-1 rounded bg-destructive/10 px-1">{issue.field}</code> : " "}
                {tm(issue.message)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {checked?.valid && (
        <div className="flex items-center gap-2 text-xs">
          <span className="flex min-w-0 items-center gap-1 text-foreground">
            <Check className="size-3.5 shrink-0 text-up" />
            <span className="truncate">{t("ui.events.paste.valid", { count: checked.events.length, tickers: checked.tickers.join(", ") })}</span>
          </span>
          <button type="button" onClick={() => setPreview(true)} className="ml-auto inline-flex shrink-0 items-center gap-1 font-medium underline-offset-2 hover:underline">
            <Table2 className="size-3.5" />
            {t("ui.events.paste.preview")}
          </button>
        </div>
      )}
      {checked?.valid && (
        <PreviewDialog
          kind={kind}
          open={preview}
          onOpenChange={setPreview}
          title={t("ui.events.paste.preview_title", { count: checked.events.length })}
          events={checked.events}
          onSave={(events) => {
            // The edited list becomes the pasted text, which is checked again.
            setText(eventsJson(events));
            setPreview(false);
          }}
        />
      )}
    </Step>
  );
}

/** n and the benchmark: they apply at once to the analysis shown, and to the next one. */
function Choices({ n, setN, presets, defaultN, min, max, benchmark, setBenchmark, busy }: {
  n: number;
  setN: (n: number | null) => void;
  presets: number[];
  defaultN: number;
  min: number;
  max: number;
  benchmark: string | null;
  setBenchmark: (value: string | null) => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const custom = !presets.includes(n);
  const [draft, setDraft] = useState(custom ? String(n) : "");
  useEffect(() => setDraft(custom ? String(n) : ""), [n, custom]);
  const [other, setOther] = useState(benchmark && !(BENCHMARKS as readonly string[]).includes(benchmark) ? benchmark : "");
  const choice = benchmark === null ? "none" : (BENCHMARKS as readonly string[]).includes(benchmark) ? benchmark : "other";
  const apply = () => {
    const value = Number(draft);
    if (Number.isInteger(value) && value >= min && value <= max) setN(value);
    else setDraft(custom ? String(n) : "");
  };
  return (
    <div className="space-y-3 border-b px-3 py-3">
      <div className="space-y-1.5">
        <div className="flex items-center gap-1.5">
          <Label>{t("ui.events.n_label")}</Label>
          {busy && <LoaderCircle className="size-3 text-muted-foreground motion-safe:animate-spin" />}
          {n !== defaultN && (
            <button type="button" onClick={() => setN(null)} className="ml-auto text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
              {t("ui.window.n_reset", { n: defaultN })}
            </button>
          )}
        </div>
        <div className="flex items-center gap-1.5" role="group" aria-label={t("ui.events.n_label")}>
          <div className="inline-flex overflow-hidden rounded-md border">
            {presets.map((value) => (
              <button key={value} type="button" aria-pressed={n === value} onClick={() => setN(value)}
                className={cn("h-8 min-w-8 border-r px-2 text-sm tabular-nums last:border-r-0 hover:bg-accent", n === value && "bg-primary text-primary-foreground hover:bg-primary")}>
                {value}
              </button>
            ))}
          </div>
          <input
            inputMode="numeric"
            aria-label={t("ui.window.n_custom")}
            placeholder={t("ui.window.n_custom_short")}
            value={draft}
            onChange={(e) => setDraft(e.target.value.replace(/\D/g, "").slice(0, 2))}
            onBlur={() => draft && apply()}
            onKeyDown={(e) => e.key === "Enter" && apply()}
            className={cn(input, "w-14 text-center tabular-nums", custom && "border-primary font-medium")}
          />
        </div>
        <p className="text-xs text-muted-foreground">{t("ui.events.n_hint", { n })}</p>
      </div>
      <div className="space-y-1">
        <Label>{t("ui.analysis.form.benchmark")}</Label>
        <div role="radiogroup" aria-label={t("ui.analysis.form.benchmark")}>
          {[...BENCHMARKS, "none", "other"].map((value) => (
            <label key={value} className="flex h-7 cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                name="event-benchmark"
                checked={choice === value}
                onChange={() => {
                  if (value === "none") setBenchmark(null);
                  else if (value !== "other") setBenchmark(value);
                  else if (other.trim()) setBenchmark(other.trim());
                }}
                className="accent-primary"
              />
              {value === "other" ? (
                <input
                  value={other}
                  placeholder={t("ui.analysis.form.benchmark_other")}
                  aria-label={t("ui.analysis.form.benchmark_other")}
                  onChange={(e) => setOther(e.target.value.toUpperCase())}
                  onBlur={() => other.trim() && setBenchmark(other.trim())}
                  onKeyDown={(e) => e.key === "Enter" && other.trim() && setBenchmark(other.trim())}
                  className={cn(input, "h-7 uppercase placeholder:normal-case")}
                />
              ) : value === "none" ? (
                t("ui.analysis.form.benchmark_none")
              ) : (
                benchmarkName(value)
              )}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

/** Saved sets of this kind: open one to analyze, rename, edit sessions or delete. */
function SavedSets({ kind, n, benchmark, current, onAnalysis }: { kind: EventKind; n: number; benchmark: string | null; current: string | null; onAnalysis: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const sets = useEventSets(kind);
  const [selected, setSelected] = useState<string | null>(current);
  useEffect(() => setSelected((value) => current ?? value), [current]);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [preview, setPreview] = useState(false);
  const detail = useQuery({
    queryKey: ["event-set", selected],
    enabled: !!selected && preview,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/events/sets/{set_id}", { params: { path: { set_id: selected! } }, signal })),
  });
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["event-sets", kind] });
    void queryClient.invalidateQueries({ queryKey: ["event-set"] });
  };
  const analyze = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.POST("/api/v1/events/sets/{set_id}/analyses", { params: { path: { set_id: id } }, body: { request_id: crypto.randomUUID(), n, benchmark } })),
    onSuccess: (created) => onAnalysis(created.analysis_id),
  });
  const update = useMutation({
    mutationFn: ({ id, ...body }: { id: string; title?: string; text?: string }) =>
      unwrap(client.PUT("/api/v1/events/sets/{set_id}", { params: { path: { set_id: id } }, body: { ...body, language: i18n.language.startsWith("zh") ? "zh" : "en" } })),
    onSuccess: (_data, variables) => {
      invalidate();
      setRenaming(null);
      if (variables.text) {
        setPreview(false);
        analyze.mutate(variables.id);
      }
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(client.DELETE("/api/v1/events/sets/{set_id}", { params: { path: { set_id: id } } })),
    onSuccess: () => {
      setSelected(null);
      invalidate();
    },
  });
  const items = sets.data?.items ?? [];
  const error = sets.error ?? analyze.error ?? update.error ?? remove.error;
  return (
    <Step title={t("ui.events.step.saved", { count: items.length })} open>
      {sets.isLoading && <p className="text-xs text-muted-foreground">{t("ui.loading")}</p>}
      {!sets.isLoading && !items.length && <p className="text-xs text-muted-foreground">{t("ui.events.saved.empty")}</p>}
      <ul className="max-h-72 space-y-1 overflow-y-auto" role="listbox" aria-label={t("ui.events.step.saved", { count: items.length })}>
        {items.map((item) => {
          const active = item.id === selected;
          return (
            <li key={item.id} role="option" aria-selected={active} className={cn("rounded-md border", active ? "border-primary bg-accent/60" : "hover:bg-accent/40")}>
              {renaming === item.id ? (
                <form
                  className="flex gap-1 p-1.5"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const title = new FormData(e.currentTarget).get("title")?.toString().trim();
                    if (title) update.mutate({ id: item.id, title });
                  }}
                >
                  <input name="title" defaultValue={item.title} maxLength={200} autoFocus aria-label={t("ui.events.saved.rename")} className={cn(input, "h-7")} />
                  <Button size="sm" type="submit" disabled={update.isPending}>{t("ui.save")}</Button>
                </form>
              ) : (
                <button type="button" onClick={() => setSelected(active ? null : item.id)} className="block w-full px-2 py-1.5 text-left">
                  <span className="block truncate text-sm font-medium">{item.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {t("ui.events.saved.meta", { tickers: item.tickers.join(", "), count: item.event_count, first: item.first_date?.slice(0, 4), last: item.last_date?.slice(0, 4) })}
                  </span>
                </button>
              )}
              {active && renaming !== item.id && (
                <div className="flex flex-wrap gap-1 border-t px-1.5 py-1.5">
                  <Button size="xs" onClick={() => analyze.mutate(item.id)} disabled={analyze.isPending}>
                    {analyze.isPending ? <LoaderCircle className="motion-safe:animate-spin" /> : <Play />}
                    {t("ui.events.saved.analyze", { n })}
                  </Button>
                  <Button size="xs" variant="outline" onClick={() => setPreview(true)}><Table2 />{t("ui.events.saved.events")}</Button>
                  <Button size="xs" variant="ghost" onClick={() => setRenaming(item.id)}><Pencil />{t("ui.events.saved.rename")}</Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    className="text-destructive"
                    onClick={() => window.confirm(t("ui.events.saved.delete_confirm", { title: item.title })) && remove.mutate(item.id)}
                  >
                    <Trash2 />
                    {t("ui.events.saved.delete")}
                  </Button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {error && <p className="text-xs text-destructive">{error.message}</p>}
      {selected && detail.data && (
        <PreviewDialog
          kind={kind}
          open={preview}
          onOpenChange={setPreview}
          title={detail.data.title}
          events={detail.data.events}
          saving={update.isPending}
          error={update.error}
          onSave={(events) => update.mutate({ id: selected, text: eventsJson(events) })}
        />
      )}
    </Step>
  );
}

/**
 * The conditions panel of the earnings and events tabs: ① copy the prompt →
 * ② paste the JSON → save and analyze; n and the benchmark; the saved sets.
 */
export function EventInputs({
  kind,
  choices,
  busy,
  currentSet,
  onAnalysis,
}: {
  kind: EventKind;
  choices: {
    n: number;
    setN: (n: number | null) => void;
    presets: number[];
    defaultN: number;
    min: number;
    max: number;
    benchmark: string | null;
    setBenchmark: (value: string | null) => void;
  };
  busy: boolean;
  currentSet: string | null;
  onAnalysis: (id: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const sets = useEventSets(kind);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [checked, setChecked] = useState<Validation | null>(null);
  const [checkError, setCheckError] = useState<Error | null>(null);
  useEffect(() => {
    if (!text.trim()) {
      setChecked(null);
      return;
    }
    let current = true;
    const timer = window.setTimeout(() => {
      unwrap(client.POST("/api/v1/events/validate", { body: { kind, text } }))
        .then((value) => current && (setChecked(value), setCheckError(null)))
        .catch((error: Error) => current && setCheckError(error));
    }, 400);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [text, kind]);
  const create = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/events/sets", {
        body: { kind, language: i18n.language.startsWith("zh") ? "zh" : "en", text, title: title.trim() || null, request_id: crypto.randomUUID(), analyze: true, n: choices.n, benchmark: choices.benchmark },
      })),
    onSuccess: (created) => {
      setText("");
      setTitle("");
      void queryClient.invalidateQueries({ queryKey: ["event-sets", kind] });
      if (created.analysis) onAnalysis(created.analysis.analysis_id);
    },
  });
  const first = useMemo(() => !sets.isLoading && !(sets.data?.items.length ?? 0), [sets.isLoading, sets.data]);
  return (
    <div>
      <PromptStep kind={kind} open={first} />
      <PasteStep kind={kind} open={first || !!text} text={text} setText={setText} checked={checked} />
      {checkError && <p className="px-3 pb-2 text-xs text-destructive">{checkError.message}</p>}
      <Choices {...choices} busy={busy} />
      {checked?.valid && (
        <div className="space-y-2 border-b px-3 py-3">
          <input value={title} maxLength={200} placeholder={t("ui.events.paste.title_placeholder")} aria-label={t("ui.events.paste.title")} onChange={(e) => setTitle(e.target.value)} className={input} />
          <Button className="w-full" disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <LoaderCircle className="motion-safe:animate-spin" /> : <Play />}
            {t("ui.events.paste.save_analyze")}
          </Button>
          {create.error && <p className="text-xs text-destructive">{create.error.message}</p>}
        </div>
      )}
      <SavedSets kind={kind} n={choices.n} benchmark={choices.benchmark} current={currentSet} onAnalysis={onAnalysis} />
    </div>
  );
}
