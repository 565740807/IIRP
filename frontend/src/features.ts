/// <reference types="vite/client" />
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import i18n from "@/i18n";

/**
 * Optional features: each directory under features/ with an index.tsx adds its
 * routes, a sidebar entry and its own translations (locales/<language>.json).
 * A checkout without such a directory simply has no such page.
 */
export type Feature = {
  /** Sidebar and page title: ui.nav.<key> and ui.page.<key>. */
  key: string;
  /** The sidebar link and the path prefix of the feature's pages. */
  path: string;
  icon: LucideIcon;
  /** Place in the sidebar: home 10, insiders 20, sectors 30, analysis 40, data 50. */
  order: number;
  routes: { path: string; element: ReactNode }[];
};

const locales = import.meta.glob<{ default: Record<string, string> }>("./features/*/locales/*.json", { eager: true });
for (const [path, module] of Object.entries(locales)) {
  const language = /\/locales\/(\w+)\.json$/.exec(path)?.[1];
  if (language) i18n.addResourceBundle(language, "translation", module.default, true, false);
}

const modules = import.meta.glob<{ feature: Feature }>("./features/*/index.tsx", { eager: true });
export const FEATURES: Feature[] = Object.values(modules).map((module) => module.feature).sort((a, b) => a.order - b.order);
