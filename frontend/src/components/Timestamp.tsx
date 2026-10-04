import { useState } from "react";
import { formatTime } from "../api";

// Calendar facts are not instants. Only a timestamp offers local conversion.
export function Timestamp({ value }: { value?: string | null }) {
  const [expanded, setExpanded] = useState(false);
  const primary = formatTime(value);
  if (!value || /^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(new Date(value).getTime()))
    return <>{primary}</>;
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <span className="timestamp">
      <button
        type="button"
        className="timestamp-toggle"
        aria-expanded={expanded}
        aria-label={`${primary}，${expanded ? "收起" : "查看"}本地时间`}
        title={expanded ? "收起本地时间" : "查看本地时间"}
        onClick={() => setExpanded((open) => !open)}
      >
        <time dateTime={value}>{primary}</time>
      </button>
      {expanded && <span className="timestamp-local">本地：{formatTime(value, localZone)}</span>}
    </span>
  );
}
