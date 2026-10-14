/// <reference types="vite/client" />
/** Optional translation resources are loaded by i18next with the current language. */
export const featureLocales = {
  ...import.meta.glob<{ default: Record<string, string> }>("./features/*/locales/{en,zh}.json"),
  ...import.meta.glob<{ default: Record<string, string> }>("./features/*/generated/entry-*.json"),
};
