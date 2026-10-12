import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Check, LoaderCircle, Settings as SettingsIcon } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import {
  MIN_CONTRAST, PRESET_ORDER, SURFACES, colorChoice, contrast, paletteOf, resolve, setColorChoice, weakestContrast,
  type ColorChoice, type Palette, type Preset,
} from "@/lib/colors";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

import { LongPricesSection } from "@/components/shell/LongPrices";

type Display = Schemas["DisplayOutput"];
const DISPLAY_KEY = ["display"];

/** The instance's display settings; the colors they name are applied as soon as they arrive. */
export function useDisplaySettings() {
  const query = useQuery({ queryKey: DISPLAY_KEY, queryFn: () => unwrap(client.GET("/api/v1/display")), staleTime: 60_000 });
  const colors = query.data?.colors;
  useEffect(() => {
    if (colors) setColorChoice(colors);
  }, [colors]);
  return query;
}

/** Two candles, a gain and a loss, and buy/sell marks in one palette. */
function Preview({ palette }: { palette: Palette }) {
  return (
    <span className="flex items-center gap-2 rounded-md border bg-card px-2 py-1.5" aria-hidden>
      <svg viewBox="0 0 28 24" className="h-6 w-7 shrink-0">
        <line x1="7" y1="2" x2="7" y2="22" stroke={palette.up} strokeWidth="1.5" />
        <rect x="3" y="6" width="8" height="11" fill={palette.up} />
        <line x1="21" y1="3" x2="21" y2="21" stroke={palette.down} strokeWidth="1.5" />
        <rect x="17" y="7" width="8" height="10" fill={palette.down} />
      </svg>
      <span className="flex flex-col text-xs leading-4 font-semibold tabular-nums">
        <span style={{ color: palette.up }}>▲ +1.25%</span>
        <span style={{ color: palette.down }}>▼ −0.84%</span>
      </span>
      <span className="ml-auto flex flex-col gap-0.5">
        <span className="h-1.5 w-8 rounded-full" style={{ background: palette.upSoft, boxShadow: `inset 3px 0 0 ${palette.up}` }} />
        <span className="h-1.5 w-8 rounded-full" style={{ background: palette.downSoft, boxShadow: `inset 3px 0 0 ${palette.down}` }} />
      </span>
    </span>
  );
}

function ContrastWarning({ side, color }: { side: "up" | "down"; color: string }) {
  const { t } = useTranslation();
  if (weakestContrast(color) >= MIN_CONTRAST) return null;
  return (
    <p className="flex gap-1.5 text-xs text-warn" role="status">
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
      {t("ui.settings.colors.low_contrast", {
        side: t(`ui.settings.colors.${side}`),
        ratio: formatNumber(contrast(color, SURFACES.card), 1),
        minimum: formatNumber(MIN_CONTRAST, 1),
      })}
    </p>
  );
}

/** Up/down colors: four presets with previews; custom adds two color pickers and a contrast check. */
function ColorSection({ display }: { display?: Display }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // The window's content mounts on open, so it starts from the colors in use.
  const [choice, setChoice] = useState<ColorChoice>(() => display?.colors ?? colorChoice());
  const save = useMutation({
    mutationFn: (colors: ColorChoice) =>
      unwrap(client.PUT("/api/v1/display", { body: { colors: { preset: colors.preset, up: colors.up ?? null, down: colors.down ?? null } } })),
    onSuccess: (data) => queryClient.setQueryData(DISPLAY_KEY, data),
  });
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const choose = (next: ColorChoice, delay = 0) => {
    setChoice(next);
    setColorChoice(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => save.mutate(next), delay);
  };
  const custom = resolve({ preset: "custom", up: choice.up, down: choice.down });
  const pick = (preset: Preset) => {
    if (preset !== "custom") return choose({ ...choice, preset });
    // The first custom choice starts from the colors in use.
    const start = choice.up && choice.down ? custom : resolve(choice);
    choose({ preset, up: start.up, down: start.down });
  };
  return (
    <section className="space-y-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{t("ui.settings.colors.title")}</h3>
        <span className="flex items-center gap-1 text-xs text-muted-foreground" role="status">
          {save.isPending ? <><LoaderCircle className="size-3 motion-safe:animate-spin" />{t("ui.settings.saving")}</>
            : save.isSuccess ? <><Check className="size-3 text-ok" />{t("ui.settings.saved")}</> : null}
        </span>
      </div>
      <div role="radiogroup" aria-label={t("ui.settings.colors.title")} className="grid grid-cols-2 gap-2">
        {PRESET_ORDER.map((preset) => {
          const active = choice.preset === preset;
          const palette = preset === "custom" ? paletteOf({ preset, up: custom.up, down: custom.down }) : paletteOf({ preset });
          return (
            <button
              key={preset}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => pick(preset)}
              className={cn(
                "flex flex-col gap-1.5 rounded-lg border p-2.5 text-left transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none",
                active && "border-primary ring-1 ring-primary",
              )}
            >
              <span className="flex items-center justify-between gap-2 text-sm font-medium">
                {t(`ui.settings.colors.preset.${preset}`)}
                {active && <Check className="size-4" />}
              </span>
              <span className="text-xs text-muted-foreground">{t(`ui.settings.colors.note.${preset}`)}</span>
              <Preview palette={palette} />
            </button>
          );
        })}
      </div>
      {choice.preset === "custom" && (
        <div className="space-y-1.5 rounded-lg border bg-secondary/40 p-2.5">
          <div className="flex flex-wrap gap-4">
            {(["up", "down"] as const).map((side) => (
              <label key={side} className="flex items-center gap-2 text-sm">
                <input
                  type="color"
                  value={custom[side]}
                  onChange={(event) => choose({ ...choice, preset: "custom", up: custom.up, down: custom.down, [side]: event.target.value }, 400)}
                  className="h-7 w-10 cursor-pointer rounded border bg-card p-0.5"
                />
                <span>{t(`ui.settings.colors.${side}`)}</span>
                <code className="text-xs text-muted-foreground">{custom[side]}</code>
              </label>
            ))}
          </div>
          <ContrastWarning side="up" color={custom.up} />
          <ContrastWarning side="down" color={custom.down} />
        </div>
      )}
      <p className="text-xs text-muted-foreground">{t("ui.settings.colors.hint")}</p>
      {save.error && <p role="alert" className="text-xs text-destructive">{save.error.message}</p>}
    </section>
  );
}

/** Which instruments the home strip shows; each change is saved at once and the strip re-read. */
function StripSection({ display }: { display?: Display }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: (symbols: string[]) => unwrap(client.PUT("/api/v1/display", { body: { market_strip: { symbols } } })),
    onSuccess: (data) => {
      queryClient.setQueryData(DISPLAY_KEY, data);
      void queryClient.invalidateQueries({ queryKey: ["home"] });
    },
  });
  if (!display) return null;
  const pending = save.isPending ? save.variables : null;
  const selected = new Set(pending ?? display.market_strip.symbols);
  const defaults = display.instruments.filter((item) => item.default).map((item) => item.symbol);
  const toggle = (symbol: string) => {
    const next = new Set(selected);
    if (next.has(symbol)) next.delete(symbol);
    else next.add(symbol);
    save.mutate(display.instruments.map((item) => item.symbol).filter((item) => next.has(item)));
  };
  const isDefault = defaults.length === selected.size && defaults.every((symbol) => selected.has(symbol));
  return (
    <section className="space-y-2.5 border-t pt-4">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{t("ui.settings.strip.title")}</h3>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          {save.isPending ? <><LoaderCircle className="size-3 motion-safe:animate-spin" />{t("ui.settings.saving")}</>
            : save.isSuccess ? <><Check className="size-3 text-ok" />{t("ui.settings.saved")}</> : null}
          <Button size="xs" variant="ghost" disabled={isDefault || save.isPending} onClick={() => save.mutate(defaults)}>
            {t("ui.settings.strip.reset")}
          </Button>
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        {display.instruments.map((item) => {
          const checked = selected.has(item.symbol);
          return (
            <label key={item.symbol} className="flex min-w-0 cursor-pointer items-start gap-2 rounded px-1 py-1 text-sm hover:bg-accent/60">
              <input
                type="checkbox"
                className="mt-1 size-3.5 shrink-0 accent-primary"
                checked={checked}
                // At least one instrument stays on the strip.
                disabled={checked && selected.size === 1}
                onChange={() => toggle(item.symbol)}
              />
              <span className="flex min-w-0 flex-col">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="truncate">{tm(item.name)}</span>
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{item.symbol}</span>
                </span>
                {(item.off_hours_only || !item.default) && (
                  <span className="text-xs text-muted-foreground">
                    {t(item.off_hours_only ? "ui.settings.strip.off_hours" : "ui.settings.strip.optional")}
                  </span>
                )}
              </span>
            </label>
          );
        })}
      </div>
      <p className="text-xs text-muted-foreground">{t("ui.settings.strip.hint")}</p>
      {save.error && <p role="alert" className="text-xs text-destructive">{save.error.message}</p>}
    </section>
  );
}

/** Gear button in the top bar; opens the small settings window. */
export function SettingsButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const display = useDisplaySettings();
  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" onClick={() => setOpen(true)} aria-label={t("ui.settings.open")}>
            <SettingsIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{t("ui.settings.open")}</TooltipContent>
      </Tooltip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{t("ui.settings.title")}</DialogTitle>
            <DialogDescription>{t("ui.settings.description")}</DialogDescription>
          </DialogHeader>
          <ColorSection display={display.data} />
          <StripSection display={display.data} />
          <LongPricesSection />
        </DialogContent>
      </Dialog>
    </>
  );
}
