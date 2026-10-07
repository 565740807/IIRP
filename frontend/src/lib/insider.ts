import type { components } from "../generated/api";
import { tm } from "../i18n";

type TradeSummary = components["schemas"]["TradeSummary"];
type InsiderOwner = components["schemas"]["InsiderOwner"];

/** Buy or sell side of a row: only open-market/private purchases (P) and sales (S). */
export function side(summary: Pick<TradeSummary, "table" | "code">): "buy" | "sell" | null {
  if (summary.table !== "I") return null;
  return summary.code === "P" ? "buy" : summary.code === "S" ? "sell" : null;
}

/** The action a line leads with: a purchase or sale first, otherwise the largest row group. */
export function primarySummary(summaries: readonly TradeSummary[]): TradeSummary | undefined {
  return (
    summaries.find((summary) => side(summary) === "buy") ??
    summaries.find((summary) => side(summary) === "sell") ??
    [...summaries].sort((a, b) => b.rows - a.rows)[0]
  );
}

/** Translation key of an SEC transaction code (Form 4 General Instructions 8). */
export function codeKey(summary: Pick<TradeSummary, "table" | "code">) {
  const code = (summary.code ?? "").toUpperCase();
  return /^[A-Z]$/.test(code) ? `ui.code.${code}` : "ui.code.other";
}

const ACRONYMS = new Set([
  "CEO", "CFO", "COO", "CTO", "CIO", "CAO", "CLO", "CMO", "CHRO", "CRO", "CSO", "CCO",
  "EVP", "SVP", "VP", "GC", "LLC", "LP", "LLP", "II", "III", "IV", "USA", "US", "NA", "PLC",
]);

/**
 * SEC names and officer titles often arrive in capitals ("PRESIDENT AND CEO").
 * Only an all-capital text is recased; acronyms and mixed-case text stay.
 */
export function readableCase(text: string) {
  if (!text || text !== text.toUpperCase() || !/[A-Z]{2}/.test(text)) return text;
  return text
    .toLowerCase()
    .replace(/[a-z][a-z.'&-]*/g, (word, offset: number) => {
      const upper = word.toUpperCase().replace(/[.,]$/, "");
      if (ACRONYMS.has(upper)) return word.toUpperCase();
      if (offset > 0 && ["and", "of", "the", "for", "&"].includes(word)) return word;
      return word.charAt(0).toUpperCase() + word.slice(1);
    });
}

/** "Director, President and CEO" from the roles stated in the filing. */
export function rolesText(owner: Pick<InsiderOwner, "roles">) {
  return owner.roles.map((role) => readableCase(tm(role))).join(" · ");
}
