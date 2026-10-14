// Generated from the authored translation dictionaries by generate-locales.mjs.
export const messageNamespaces: Record<string, string> = {"events":"events","diagnostics":"data","probe":"data","maintenance":"data","storage":"data","health":"settings","system":"settings","long_prices":"settings"};
export const localeImports: Record<string, Record<string, () => Promise<{ default: Record<string, string> }>>> = {
  en: {
    translation: () => import("./locales/en/translation.json"),
    analysis: () => import("./locales/en/analysis.json"),
    events: () => import("./locales/en/events.json"),
    sectors: () => import("./locales/en/sectors.json"),
    insider: () => import("./locales/en/insider.json"),
    data: () => import("./locales/en/data.json"),
    settings: () => import("./locales/en/settings.json"),
  },
  zh: {
    translation: () => import("./locales/zh/translation.json"),
    analysis: () => import("./locales/zh/analysis.json"),
    events: () => import("./locales/zh/events.json"),
    sectors: () => import("./locales/zh/sectors.json"),
    insider: () => import("./locales/zh/insider.json"),
    data: () => import("./locales/zh/data.json"),
    settings: () => import("./locales/zh/settings.json"),
  },
};
