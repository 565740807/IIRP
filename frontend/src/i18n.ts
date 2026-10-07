import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh/translation.json";

/**
 * Backend text arrives as message codes plus parameters (backend/iirp/messages.py):
 * either the object {code, params} or its compact JSON inside a former text field.
 * Text saved before messages existed is shown unchanged.
 */
export type Message = { code: string; params?: Record<string, unknown> };

void i18n.use(initReactI18next).init({
  resources: { zh: { translation: zh } },
  lng: "zh",
  fallbackLng: "zh",
  // Resources are bundled, so translations are ready before the first render.
  initAsync: false,
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
  return decodeMessage(value) ? tm(value) : value;
}

/** Translate one backend message; plain text passes through. */
export function tm(value: unknown): string {
  if (value === null || value === undefined) return "";
  const message = decodeMessage(value);
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
