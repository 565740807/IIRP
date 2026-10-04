import type { ReactNode } from "react";
import { Button, ErrorNotice, Loading } from "./ui";

/** Read failures never hide an already readable, explicitly identified result. */
export function ResearchReadStatus({ error, retry, pending, label, taskError, retryTask }: {
  error: Error | null;
  retry: () => unknown;
  pending: boolean;
  label: string;
  taskError: Error | null;
  retryTask: () => unknown;
}) {
  return <>
    <ErrorNotice error={error} retry={retry} />
    <ErrorNotice error={taskError} retry={retryTask} />
    {pending && <Loading label={label} />}
  </>;
}

/** Version actions are shared; each page retains its own conditions and coverage. */
export function ResearchVersionActions({ busy, disabled, refreshLabel, refresh, freeze,
  freezeLabel = "固定当前快照", children }: {
  busy: boolean;
  disabled?: boolean;
  refreshLabel: string;
  refresh: () => void;
  freeze?: () => void;
  freezeLabel?: string;
  children?: ReactNode;
}) {
  return <div className="button-row">
    <Button busy={busy} disabled={disabled} onClick={refresh}>{refreshLabel}</Button>
    {freeze && <Button variant="ghost" onClick={freeze}>{freezeLabel}</Button>}
    {children}
  </div>;
}
