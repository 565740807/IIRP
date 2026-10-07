import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Activity, BarChart3, Database, Home, RefreshCw, Users } from "lucide-react";
import { setLanguage, tm, type Language } from "@/i18n";
import { formatEt } from "@/lib/format";
import { useRefresh } from "@/lib/refresh";
import { researchHref } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Search } from "./Search";
import { SystemStatus } from "./SystemStatus";
import { TaskEntry } from "./Tasks";

const NAVIGATION = [
  { to: "/", key: "home", icon: Home },
  { to: "/insiders", key: "insiders", icon: Users },
  { to: "/analysis/monthly", key: "analysis", icon: BarChart3 },
  { to: "/data", key: "data", icon: Database },
] as const;

/** Page name for the top bar and the document title. */
function pageKey(pathname: string) {
  if (pathname === "/") return "home";
  if (pathname.startsWith("/insiders")) return "insiders";
  if (pathname.startsWith("/analysis")) return "analysis";
  if (pathname.startsWith("/data")) return "data";
  if (pathname.startsWith("/companies/")) return "company";
  if (pathname.startsWith("/people/")) return "person";
  if (pathname.startsWith("/transactions/")) return "transaction";
  if (pathname.startsWith("/market/")) return "market";
  return "not_found";
}

function Sidebar({ openSystem }: { openSystem: () => void }) {
  const { t } = useTranslation();
  const location = useLocation();
  return (
    <aside className="sticky top-0 flex h-screen w-52 shrink-0 flex-col border-r bg-card">
      <Link to="/" className="flex h-14 items-center gap-2 px-5" aria-label={t("ui.nav.brand_home")}>
        <span className="grid size-7 place-items-center rounded-md bg-primary text-sm font-semibold text-primary-foreground">I</span>
        <span className="font-semibold tracking-tight">IIRP</span>
      </Link>
      <nav aria-label={t("ui.nav.label")} className="flex flex-col gap-0.5 px-3 py-2">
        {NAVIGATION.map((item) => (
          <NavLink
            key={item.to}
            to={item.key === "analysis" ? researchHref("monthly") : item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              cn(
                "flex h-9 items-center gap-2.5 rounded-md px-2.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                (isActive || (item.key === "analysis" && location.pathname.startsWith("/analysis"))) &&
                  "bg-accent font-medium text-foreground",
              )
            }
          >
            <item.icon className="size-4" strokeWidth={1.8} />
            {t(`ui.nav.${item.key}`)}
          </NavLink>
        ))}
      </nav>
      <button
        onClick={openSystem}
        className="mx-3 mt-auto mb-3 flex h-9 items-center gap-2.5 rounded-md px-2.5 text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <Activity className="size-4" strokeWidth={1.8} />
        {t("ui.nav.system")}
      </button>
    </aside>
  );
}

/** Spinning icon while a check or fetch runs; a click asks for fresh data now. */
function RefreshIndicator() {
  const { t } = useTranslation();
  const { refreshing, freshness, error, checkNow } = useRefresh();
  const sources = (freshness?.sources ?? {}) as Record<string, { stage?: string; last_checked_at?: string | null } | undefined>;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon" onClick={checkNow} aria-label={t("ui.refresh.check_now")} aria-busy={refreshing}>
          <RefreshCw className={cn(refreshing && "motion-safe:animate-spin", error && "text-warn")} />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs text-left">
        <p className="font-medium">{refreshing ? t("ui.refresh.refreshing") : t("ui.refresh.check_now")}</p>
        {(["sec", "market"] as const).map((key) =>
          sources[key] ? (
            <p key={key}>
              {t(`ui.refresh.source.${key}`)}: {tm(sources[key]?.stage)}
              {sources[key]?.last_checked_at && <> · {formatEt(sources[key]?.last_checked_at, { date: false })}</>}
            </p>
          ) : null,
        )}
        {error && <p>{error.message}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

function LanguageSwitch() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  return (
    <ToggleGroup
      type="single"
      size="sm"
      variant="outline"
      spacing={0}
      aria-label={t("ui.language.label")}
      value={i18n.language}
      onValueChange={(value) => {
        if (!value || value === i18n.language) return;
        void setLanguage(value as Language).then(() =>
          // Pages not yet moved over translate when they read; read them again.
          queryClient.invalidateQueries({ predicate: (query) => !["home", "my-tasks", "freshness", "system", "search"].includes(String(query.queryKey[0])) }),
        );
      }}
    >
      <ToggleGroupItem value="en" lang="en" className="px-2.5">English</ToggleGroupItem>
      <ToggleGroupItem value="zh" lang="zh-CN" className="px-2.5">中文</ToggleGroupItem>
    </ToggleGroup>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const { refreshing } = useRefresh();
  const [systemOpen, setSystemOpen] = useState(false);
  const title = t(`ui.page.${pageKey(location.pathname)}`);
  useEffect(() => {
    document.title = `${title} · IIRP`;
  }, [title, i18n.language]);
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex min-h-screen">
        <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-card focus:px-3 focus:py-2">
          {t("ui.nav.skip")}
        </a>
        <Sidebar openSystem={() => setSystemOpen(true)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-14 items-center gap-4 border-b bg-background/90 px-6 backdrop-blur">
            <h1 className="w-36 shrink-0 truncate text-base font-semibold">{title}</h1>
            <Search />
            <div className="ml-auto flex items-center gap-1.5">
              <RefreshIndicator />
              <TaskEntry />
              <LanguageSwitch />
            </div>
            {refreshing && (
              <div className="absolute inset-x-0 bottom-0 h-0.5 overflow-hidden" role="progressbar" aria-label={t("ui.refresh.refreshing")}>
                <div className="h-full w-full origin-left bg-primary/60 motion-safe:animate-progress" />
              </div>
            )}
          </header>
          <main id="main-content" tabIndex={-1} className="w-full max-w-[1280px] flex-1 px-6 py-5 outline-none">
            {children}
          </main>
        </div>
        <SystemStatus open={systemOpen} onOpenChange={setSystemOpen} />
      </div>
    </TooltipProvider>
  );
}
