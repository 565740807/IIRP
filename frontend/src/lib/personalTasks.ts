import { useQuery } from "@tanstack/react-query";
import { client, unwrap } from "./api-client";
import { usePageVisible } from "./refresh";

export const ACTIVE_TASKS = ["QUEUED", "RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "RETRY_WAIT", "WAITING"];

export function useMyBatches(category: string, enabled = true) {
  const visible = usePageVisible();
  return useQuery({
    queryKey: ["my-tasks", category],
    queryFn: () => unwrap(client.GET("/api/v1/batches", { params: { query: { category, view: "personal" } } })),
    enabled,
    refetchInterval: (query) => !visible ? false : query.state.data?.items.some((batch) => ACTIVE_TASKS.includes(batch.status)) ? 2000 : 15000,
  });
}
