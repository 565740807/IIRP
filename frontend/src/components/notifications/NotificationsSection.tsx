import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Check, Copy, LoaderCircle, RefreshCw, Send } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap } from "@/lib/api-client";
import { formatEt, formatNumber } from "@/lib/format";
import { NOTIFICATION_SETTINGS_KEY, chooseDesktop, desktopChosen, useNotificationSettings, useSaveNotificationSettings, type NotificationSettings, type SettingsChange } from "@/lib/notifications";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Explain } from "@/components/ui/info";
import { ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";

const inputStyle = "h-7 rounded-md border bg-card px-2 text-xs tabular-nums outline-none focus:border-ring";

function Switch({ checked, onChange, label, disabled, name }: { checked: boolean; onChange: (value: boolean) => void; label: string; disabled?: boolean; name: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} data-switch={name}
      onClick={() => onChange(!checked)}
      className={cn("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50",
        checked ? "border-primary bg-primary" : "bg-muted")}>
      <span className={cn("inline-block size-3.5 rounded-full bg-card shadow transition-transform", checked ? "translate-x-4.5" : "translate-x-0.5")} />
    </button>
  );
}

function Card({ title, toggle, note, children, name }: { title: string; toggle?: ReactNode; note?: ReactNode; children?: ReactNode; name: string }) {
  return (
    <div className="space-y-2 rounded-md border px-3 py-2.5" data-notification-card={name}>
      <div className="flex items-center gap-2">
        <h4 className="text-sm font-medium">{title}</h4>
        <span className="ml-auto">{toggle}</span>
      </div>
      {note && <div className="text-xs text-muted-foreground">{note}</div>}
      {children}
    </div>
  );
}

/** A number field saved when it loses focus or Enter is pressed; out-of-range input returns to the saved value. */
function NumberField({ value, onSave, label, min, max, width = "w-20" }: { value: number; onSave: (value: number) => void; label: string; min: number; max: number; width?: string }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const save = () => {
    const number = Number(draft.replace(/[,\s]/g, ""));
    if (Number.isInteger(number) && number >= min && number <= max) {
      if (number !== value) onSave(number);
    } else setDraft(String(value));
  };
  return <input aria-label={label} inputMode="numeric" value={draft} className={cn(inputStyle, width, "text-right")}
    onChange={(event) => setDraft(event.target.value)} onBlur={save} onKeyDown={(event) => event.key === "Enter" && save()} />;
}

function Rules({ settings, save, disabled }: { settings: NotificationSettings; save: (change: SettingsChange) => void; disabled: boolean }) {
  const { t } = useTranslation();
  const rules = settings.rules;
  const set = (change: Partial<NotificationSettings["rules"]>) => save({ rules: change });
  return (
    <Card name="rules" title={t("ui.settings.notifications.rules_title")}
      toggle={<Switch name="rules" checked={rules.enabled} disabled={disabled} label={t("ui.settings.notifications.rules_title")} onChange={(enabled) => set({ enabled })} />}
      note={t("ui.settings.notifications.rules_note")}>
      <div className={cn("space-y-1.5 text-xs", (!rules.enabled || disabled) && "opacity-60")}>
        <p className="flex flex-wrap items-center gap-1.5">
          {t("ui.settings.notifications.executive_before")}
          <NumberField label={t("ui.settings.notifications.executive_label")} value={rules.executive_min} min={1000} max={100_000_000} width="w-24"
            onSave={(executive_min) => set({ executive_min })} />
          {t("ui.settings.notifications.executive_after")}
        </p>
        <p className="flex flex-wrap items-center gap-1.5">
          {t("ui.settings.notifications.cluster_before")}
          <NumberField label={t("ui.settings.notifications.cluster_days_label")} value={rules.cluster_days} min={1} max={30} width="w-12"
            onSave={(cluster_days) => set({ cluster_days })} />
          {t("ui.settings.notifications.cluster_middle")}
          <NumberField label={t("ui.settings.notifications.cluster_people_label")} value={rules.cluster_people} min={2} max={20} width="w-12"
            onSave={(cluster_people) => set({ cluster_people })} />
          {t("ui.settings.notifications.cluster_after")}
        </p>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-muted-foreground">{t("ui.settings.notifications.scope")}</span>
          {(["index_watchlist", "all"] as const).map((scope) => (
            <label key={scope} className="inline-flex items-center gap-1">
              <input type="radio" name="notification-scope" className="accent-primary" checked={rules.scope === scope} onChange={() => set({ scope })} />
              {t(`ui.settings.notifications.scope_${scope}`)}
            </label>
          ))}
          <label className="inline-flex items-center gap-1">
            <input type="checkbox" className="accent-primary" checked={rules.exclude_plans} onChange={(event) => set({ exclude_plans: event.target.checked })} />
            {t("ui.settings.notifications.exclude_plans")}
          </label>
        </p>
      </div>
    </Card>
  );
}

function Desktop() {
  const { t } = useTranslation();
  const supported = typeof Notification !== "undefined";
  const [on, setOn] = useState(() => desktopChosen() && supported && Notification.permission === "granted");
  const [permission, setPermission] = useState(supported ? Notification.permission : "denied");
  const change = async (value: boolean) => {
    if (value && supported && Notification.permission !== "granted") {
      const answer = await Notification.requestPermission();
      setPermission(answer);
      if (answer !== "granted") return;
    }
    chooseDesktop(value);
    setOn(value);
  };
  return (
    <Card name="desktop" title={t("ui.settings.notifications.desktop_title")}
      toggle={<Switch name="desktop" checked={on} disabled={!supported} label={t("ui.settings.notifications.desktop_title")} onChange={(value) => void change(value)} />}
      note={<>
        {t("ui.settings.notifications.desktop_note")}
        {permission === "denied" && <span className="mt-1 block text-warn" role="status">{t(supported ? "ui.settings.notifications.desktop_denied" : "ui.settings.notifications.desktop_unsupported")}</span>}
      </>} />
  );
}

function Phone({ settings, save, disabled }: { settings: NotificationSettings; save: (change: SettingsChange) => void; disabled: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const ntfy = settings.ntfy;
  const push = settings.push;
  const [server, setServer] = useState(ntfy.server);
  useEffect(() => setServer(ntfy.server), [ntfy.server]);
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const regenerate = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/notifications/settings/topic")),
    onSuccess: (data) => { queryClient.setQueryData(NOTIFICATION_SETTINGS_KEY, data); setConfirming(false); },
  });
  const test = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/notifications/test")),
    onSuccess: (data) => queryClient.setQueryData(NOTIFICATION_SETTINGS_KEY, data),
  });
  const copy = async () => {
    if (!ntfy.topic) return;
    try {
      await navigator.clipboard.writeText(ntfy.topic);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* The topic stays selectable for copying by hand. */
    }
  };
  const status = push.last_status
    ? t(`ui.settings.notifications.last_${push.last_status}`, { time: push.last_at ? formatEt(push.last_at, { zone: false }) : "—", kind: t(`ui.settings.notifications.kind_${push.last_kind}`) })
    : t("ui.settings.notifications.last_none");
  return (
    <Card name="phone" title={t("ui.settings.notifications.phone_title")}
      toggle={<Switch name="phone" checked={ntfy.enabled} disabled={disabled} label={t("ui.settings.notifications.phone_title")} onChange={(enabled) => save({ ntfy: { enabled } })} />}
      note={t("ui.settings.notifications.phone_note", { server: ntfy.server })}>
      {ntfy.enabled && (
        <div className="space-y-2 text-xs">
          <label className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-muted-foreground">{t("ui.settings.notifications.server")}</span>
            <input value={server} onChange={(event) => setServer(event.target.value)} aria-label={t("ui.settings.notifications.server")}
              onBlur={() => server !== ntfy.server && save({ ntfy: { server } })} onKeyDown={(event) => event.key === "Enter" && server !== ntfy.server && save({ ntfy: { server } })}
              className={cn(inputStyle, "min-w-0 flex-1 font-mono")} data-ntfy-server />
          </label>
          <div className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-muted-foreground">{t("ui.settings.notifications.topic")}</span>
            <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-1 font-mono select-all" title={ntfy.topic ?? ""} data-ntfy-topic>{ntfy.topic}</code>
            <Button size="icon-xs" variant="ghost" onClick={() => void copy()} aria-label={t("ui.settings.notifications.copy")} title={t("ui.settings.notifications.copy")}>
              {copied ? <Check className="text-ok" /> : <Copy />}
            </Button>
            {confirming ? (
              <>
                <Button size="xs" onClick={() => regenerate.mutate()} disabled={regenerate.isPending}>{t("ui.settings.notifications.regenerate_yes")}</Button>
                <Button size="xs" variant="ghost" onClick={() => setConfirming(false)}>{t("ui.settings.notifications.regenerate_no")}</Button>
              </>
            ) : (
              <Button size="icon-xs" variant="ghost" onClick={() => setConfirming(true)} aria-label={t("ui.settings.notifications.regenerate")} title={t("ui.settings.notifications.regenerate")}>
                <RefreshCw />
              </Button>
            )}
          </div>
          {confirming && <p className="text-warn">{t("ui.settings.notifications.regenerate_note")}</p>}
          <label className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-muted-foreground">{t("ui.settings.notifications.language")}</span>
            <select className={cn(inputStyle, "w-28")} value={ntfy.language} onChange={(event) => save({ ntfy: { language: event.target.value as "en" | "zh" } })}>
              <option value="en">{t("ui.settings.notifications.lang_en")}</option>
              <option value="zh">{t("ui.settings.notifications.lang_zh")}</option>
            </select>
          </label>
          <Explain summary={t("ui.settings.notifications.subscribe_title")}>
            <ol className="mt-1 list-decimal space-y-0.5 pl-4">
              <li>{t("ui.settings.notifications.subscribe_1")}</li>
              <li>{t("ui.settings.notifications.subscribe_2", { server: ntfy.server })}</li>
              <li>{t("ui.settings.notifications.subscribe_3")}</li>
            </ol>
            <p className="mt-1">{t("ui.settings.notifications.privacy")}</p>
          </Explain>
          <div className="flex flex-wrap items-center gap-2 border-t pt-2">
            <Button size="xs" variant="outline" onClick={() => test.mutate()} disabled={test.isPending || disabled} data-send-test>
              {test.isPending ? <LoaderCircle className="motion-safe:animate-spin" /> : <Send />}{t("ui.settings.notifications.test")}
            </Button>
            <span className="text-muted-foreground" role="status" data-push-status>
              {push.waiting > 0 || (test.isSuccess && !push.last_status) ? t("ui.settings.notifications.waiting", { count: push.waiting }) : status}
              {" · "}{t("ui.settings.notifications.today", { count: push.sent_today, limit: formatNumber(push.daily_limit, 0) })}
            </span>
          </div>
          {push.last_status === "failed" && push.last_error && <p className="text-destructive" role="alert">{tm(push.last_error)}</p>}
          {(test.error || regenerate.error) && <p className="text-destructive" role="alert">{(test.error ?? regenerate.error)!.message}</p>}
        </div>
      )}
    </Card>
  );
}

/**
 * Settings → Notifications: the main switch (counted from the moment it is turned on),
 * the rule reminders, desktop notifications for this browser, and phone pushes through ntfy.
 */
export function NotificationsSection() {
  const { t } = useTranslation();
  const settings = useNotificationSettings();
  const save = useSaveNotificationSettings();
  if (settings.error) return <ErrorState error={settings.error} retry={() => void settings.refetch()} compact />;
  const data = settings.data;
  if (!data) return <div className="space-y-2">{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-14 w-full" />)}</div>;
  const off = !data.enabled;
  return (
    <section className="space-y-2.5" data-settings-notifications>
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium">{t("ui.settings.notifications.title")}</h3>
        {data.enabled && data.enabled_at && <span className="text-xs text-muted-foreground">{t("ui.settings.notifications.since", { time: formatEt(data.enabled_at, { zone: false }) })}</span>}
        <span className="ml-auto inline-flex items-center gap-2 text-xs">
          {save.isPending && <LoaderCircle className="size-3.5 text-muted-foreground motion-safe:animate-spin" />}
          <Switch name="main" checked={data.enabled} label={t("ui.settings.notifications.title")} onChange={(enabled) => save.mutate({ enabled })} />
        </span>
      </div>
      <Explain summary={t("ui.settings.notifications.rule")}>
        <p className="mt-1">{t("ui.settings.notifications.rule_more")}</p>
      </Explain>
      {save.error && <p role="alert" className="text-xs text-destructive">{save.error.message}</p>}
      <div className={cn("space-y-2", off && "opacity-60")} aria-disabled={off}>
        <Rules settings={data} save={(change) => save.mutate(change)} disabled={off} />
        <Desktop />
        <Phone settings={data} save={(change) => save.mutate(change)} disabled={off} />
      </div>
    </section>
  );
}
