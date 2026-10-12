/**
 * One display choice kept in the page link (a tab, "focus / all"): read with a
 * fallback for unknown values, written only when it differs from the default
 * so the plain link stays short.
 */
export function choiceFrom<T extends string>(params: URLSearchParams, name: string, options: readonly T[], fallback: T): T {
  const value = params.get(name);
  return value != null && (options as readonly string[]).includes(value) ? (value as T) : fallback;
}

export function withChoice(params: URLSearchParams, name: string, value: string, fallback: string) {
  const next = new URLSearchParams(params);
  if (value === fallback) next.delete(name);
  else next.set(name, value);
  return next;
}
