import { lazy, Suspense, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Settings as SettingsIcon } from "lucide-react";
import { useDisplaySettings } from "@/lib/display";
import { OPEN_SETTINGS, SETTINGS_TABS, type SettingsTab } from "@/lib/settings";
import { Button } from "@/components/ui/button";
import { PageLanguage } from "@/components/PageLanguage";
import { BlockSkeleton } from "@/components/ui/states";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const SettingsDialog = lazy(() => import("./SettingsDialog").then(module => ({ default: module.SettingsDialog })));

/** Gear button and lightweight event listener; the dialog loads when opened. */
export function SettingsButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<SettingsTab>("display");
  useDisplaySettings();
  useEffect(() => {
    const listener = (event: Event) => {
      const wanted = (event as CustomEvent<SettingsTab>).detail;
      setTab(SETTINGS_TABS.includes(wanted) ? wanted : "display");
      setOpen(true);
    };
    window.addEventListener(OPEN_SETTINGS, listener);
    return () => window.removeEventListener(OPEN_SETTINGS, listener);
  }, []);
  return <>
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon" onClick={() => setOpen(true)} aria-label={t("ui.settings.open")}>
          <SettingsIcon />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{t("ui.settings.open")}</TooltipContent>
    </Tooltip>
    {open && <Suspense fallback={<BlockSkeleton height="h-64" className="fixed top-1/2 left-1/2 z-50 w-96 -translate-x-1/2 -translate-y-1/2" />}>
      <PageLanguage namespaces={["settings", "translation"]}><SettingsDialog open={open} setOpen={setOpen} tab={tab} setTab={setTab} /></PageLanguage>
    </Suspense>}
  </>;
}
