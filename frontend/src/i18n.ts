import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import resourcesToBackend from "i18next-resources-to-backend";
import { localeImports, messageNamespaces } from "./generated/locale-imports";

/**
 * Backend text arrives as message codes plus parameters (backend/iirp/messages.py):
 * either the object {code, params} or its compact JSON inside a former text field.
 * Text saved before messages existed is shown unchanged.
 */
export type Message = { code: string; params?: Record<string, unknown> };

export const LANGUAGES = ["en", "zh"] as const;
export type Language = (typeof LANGUAGES)[number];
const STORAGE_KEY = "iirp.language";

/** English on first open; the reader's choice is kept in this browser. */
function savedLanguage(): Language {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "en" || value === "zh") return value;
  } catch {
    /* Storage can be blocked; English is the default. */
  }
  return "en";
}

export type LocaleImports = Record<string, () => Promise<{ default: Record<string, string> }>>;

/** i18next owns resource caching, initial loading and language switches. */
export function initialize(featureLocales: LocaleImports = {}) {
  const featureNames = [...new Set(Object.keys(featureLocales).flatMap(path =>
    /\/features\/([^/]+)\//.exec(path)?.[1] ?? []))];
  const ready = i18n.use(resourcesToBackend(async (language: string, namespace: string) => {
    const loadCore = localeImports[language]?.[namespace];
    const [core, ...additions] = await Promise.all([loadCore ? loadCore() : Promise.resolve({ default: {} }), ...Object.entries(featureLocales)
      .filter(([path]) => namespace === "translation"
        ? path.endsWith(`/generated/entry-${language}.json`)
        : path.endsWith(`/features/${namespace}/locales/${language}.json`))
      .map(([, load]) => load())]);
    return Object.assign({}, core.default, ...additions.map(module => module.default));
  })).use(initReactI18next).init({
    lng: savedLanguage(),
    // Both supported dictionaries have the same keys; no second language is fetched.
    fallbackLng: false,
    ns: ["translation"],
    defaultNS: "translation",
    fallbackNS: ["translation", ...Object.keys(localeImports.en).filter(name => name !== "translation"), ...featureNames],
    supportedLngs: [...LANGUAGES],
    load: "languageOnly",
    // Codes are flat keys such as "market.bar.jump".
    keySeparator: false,
    nsSeparator: false,
    interpolation: { escapeValue: false },
  });
  // Array parameters: "list" joins names (、), "items" joins whole statements (；).
  for (const [name, separator] of [
    ["list", "format.list_separator"],
    ["items", "format.item_separator"],
  ] as const) {
    i18n.services.formatter?.add(name, (value: unknown) =>
      Array.isArray(value) ? value.join(i18n.t(separator)) : String(value ?? ""),
    );
  }

  // "{{time, et}}": an instant in US Eastern time; "{{size, bytes}}": a byte count;
  // "{{kind, jobkind}}": a job kind in words.
  i18n.services.formatter?.add("et", (value: unknown, language: string | undefined) => {
    const instant = new Date(String(value ?? ""));
    if (Number.isNaN(instant.getTime())) return String(value ?? "");
    const text = new Intl.DateTimeFormat(language === "zh" ? "zh-CN" : "en-US", {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: language !== "zh", timeZone: "America/New_York",
    }).format(instant);
    return `${text} ${i18n.t("ui.time.et")}`;
  });
  i18n.services.formatter?.add("bytes", (value: unknown, language: string | undefined) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes)) return String(value ?? "");
    const units = ["B", "KB", "MB", "GB", "TB"];
    const power = Math.min(units.length - 1, Math.max(0, Math.floor(Math.log(Math.max(bytes, 1)) / Math.log(1024))));
    return `${new Intl.NumberFormat(language === "zh" ? "zh-CN" : "en-US", { maximumFractionDigits: 1 }).format(bytes / 1024 ** power)} ${units[power]}`;
  });
  // "{{purpose, purpose}}": a batch purpose code ("insider_window"); legacy Chinese text passes to tm.
  i18n.services.formatter?.add("purpose", (value: unknown) => {
    const text = String(value ?? "");
    return /^[a-z_]+$/.test(text) ? i18n.t(`ui.purpose.${text}`, { defaultValue: text }) : text;
  });
  i18n.services.formatter?.add("jobkind", (value: unknown) => i18n.t(`ui.job_kind.${String(value)}`, { defaultValue: String(value) }));

  void ready.then(() => applyDocumentLanguage(i18n.language));
  return ready;
}
i18n.on("languageChanged", applyDocumentLanguage);

function applyDocumentLanguage(language: string) {
  if (typeof document !== "undefined")
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
}


export function setLanguage(language: Language) {
  try {
    localStorage.setItem(STORAGE_KEY, language);
  } catch {
    /* The switch still applies to this tab. */
  }
  return i18n.changeLanguage(language);
}

/** Intl locale for numbers and dates in the current language. */
export function locale() {
  return i18n.language === "zh" ? "zh-CN" : "en-US";
}

function isMessage(value: unknown): value is Message {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as Message).code === "string"
  );
}

export function decodeMessage(value: unknown): Message | null {
  if (isMessage(value)) return value;
  if (typeof value !== "string" || !value.startsWith('{"code":')) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return isMessage(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function param(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(param);
  if (decodeMessage(value)) return tm(value);
  // A legacy Chinese fragment inside a parameter ("自动日线更新").
  return typeof value === "string" && i18n.language !== "zh" && legacyMessage(value) ? tm(value) : value;
}

/**
 * Rows written by older versions, before message codes, hold Chinese sentences.
 * Match them against the Chinese templates once and show them in the reader's
 * language; text that matches no template stays as it is.
 */
let legacyMatcher: ((text: string) => Message | null) | undefined;
const CJK = /[\u3400-\u9fff]/;

function containsChinese(value: unknown): boolean {
  if (typeof value === "string") return CJK.test(value);
  if (Array.isArray(value)) return value.some(containsChinese);
  return !!value && typeof value === "object" && Object.values(value).some(containsChinese);
}

function responseNamespaces(value: unknown, namespaces = new Set<string>()) {
  const message = decodeMessage(value) ?? (typeof value === "string" ? legacyMatcher?.(value) : null);
  const namespace = message ? messageNamespaces[message.code.split(".")[0]] : undefined;
  if (namespace) namespaces.add(namespace);
  if (Array.isArray(value)) value.forEach(child => responseNamespaces(child, namespaces));
  else if (value && typeof value === "object")
    Object.values(value).forEach(child => responseNamespaces(child, namespaces));
  return namespaces;
}

/** Load message namespaces and old-text recognition before publishing API data. */
export async function prepareMessages(value: unknown) {
  if (!legacyMatcher && containsChinese(value))
    legacyMatcher = (await import("./legacy-messages")).recognizeLegacyMessage;
  const namespaces = [...responseNamespaces(value)];
  if (namespaces.length) await i18n.loadNamespaces(namespaces);
}

function legacyMessage(text: string): Message | null {
  return CJK.test(text) ? legacyMatcher?.(text) ?? null : null;
}

/** Translate one backend message; plain text passes through. */
export function tm(value: unknown): string {
  if (value === null || value === undefined) return "";
  let message = decodeMessage(value);
  if (!message && typeof value === "string" && i18n.language !== "zh") message = legacyMessage(value);
  if (!message) return String(value);
  const params = Object.fromEntries(
    Object.entries(message.params ?? {}).map(([key, child]) => [key, param(child)]),
  );
  return i18n.t(message.code, params);
}

/**
 * Replace every message in an API response with its text in the current
 * language. Components then show response fields as they are; a later
 * language switch refetches the queries.
 */
export function localize<T>(value: T): T {
  if (typeof value === "string") return (decodeMessage(value) ? tm(value) : value) as T;
  if (Array.isArray(value)) return value.map(localize) as T;
  // Only strings carry messages in responses; objects with a "code" field are
  // data (an SEC transaction code, for example).
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [key, localize(child)]),
    ) as T;
  }
  return value;
}

export default i18n;
