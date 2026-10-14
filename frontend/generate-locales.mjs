import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const namespaces = ["translation", "analysis", "events", "sectors", "insider", "data", "settings"];
const messageNamespaces = { events: "events", diagnostics: "data", probe: "data", maintenance: "data", storage: "data", health: "settings", system: "settings", long_prices: "settings" };
const entrySector = /^(ui\.sectors\.(?:name\.|short\.|group\.|strip_|closed_basis|intraday|intraday_rule|waiting))/;
// The home page loads no page namespace: its index filter and the trade and current
// price cells it shares with the Insider pages read these keys from the entry.
const entryOverview = /^ui\.overview\.(?:index|estimated$|(?:price_check|quote_status|relation)\.|(?:day_range|weighted_range|average_method|estimate_method|reported_price)$|foreign_|comparison_)/;
function namespace(key) {
  const messages = messageNamespaces[key.split(".")[0]];
  if (messages) return messages;
  if (entrySector.test(key) || key.startsWith("ui.window.") || entryOverview.test(key) || key === "ui.settings.open") return "translation";
  if (key.startsWith("ui.analysis.")) return "analysis";
  if (key.startsWith("ui.events.")) return "events";
  if (/^ui\.(sectors|rotation)\./.test(key)) return "sectors";
  // The trade page's plain-language sentence (ui.plain) loads with the Insider pages.
  if (/^ui\.(overview|entity|lookup|detail|amendment|range|market_detail|plain)\./.test(key)) return "insider";
  if (/^ui\.(data|health|index_lists|diagnostics)\./.test(key)) return "data";
  if ((key.startsWith("ui.sec_contact.") && !["ui.sec_contact.add", "ui.sec_contact.banner", "ui.sec_contact.banner_config"].includes(key)) || key.startsWith("ui.watchlist.settings.") || key.startsWith("ui.system.") || /^ui\.(settings|long_prices)\./.test(key)) return "settings";
  return "translation";
}
function write(relative, value) {
  const target = path.join(root, relative);
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2) + "\n";
  if (process.argv.includes("--check")) {
    if (!fs.existsSync(target) || fs.readFileSync(target, "utf8") !== text)
      throw new Error("Run npm run generate:locales");
  } else {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, text);
  }
}
for (const language of ["en", "zh"]) {
  const source = JSON.parse(fs.readFileSync(path.join(root, `src/locales/${language}/translation.json`), "utf8"));
  const resources = Object.fromEntries(namespaces.map(name => [name, {}]));
  for (const [key, value] of Object.entries(source)) resources[namespace(key)][key] = value;
  for (const name of namespaces) write(`src/generated/locales/${language}/${name}.json`, resources[name]);
}
write("src/generated/locale-imports.ts", `// Generated from the authored translation dictionaries by generate-locales.mjs.\nexport const messageNamespaces: Record<string, string> = ${JSON.stringify(messageNamespaces)};\nexport const localeImports: Record<string, Record<string, () => Promise<{ default: Record<string, string> }>>> = {\n${["en", "zh"].map(language => `  ${language}: {\n${namespaces.map(name => `    ${name}: () => import("./locales/${language}/${name}.json"),`).join("\n")}\n  },`).join("\n")}\n};\n`);
const featureRoot = path.join(root, "src/features");
if (fs.existsSync(featureRoot)) for (const directory of fs.readdirSync(featureRoot)) {
  const owned = path.join(featureRoot, directory, "locales");
  const declaration = path.join(owned, "entry-keys.json");
  if (!fs.existsSync(path.join(owned, "en.json")) || !fs.existsSync(path.join(owned, "zh.json"))) continue;
  const prefixes = fs.existsSync(declaration) ? JSON.parse(fs.readFileSync(declaration, "utf8")) : ["ui.nav.", "ui.page."];
  for (const language of ["en", "zh"]) {
    const source = JSON.parse(fs.readFileSync(path.join(owned, `${language}.json`), "utf8"));
    write(`src/features/${directory}/generated/entry-${language}.json`, Object.fromEntries(
      Object.entries(source).filter(([key]) => prefixes.some(prefix => key.startsWith(prefix)))));
  }
}
