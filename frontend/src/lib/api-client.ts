import createClient from "openapi-fetch";
import i18n from "@/i18n";
import { decodeMessage, tm } from "@/i18n";
import type { components, paths } from "@/generated/api";

/**
 * Typed requests generated from docs/openapi.json. Message codes in response
 * fields are translated where they are shown (tm), so a language switch never
 * needs a refetch. Writes carry the same-origin client header.
 */
export const client = createClient<paths>({ baseUrl: "" });
client.use({
  onRequest({ request }) {
    if (request.method !== "GET") request.headers.set("X-IIRP-Client", "web");
    return request;
  },
});

export type Schemas = components["schemas"];

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function errorText(detail: unknown, status: number) {
  if (typeof detail === "string" || decodeMessage(detail)) return tm(detail);
  if (Array.isArray(detail))
    return detail.map((item) => (item?.msg ? tm(item.msg) : i18n.t("ui.error.invalid_input"))).join(i18n.t("format.item_separator"));
  return i18n.t("ui.error.request_failed", { status });
}

/** Resolve an openapi-fetch call to its data, or throw a readable ApiError. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(i18n.t("ui.error.offline"), 0, error);
  }
  if (result.error !== undefined || !result.response.ok) {
    const detail = (result.error as { detail?: unknown } | undefined)?.detail;
    throw new ApiError(errorText(detail, result.response.status), result.response.status, detail);
  }
  return result.data as T;
}
