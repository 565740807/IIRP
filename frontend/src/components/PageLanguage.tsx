import type { ReactNode } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";
import i18n from "@/i18n";

/** Official namespaces suspend inside the existing route skeleton until ready. */
export function PageLanguage({ namespaces, children }: { namespaces: string[]; children: ReactNode }) {
  useTranslation(namespaces);
  return <I18nextProvider i18n={i18n} defaultNS={namespaces}>{children}</I18nextProvider>;
}
