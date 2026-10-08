import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, LoaderCircle, Mail } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const input = "h-8 w-full rounded-md border bg-background px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30";

export function useSecContact() {
  return useQuery({ queryKey: ["sec-contact"], queryFn: () => unwrap(client.GET("/api/v1/sec-contact")) });
}

/** Name and e-mail for the SEC User-Agent, saved in the local database; read-only when deploy/.env sets it. */
export function SecContactDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const contact = useSecContact();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const save = useMutation({
    mutationFn: () => unwrap(client.PUT("/api/v1/sec-contact", { body: { name, email } })),
    onSuccess: (data) => {
      queryClient.setQueryData(["sec-contact"], data);
      for (const key of ["collection-policy", "system", "system-health", "freshness"])
        void queryClient.invalidateQueries({ queryKey: [key] });
    },
  });
  const { reset } = save;
  useEffect(() => {
    if (!open) return;
    reset();
    setName(contact.data?.name ?? "");
    setEmail(contact.data?.email ?? "");
  }, [open, contact.data?.name, contact.data?.email, reset]);
  const data = contact.data;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("ui.sec_contact.title")}</DialogTitle>
          <DialogDescription>{t("ui.sec_contact.description")}</DialogDescription>
        </DialogHeader>
        {data && !data.editable ? (
          <p className="flex gap-2 rounded-md border bg-secondary/50 px-3 py-2 text-sm">
            {data.configured ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-up" /> : <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />}
            <span>{data.configured ? t("ui.sec_contact.from_config") : t("ui.sec_contact.banner_config")}</span>
          </p>
        ) : (
          <form onSubmit={submit} className="space-y-3 text-sm">
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">{t("ui.sec_contact.name")}</span>
              <input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" maxLength={80} required className={input} />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">{t("ui.sec_contact.email")}</span>
              <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" maxLength={254} required className={input} />
            </label>
            {(name.trim() || email.trim()) && (
              <p className="text-xs text-muted-foreground">
                {t("ui.sec_contact.preview")}: <code className="rounded bg-secondary px-1 py-0.5">{`${name.trim().replace(/\s+/g, " ")} ${email.trim()}`}</code>
              </p>
            )}
            {save.error && <p role="alert" className="text-destructive">{save.error.message}</p>}
            {save.isSuccess && (
              <p role="status" className="flex items-center gap-1.5 text-up">
                <CheckCircle2 className="size-4" />
                {t("ui.sec_contact.saved")}
              </p>
            )}
            <div className="flex justify-end">
              <Button type="submit" size="sm" disabled={save.isPending || !name.trim() || !email.trim()}>
                {save.isPending && <LoaderCircle className="motion-safe:animate-spin" />}
                {t("ui.sec_contact.save")}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Home page notice while SEC has no usable contact; opens the dialog. */
export function SecContactBanner() {
  const { t } = useTranslation();
  const contact = useSecContact();
  const [open, setOpen] = useState(false);
  const data = contact.data;
  return (
    <>
      {data && !data.configured && (
        <div role="status" className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-warn/40 bg-card px-4 py-2.5 text-sm">
          <Mail className="size-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1">{data.editable ? t("ui.sec_contact.banner") : t("ui.sec_contact.banner_config")}</span>
          {data.editable && (
            <Button size="sm" onClick={() => setOpen(true)}>
              {t("ui.sec_contact.add")}
            </Button>
          )}
        </div>
      )}
      <SecContactDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
