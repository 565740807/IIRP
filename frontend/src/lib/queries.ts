import { queryOptions } from "@tanstack/react-query";
import { client, unwrap } from "@/lib/api-client";

/** Saved home quotes plus the browser refresh intervals (local read only). */
export const homeQuery = queryOptions({
  queryKey: ["home"],
  queryFn: () => unwrap(client.GET("/api/v1/home")),
});
