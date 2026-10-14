import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Bell, BellOff, CheckCheck } from "lucide-react";
import { formatEt } from "@/lib/format";
import { sourceContext } from "@/lib/navigation";
import { describe, desktopChosen, markSeen, notificationHref, seenAt, unseen, useMarkRead, useNotifications, type NotificationItem } from "@/lib/notifications";
import { usePageVisible } from "@/lib/refresh";
import { openSettings } from "@/lib/settings";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";

const POLL_MS = 30_000;

/** New notifications on the desktop while this page is in the background (this browser's choice). */
function useDesktop(items: readonly NotificationItem[] | undefined) {
  const { t } = useTranslation();
  useEffect(() => {
    if (!items) return;
    const { fresh, newest } = unseen(items, seenAt());
    markSeen(newest);
    if (!fresh.length || !desktopChosen() || typeof Notification === "undefined" || Notification.permission !== "granted") return;
    if (document.visibilityState === "visible" && document.hasFocus()) return;
    for (const item of fresh.slice(-5)) {
      const text = describe(item, t);
      // The tag keeps one desktop notification per item however many IIRP tabs are open.
      const shown = new Notification(text.title, { body: text.detail, tag: item.id });
      shown.onclick = () => {
        window.focus();
        window.location.assign(notificationHref(item));
      };
    }
  }, [items, t]);
}

function Row({ item, close }: { item: NotificationItem; close: () => void }) {
  const { t } = useTranslation();
  const location = useLocation();
  const text = describe(item, t);
  const data = item.data as { amendment?: boolean };
  return (
    <li>
      <Link to={notificationHref(item)} state={sourceContext(location)} onClick={close} data-notification={item.kind}
        className={cn("flex gap-2 rounded-md px-2 py-1.5 hover:bg-accent", !item.read && "bg-primary/5")}>
        <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", item.read ? "bg-transparent" : "bg-primary")} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-1.5">
            {item.kind !== "trade" && <Badge variant="secondary" className="h-4 shrink-0 px-1 text-xs font-normal">{t("ui.notifications.rule")}</Badge>}
            <span className="min-w-0 truncate text-sm font-medium" title={text.title}>{text.title}</span>
            {data.amendment && <Badge variant="outline" className="h-4 shrink-0 px-1 text-xs">{t("ui.notable.amended")}</Badge>}
          </span>
          <span className="flex items-baseline gap-2 text-xs text-muted-foreground">
            <span className="min-w-0 truncate" title={text.detail}>{text.detail}</span>
            <span className="ml-auto shrink-0 tabular-nums" title={t("ui.notifications.created_tip")}>{formatEt(item.created_at, { zone: false })}</span>
          </span>
        </span>
      </Link>
    </li>
  );
}

/** The top bar's bell: unread count, and the latest notifications, each opening what it is about. */
export function NotificationBell() {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const [open, setOpen] = useState(false);
  const list = useNotifications(visible || desktopChosen() ? POLL_MS : false);
  const read = useMarkRead();
  const data = list.data;
  useDesktop(data?.items);
  const unread = data?.unread ?? 0;
  // What was on screen when the list closes has been seen.
  const close = () => {
    setOpen(false);
    const ids = data?.items.filter((item) => !item.read).map((item) => item.id) ?? [];
    if (ids.length) read.mutate({ ids });
  };
  return (
    <Popover open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" className="relative" aria-label={t("ui.notifications.bell", { count: unread })} data-notification-bell>
          {data && !data.enabled ? <BellOff className="text-muted-foreground" /> : <Bell />}
          {unread > 0 && (
            <span className="absolute -top-0.5 -right-0.5 min-w-4 rounded-full bg-primary px-1 text-center text-xs leading-4 font-medium text-primary-foreground tabular-nums" data-unread>
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[28rem] p-0" data-notification-list>
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <h2 className="text-sm font-semibold">{t("ui.notifications.title")}</h2>
          {unread > 0 && <span className="text-xs text-muted-foreground tabular-nums">{t("ui.notifications.unread", { count: unread })}</span>}
          <span className="ml-auto flex items-center gap-1">
            {unread > 0 && (
              <Button size="xs" variant="ghost" onClick={() => read.mutate({ all: true })} data-read-all>
                <CheckCheck />{t("ui.notifications.read_all")}
              </Button>
            )}
            <Button size="xs" variant="ghost" onClick={() => { close(); openSettings("notifications"); }}>{t("ui.notifications.settings")}</Button>
          </span>
        </div>
        <div className="max-h-[60vh] overflow-y-auto p-1.5">
          {!data ? (
            list.error ? <p className="px-2 py-3 text-sm text-destructive">{list.error.message}</p>
              : <div className="space-y-1.5 p-1">{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-9 w-full" />)}</div>
          ) : !data.enabled && !data.items.length ? (
            <div className="space-y-2 px-3 py-4 text-center text-sm" data-notifications-off>
              <p className="text-muted-foreground">{t("ui.notifications.off")}</p>
              <Button size="sm" variant="outline" onClick={() => { close(); openSettings("notifications"); }}>{t("ui.notifications.turn_on")}</Button>
            </div>
          ) : !data.items.length ? (
            <p className="px-3 py-4 text-center text-sm text-muted-foreground">{t("ui.notifications.empty")}</p>
          ) : (
            <ul className="space-y-0.5">{data.items.map((item) => <Row key={item.id} item={item} close={close} />)}</ul>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
