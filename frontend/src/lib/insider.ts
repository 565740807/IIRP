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

type Row = Pick<components["schemas"]["InsiderTransaction"], "table" | "code" | "kind">;

/** "Gift (G)", "Open-market sale (S)": the SEC code in plain words plus the code. */
export function actionLabel(row: Row, t: (key: string, options?: Record<string, unknown>) => string) {
  const code = (row.code ?? "").toUpperCase();
  const name = t(codeKey(row), { defaultValue: tm(row.kind) });
  const text = /^[A-Z]$/.test(code) ? t("ui.code.with_code", { action: name, code }) : name;
  return row.table === "II" ? t("ui.feed.derivative", { action: text }) : text;
}

/** Direction of a row on the page: purchases blue, sales orange, the rest neutral. */
export function sideClass(row: Pick<TradeSummary, "table" | "code">) {
  const direction = side(row);
  return direction === "buy" ? "text-up" : direction === "sell" ? "text-down" : "";
}

const PLAIN = new Set(["P", "S", "A", "F", "G", "M", "X", "O", "C", "D", "J", "W"]);

/** Translation key of the plain-language sentence for a row (code and A/D direction). */
export function plainKey(row: Pick<components["schemas"]["InsiderTransaction"], "code" | "direction" | "shares">) {
  const code = (row.code ?? "").toUpperCase();
  const direction = row.direction === "D" ? "D" : "A";
  if (!code) return "ui.plain.holding";
  const group = ["X", "O"].includes(code) ? "M" : code;
  return PLAIN.has(group) ? `ui.plain.${group}.${direction}` : `ui.plain.other.${direction}`;
}
