import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Check, LoaderCircle, RotateCcw, X } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { NOTABLE_DEPENDENT, NOTABLE_KEY, personHref, personName, useFollowToggle, useNotablePeople, type NotablePerson } from "@/lib/notable";
import { Button } from "@/components/ui/button";
import { Explain } from "@/components/ui/info";
import { ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";

const ORDER = ["investor", "investment_firm", "founder_ceo", "house", "president", "cabinet", "sec", "oge"];

function detail(person: NotablePerson) {
  if (person.source === "sec") return person.cik ? `CIK ${Number(person.cik)}` : "";
  return person.district ?? person.position ?? "";
}

/**
 * Settings → Followed people: everyone followed, grouped like the default list,
 * with a remove button each, and a way to add back the default people who were
 * removed. People are added with ☆ on their pages, the timeline and search.
 */
export function NotableSection({ close }: { close?: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const list = useNotablePeople();
  const toggle = useFollowToggle();
  const [confirming, setConfirming] = useState(false);
  const restore = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/notable/people/restore")),
    onSuccess: (data) => {
      queryClient.setQueryData(NOTABLE_KEY, data);
      void queryClient.invalidateQueries({ predicate: (query) => query.queryKey.indexOf(NOTABLE_DEPENDENT) > 0 });
      setConfirming(false);
    },
  });
  const groups = useMemo(() => {
    const map = new Map<string, NotablePerson[]>();
    for (const person of list.data?.items ?? []) {
      const group = person.category ?? person.source;
      map.set(group, [...(map.get(group) ?? []), person]);
    }
    return [...map.entries()].sort(([a], [b]) => ORDER.indexOf(a) - ORDER.indexOf(b));
  }, [list.data]);
  if (list.error) return <ErrorState error={list.error} retry={() => void list.refetch()} compact />;
  const data = list.data;
  const missingDefaults = data ? data.default_count - data.items.filter((item) => item.default).length : 0;
  return (
    <section className="space-y-2.5" data-settings-notable>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">{t("ui.settings.notable.title")}</h3>
        {data && <span className="text-xs text-muted-foreground tabular-nums">{t("ui.settings.notable.count", { count: data.items.length, limit: data.limit })}</span>}
        <span className="ml-auto flex items-center gap-1.5 text-xs">
          {restore.isSuccess && <span className="inline-flex items-center gap-1 text-muted-foreground" role="status"><Check className="size-3 text-ok" />{t("ui.settings.notable.restored", { count: restore.data.added })}</span>}
          {confirming ? (
            <>
              <span className="text-muted-foreground">{t("ui.settings.notable.restore_confirm", { count: missingDefaults })}</span>
              <Button size="xs" onClick={() => restore.mutate()} disabled={restore.isPending}>
                {restore.isPending ? <LoaderCircle className="motion-safe:animate-spin" /> : <Check />}{t("ui.settings.notable.restore_yes")}
              </Button>
              <Button size="xs" variant="ghost" onClick={() => setConfirming(false)}>{t("ui.settings.notable.restore_no")}</Button>
            </>
          ) : (
            <Button size="xs" variant="outline" disabled={!data || missingDefaults <= 0} onClick={() => setConfirming(true)}
              title={missingDefaults > 0 ? undefined : t("ui.settings.notable.restore_none")} data-restore-defaults>
              <RotateCcw />{t("ui.settings.notable.restore")}
            </Button>
          )}
        </span>
      </div>
      <Explain summary={t("ui.settings.notable.rule")}>
        <p className="mt-1">{t("ui.settings.notable.rule_more")}</p>
      </Explain>
      {restore.error && <p role="alert" className="text-xs text-destructive">{restore.error.message}</p>}
      {!data ? (
        <div className="space-y-1.5">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-6 w-full" />)}</div>
      ) : !data.items.length ? (
        <p className="rounded-md border border-dashed px-3 py-4 text-center text-sm text-muted-foreground">{t("ui.settings.notable.empty")}</p>
      ) : (
        <div className="max-h-[50vh] space-y-2 overflow-y-auto rounded-md border px-2.5 py-2" data-notable-list>
          {groups.map(([group, people]) => (
            <div key={group}>
              <p className="pb-0.5 text-xs font-medium text-muted-foreground">{t(`ui.notable.category.${group}`)} <span className="tabular-nums">{people.length}</span></p>
              <ul>
                {people.map((person) => {
                  const name = personName(person);
                  return (
                    <li key={person.key} className="flex min-w-0 items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-accent/60" data-notable-person={person.key}>
                      <Link to={personHref(person.key)} onClick={close} className="min-w-0 truncate hover:underline" title={name}>{name}</Link>
                      <span className="shrink-0 text-xs text-muted-foreground">{detail(person)}</span>
                      {!person.local && <span className="shrink-0 text-xs text-muted-foreground" title={t(`ui.notable.not_local.${person.source}`)}>{t("ui.settings.notable.no_local")}</span>}
                      <Button type="button" size="icon-xs" variant="ghost" className="ml-auto shrink-0 text-muted-foreground"
                        aria-label={t("ui.settings.notable.remove", { name })} onClick={() => toggle.mutate({ key: person.key, follow: false })}>
                        <X />
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
      {toggle.error && <p role="alert" className="text-xs text-destructive">{toggle.error.message}</p>}
    </section>
  );
}
