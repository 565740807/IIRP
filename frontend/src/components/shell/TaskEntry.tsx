import { lazy, Suspense, useState } from "react";
import { useTranslation } from "react-i18next";
import { ClipboardList, LoaderCircle } from "lucide-react";
import { useMyBatches } from "@/lib/personalTasks";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { BlockSkeleton } from "@/components/ui/states";

const TaskPanel = lazy(() => import("./Tasks").then(module => ({ default: module.TaskPanel })));

/** The small task summary stays on the top bar; its drawer loads when opened. */
export function TaskEntry() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<"active" | "attention">("active");
  const summary = useMyBatches("active");
  const counts = summary.data?.counts ?? {};
  const running = (counts.running ?? 0) + (counts.waiting ?? 0);
  const attention = counts.attention ?? 0;
  return <>
    <Button variant="ghost" onClick={() => {
      setCategory(running || !attention ? "active" : "attention");
      setOpen(true);
    }} aria-label={t("ui.tasks.entry_label", { running, attention })}>
      {running ? <LoaderCircle className="motion-safe:animate-spin" /> : <ClipboardList />}
      <span>{t("ui.tasks.title")}</span>
      {running > 0 && <Badge className="tabular-nums">{running}</Badge>}
      {!running && attention > 0 && <Badge variant="outline" className="tabular-nums">{attention}</Badge>}
    </Button>
    {open && <Suspense fallback={<BlockSkeleton height="h-screen" className="fixed top-0 right-0 z-50 w-[420px]" />}>
      <TaskPanel open={open} onOpenChange={setOpen} initialCategory={category} />
    </Suspense>}
  </>;
}
