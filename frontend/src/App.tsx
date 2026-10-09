import {
  lazy,
  Suspense,
  useLayoutEffect,
  useRef,
} from "react";
import {
  Link,
  Route,
  Routes,
  useLocation,
  useNavigationType,
} from "react-router-dom";
import { useTranslation } from "react-i18next";
import { MotionConfig } from "motion/react";
import { FileSearch, LoaderCircle } from "lucide-react";
import { AppShell } from "./components/shell/AppShell";
import { RefreshProvider } from "./lib/refresh";
import { HomePage } from "./pages/Home";
const DataPage = lazy(() =>
  import("./pages/Data").then((m) => ({ default: m.DataPage })),
);
const DiagnosticsPage = lazy(() =>
  import("./pages/Diagnostics").then((m) => ({ default: m.DiagnosticsPage })),
);
const SeasonalPage = lazy(() =>
  import("./pages/Seasonal").then((m) => ({ default: m.SeasonalPage })),
);
const EventsPage = lazy(() =>
  import("./pages/Events").then((m) => ({ default: m.EventsPage })),
);
const InsidersPage = lazy(() =>
  import("./pages/Insiders").then((m) => ({ default: m.InsidersPage })),
);
const EntityPage = lazy(() =>
  import("./pages/Entity").then((m) => ({ default: m.EntityPage })),
);
const TransactionPage = lazy(() =>
  import("./pages/Transaction").then((m) => ({ default: m.TransactionPage })),
);
const SectorsPage = lazy(() =>
  import("./pages/Sectors").then((m) => ({ default: m.SectorsPage })),
);
const MarketDetailPage = lazy(() =>
  import("./pages/Market").then((m) => ({ default: m.MarketDetailPage })),
);

function useReadingScroll() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const previousPath = useRef(location.pathname);
  useLayoutEffect(() => {
    const key = `iirp.scroll.${location.pathname}${location.search}`;
    let saved: number | null = null;
    try {
      const value = sessionStorage.getItem(key);
      if (value != null && Number.isFinite(Number(value)))
        saved = Number(value);
    } catch {
      /* Scrolling remains usable when browser storage is restricted. */
    }
    const restore =
      saved != null &&
      (navigationType === "POP" || location.pathname !== previousPath.current);
    let restoring = restore;
    let observer: ResizeObserver | undefined;
    let mutations: MutationObserver | undefined;
    let frame: number | undefined;
    let lastPosition = window.scrollY;
    const root = document.documentElement;
    const priorAnchor = root.style.overflowAnchor;
    const priorAnchorPriority = root.style.getPropertyPriority("overflow-anchor");
    const persist = () => {
      try {
        // A temporarily short loading layout cannot replace the reading intent.
        sessionStorage.setItem(key, String(restoring ? saved : lastPosition));
      } catch {
        /* Optional UI state. */
      }
    };
    const apply = () => {
      frame = undefined;
      if (!restoring || saved == null) return;
      if (Math.abs(window.scrollY - saved) > 1)
        window.scrollTo({ top: saved, behavior: "instant" });
    };
    const schedule = () => {
      if (restoring && frame == null) frame = window.requestAnimationFrame(apply);
    };
    const save = () => {
      if (restoring) {
        schedule();
        return;
      }
      lastPosition = window.scrollY;
      persist();
    };
    const finish = () => {
      restoring = false;
      observer?.disconnect();
      mutations?.disconnect();
      if (frame != null) window.cancelAnimationFrame(frame);
      root.style.setProperty("overflow-anchor", priorAnchor, priorAnchorPriority);
    };
    const interrupt = (event: Event) => {
      if (!restoring) return;
      if (event instanceof KeyboardEvent) {
        if (![
          "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " ", "Tab",
        ].includes(event.key)) return;
        const target = event.target as HTMLElement | null;
        if (target?.isContentEditable || target?.closest("input, textarea, select")) return;
      }
      finish();
      save();
    };
    const priorRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    if (restore) {
      // Reaching the target once is not completion: independent queries may
      // still shrink the document, then expand it again. Keep the intent until
      // the reader interacts or leaves, without a guessed network/layout timer.
      root.style.overflowAnchor = "none";
      observer = new ResizeObserver(schedule);
      observer.observe(document.body);
      mutations = new MutationObserver(schedule);
      mutations.observe(document.body, { childList: true, subtree: true, characterData: true });
      schedule();
    } else if (previousPath.current !== location.pathname)
      window.scrollTo({ top: 0, behavior: "instant" });
    previousPath.current = location.pathname;
    window.addEventListener("scroll", save, { passive: true });
    window.addEventListener("pagehide", save);
    for (const event of ["wheel", "touchstart", "pointerdown", "keydown"])
      window.addEventListener(event, interrupt, { passive: true });
    return () => {
      // Route teardown can already have shortened the document; keep the last observed position.
      persist();
      finish();
      window.history.scrollRestoration = priorRestoration;
      window.removeEventListener("scroll", save);
      window.removeEventListener("pagehide", save);
      for (const event of ["wheel", "touchstart", "pointerdown", "keydown"])
        window.removeEventListener(event, interrupt);
    };
  }, [location.key, location.pathname, location.search, navigationType]);
}
function Workbench() {
  const { t } = useTranslation();
  useReadingScroll();
  return (
    <AppShell>
      <Suspense
        fallback={
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground" role="status">
            <LoaderCircle className="size-4 motion-safe:animate-spin" />
            {t("ui.loading")}
          </div>
        }
      >
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/insiders" element={<InsidersPage />} />
          <Route path="/sectors" element={<SectorsPage />} />
          <Route
            path="/analysis/monthly"
            element={<SeasonalPage key="monthly" kind="monthly" />}
          />
          <Route
            path="/analysis/interval"
            element={<SeasonalPage key="interval" kind="interval" />}
          />
          <Route
            path="/analysis/earnings"
            element={<EventsPage key="earnings" kind="earnings" />}
          />
          <Route
            path="/analysis/events"
            element={<EventsPage key="custom" kind="custom" />}
          />
          <Route path="/data" element={<DataPage />} />
          <Route path="/data/diagnostics" element={<DiagnosticsPage />} />
          <Route
            path="/companies/:id"
            element={<EntityPage key="company" kind="company" />}
          />
          <Route
            path="/people/:id"
            element={<EntityPage key="person" kind="person" />}
          />
          <Route path="/transactions/:id" element={<TransactionPage />} />
          <Route path="/market/:symbol" element={<MarketDetailPage />} />
          <Route
            path="*"
            element={
              <section className="rounded-lg border border-dashed bg-card px-6 py-12 text-center">
                <FileSearch className="mx-auto size-10 text-muted-foreground" strokeWidth={1.2} aria-hidden />
                <h1 className="mt-3 text-base font-semibold">{t("ui.not_found.title")}</h1>
                <p className="mt-1 text-sm text-muted-foreground">{t("ui.not_found.description")}</p>
                <Link to="/" className="mt-3 inline-block text-sm font-medium underline-offset-2 hover:underline">
                  {t("ui.not_found.home")}
                </Link>
              </section>
            }
          />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
export function App() {
  return (
    <MotionConfig reducedMotion="user">
      <RefreshProvider>
        <Workbench />
      </RefreshProvider>
    </MotionConfig>
  );
}
