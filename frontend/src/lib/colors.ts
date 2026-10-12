import { useSyncExternalStore } from "react";

/**
 * Up/down colors. The saved choice (a preset, or two custom colors) comes from
 * the server's display settings; every text color, background, flash and chart
 * reads the palette derived here, so a change applies everywhere at once. The
 * CSS tokens (--up, --up-soft, --up-flash and the same for down) are set on the
 * root element; charts read the same values through usePalette(). The last
 * choice is cached in this browser so the first paint already uses it.
 */
export type Preset = "us" | "colorblind" | "asia" | "custom";
export type ColorChoice = { preset: Preset; up?: string | null; down?: string | null };
export type Palette = {
  up: string; upSoft: string; upFlash: string;
  down: string; downSoft: string; downFlash: string;
};

export const PRESETS: Record<Exclude<Preset, "custom">, { up: string; down: string }> = {
  us: { up: "#137333", down: "#c5221f" },
  colorblind: { up: "#005ccc", down: "#b24500" },
  asia: { up: "#c5221f", down: "#137333" },
};
export const PRESET_ORDER: Preset[] = ["us", "colorblind", "asia", "custom"];
export const DEFAULT_CHOICE: ColorChoice = { preset: "us", up: null, down: null };

/** Backgrounds the colored text sits on: cards and the page. */
export const SURFACES = { card: "#ffffff", page: "#f9fafb" } as const;
/** WCAG AA for normal text. */
export const MIN_CONTRAST = 4.5;
const SOFT = 0.08;
const FLASH = 0.14;

/** Neutral chart colors shared by every ECharts chart. */
export const CHART = {
  ink: "#334155",
  muted: "#64748b",
  grid: "#eef2f6",
  axis: "#cbd5e1",
  band: "#94a3b8",
  box: "#e2e8f0",
  tile: "#f1f5f9",
  surface: SURFACES.card,
  darkLabel: "#0f172a",
  lightLabel: "#ffffff",
  /** Axis, label and legend text of every chart, in pixels (the smallest size used in the interface). */
  font: 12,
} as const;

const HEX = /^#[0-9a-f]{6}$/i;
export const isHex = (value: unknown): value is string => typeof value === "string" && HEX.test(value);

function channels(hex: string) {
  return [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16));
}
const toLinear = (value: number) => {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};
const toByte = (value: number) => {
  const c = value <= 0.0031308 ? 12.92 * value : 1.055 * value ** (1 / 2.4) - 0.055;
  return Math.max(0, Math.min(255, Math.round(c * 255)));
};

/** WCAG relative luminance and contrast ratio. */
export function luminance(hex: string) {
  const [r, g, b] = channels(hex).map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a: string, b: string) {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

function toOklab(hex: string) {
  const [r, g, b] = channels(hex).map(toLinear);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}
function fromOklab([L, a, b]: number[]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const rgb = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  return `#${rgb.map((value) => toByte(value).toString(16).padStart(2, "0")).join("")}`;
}

/** `share` of `color` over `base`, mixed in OKLab like CSS color-mix(in oklab). */
export function mix(base: string, color: string, share: number) {
  const from = toOklab(base);
  const to = toOklab(color);
  return fromOklab(from.map((value, index) => value + (to[index] - value) * share));
}

/** The two colors a choice stands for; a custom choice without both colors falls back to the default. */
export function resolve(choice: ColorChoice): { up: string; down: string } {
  if (choice.preset === "custom") {
    return isHex(choice.up) && isHex(choice.down) ? { up: choice.up.toLowerCase(), down: choice.down.toLowerCase() } : PRESETS.us;
  }
  return PRESETS[choice.preset] ?? PRESETS.us;
}

export function paletteOf(choice: ColorChoice): Palette {
  const { up, down } = resolve(choice);
  return {
    up, upSoft: mix(SURFACES.card, up, SOFT), upFlash: mix(SURFACES.card, up, FLASH),
    down, downSoft: mix(SURFACES.card, down, SOFT), downFlash: mix(SURFACES.card, down, FLASH),
  };
}

/** The lowest contrast of a text color on the backgrounds it is drawn on (card, page, its own soft tint). */
export function weakestContrast(color: string) {
  return Math.min(contrast(color, SURFACES.card), contrast(color, SURFACES.page), contrast(color, mix(SURFACES.card, color, SOFT)));
}

/** Label color (white or near-black) that reads best on a filled tile. */
export function labelOn(fill: string) {
  return contrast(CHART.lightLabel, fill) >= contrast(CHART.darkLabel, fill) ? CHART.lightLabel : CHART.darkLabel;
}

// ---- Current choice: cached in this browser, confirmed by the server. ----

const STORAGE_KEY = "iirp.colors";

function cachedChoice(): ColorChoice {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as ColorChoice | null;
    if (saved && PRESET_ORDER.includes(saved.preset)) return saved;
  } catch {
    /* Storage can be blocked or hold an old value; the default applies. */
  }
  return DEFAULT_CHOICE;
}

let current: ColorChoice = DEFAULT_CHOICE;
let palette: Palette = paletteOf(DEFAULT_CHOICE);
const listeners = new Set<() => void>();

const VARIABLES: [keyof Palette, string][] = [
  ["up", "--up"], ["upSoft", "--up-soft"], ["upFlash", "--up-flash"],
  ["down", "--down"], ["downSoft", "--down-soft"], ["downFlash", "--down-flash"],
];

function apply() {
  if (typeof document === "undefined") return;
  const style = document.documentElement.style;
  for (const [key, name] of VARIABLES) style.setProperty(name, palette[key]);
}

/** Use a choice now (tokens and charts), and remember it in this browser. */
export function setColorChoice(choice: ColorChoice) {
  const same = choice.preset === current.preset && choice.up === current.up && choice.down === current.down;
  current = { preset: choice.preset, up: choice.up ?? null, down: choice.down ?? null };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* Not cached; the server's value still applies on the next load. */
  }
  if (same) return;
  palette = paletteOf(current);
  apply();
  listeners.forEach((listener) => listener());
}

/** Called once before the first render so the cached colors paint first. */
export function initColors() {
  current = cachedChoice();
  palette = paletteOf(current);
  apply();
}

export const colorChoice = () => current;

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The current palette; components that draw with it re-render when it changes. */
export function usePalette() {
  return useSyncExternalStore(subscribe, () => palette, () => palette);
}
