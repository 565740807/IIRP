import patterns from "./generated/legacy-messages.json";
import type { Message } from "./i18n";

// Older database text needs recognition only when an API response contains it.
const templates = patterns.map(({ code, source, names }) => ({ code, pattern: new RegExp(source), names }));
const cache = new Map<string, Message | null>();

export function recognizeLegacyMessage(text: string): Message | null {
  if (cache.has(text)) return cache.get(text)!;
  let found: Message | null = null;
  for (const template of templates) {
    const match = template.pattern.exec(text);
    if (!match) continue;
    found = { code: template.code, params: Object.fromEntries(template.names.map((name, index) => [name, match[index + 1]])) };
    break;
  }
  cache.set(text, found);
  return found;
}
