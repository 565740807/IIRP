/**
 * Which part of a long page is being read: the last section whose top has
 * passed the reading line (a little under the sticky header and contents bar),
 * or the last section once the page is scrolled to its end.
 */
export type SectionTop = { id: string; top: number };

export function activeSection(sections: readonly SectionTop[], line: number, atEnd = false): string | null {
  if (!sections.length) return null;
  if (atEnd) return sections[sections.length - 1].id;
  let current = sections[0].id;
  for (const section of sections) {
    if (section.top <= line) current = section.id;
    else break;
  }
  return current;
}
