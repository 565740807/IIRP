import fs from "node:fs";
import { fileURLToPath } from "node:url";

// Recognition patterns for pre-message-code database text, not language resources.
export function legacyPatterns(translation) {
  const cjk = /[\u3400-\u9fff]/;
  return Object.entries(translation)
    .filter(([code, template]) => !code.startsWith("ui.") && cjk.test(template.replace(/\{\{[^}]+\}\}/g, "")))
    .map(([code, template]) => {
      const names = [];
      const source = template.split(/(\{\{[^}]+\}\})/).map(part => {
        const name = /^\{\{\s*([\w.]+)/.exec(part)?.[1];
        if (!name) return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        names.push(name);
        return "(.+?)";
      }).join("");
      return { code: code.replace(/_(one|other)$/, ""), source: `^${source}$`, names };
    }).sort((a, b) => b.source.length - a.source.length);
}

const translation = JSON.parse(fs.readFileSync(new URL("src/locales/zh/translation.json", import.meta.url), "utf8"));
const output = JSON.stringify(legacyPatterns(translation), null, 2) + "\n";
const target = fileURLToPath(new URL("src/generated/legacy-messages.json", import.meta.url));
if (process.argv.includes("--check")) {
  if (fs.readFileSync(target, "utf8") !== output) throw new Error("Run npm run generate:locales");
} else fs.writeFileSync(target, output);
