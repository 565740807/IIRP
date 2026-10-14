import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Mail } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { PageLanguage } from "@/components/PageLanguage";
import { BlockSkeleton } from "@/components/ui/states";

const ContactContent = lazy(() => import("./SecContactDialog").then(module => ({ default: module.SecContactDialog })));

export function useSecContact() {
  return useQuery({ queryKey: ["sec-contact"], queryFn: () => unwrap(client.GET("/api/v1/sec-contact")) });
}

export function SecContactDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return open ? <Suspense fallback={<BlockSkeleton height="h-64" className="fixed top-1/2 left-1/2 z-50 w-96 -translate-x-1/2 -translate-y-1/2" />}>
    <PageLanguage namespaces={["settings", "translation"]}><ContactContent open={open} onOpenChange={onOpenChange} /></PageLanguage>
  </Suspense> : null;
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
