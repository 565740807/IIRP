import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, unwrap } from "./api-client";
import { setColorChoice } from "./colors";

export const DISPLAY_KEY = ["display"];

/** The instance's display settings; apply saved colors as soon as they arrive. */
export function useDisplaySettings() {
  const query = useQuery({ queryKey: DISPLAY_KEY, queryFn: () => unwrap(client.GET("/api/v1/display")), staleTime: 60_000 });
  const colors = query.data?.colors;
  useEffect(() => { if (colors) setColorChoice(colors); }, [colors]);
  return query;
}
