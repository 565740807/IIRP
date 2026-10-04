import { systemRefreshInterval } from "./systemRefresh";
import { Timestamp } from "./components/Timestamp";
import {
  lazy,
  Suspense,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigationType,
} from "react-router-dom";
import {
  Activity,
  BarChart3,
  ClipboardList,
  Database,
  Home,
  Menu,
  Search,
  Users,
  X,
} from "lucide-react";
import {
  api,
  formatNumber,
  useBatches,
  rows,
  display,
  type GenericOutput,
  type System,
} from "./api";
import { FreshnessStrip } from "./components/Freshness";
import { researchHref, sourceContext } from "./researchStorage";
import { BatchList } from "./components/Batches";
import { taskEntryCategory } from "./taskPresentation";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Loading,
  Modal,
  NoticeProvider,
} from "./components/ui";
import { HomePage, InsiderPage } from "./pages/Home";
import { DataPage, DiagnosticsPage } from "./pages/Data";
const AnalysisPage = lazy(() =>
  import("./pages/Analysis").then((m) => ({ default: m.AnalysisPage })),
);
const EventsPage = lazy(() =>
  import("./pages/Events").then((m) => ({ default: m.EventsPage })),
);
const EntityPage = lazy(() =>
  import("./pages/Details").then((m) => ({ default: m.EntityPage })),
);
const TransactionPage = lazy(() =>
  import("./pages/Details").then((m) => ({ default: m.TransactionPage })),
);
const MarketDetailPage = lazy(() =>
  import("./pages/Details").then((m) => ({ default: m.MarketDetailPage })),
);

const navigation = [
  { to: "/", label: "首页", icon: Home },
  { to: "/insiders", label: "Insider", icon: Users },
  { to: "/analysis/monthly", label: "分析中心", icon: BarChart3 },
  { to: "/data", label: "数据与任务", icon: Database },
];
function Navigation({ close }: { close?: () => void }) {
  const location = useLocation();
  return (
    <nav aria-label="主导航">
      {navigation.map((item) => (
        <NavLink
          key={item.to}
          to={
            item.to === "/analysis/monthly" ? researchHref("monthly") : item.to
          }
          end={item.to === "/"}
          onClick={close}
          title={item.label}
          className={({ isActive }) =>
            `nav-link ${isActive || (item.to.startsWith("/analysis") && location.pathname.startsWith("/analysis")) ? "active" : ""}`
          }
        >
          <item.icon size={21} strokeWidth={1.7} />
          <span>{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
function GlobalSearch() {
  const location = useLocation();
  const [input, setInput] = useState("");
  const [value, setValue] = useState("");
  const [focused, setFocused] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const searchRoot = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setValue(input.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [input]);
  const results = useQuery({
    queryKey: ["search", value],
    queryFn: () => api<GenericOutput>(`/search?q=${encodeURIComponent(value)}`),
    enabled: value.length > 0,
  });
  return (
    <div className="search-wrapper" ref={searchRoot}>
      <Search size={17} />
      <input
        aria-label="搜索公司、股票或 Insider"
        role="combobox"
        aria-expanded={focused && !!value}
        aria-controls="global-search-results"
        aria-activedescendant={
          focused && value ? `search-option-${selectedIndex}` : undefined
        }
        placeholder="搜索公司、股票或 Insider"
        value={input}
        onChange={(event) => {
          setInput(event.target.value);
          setSelectedIndex(0);
        }}
        onFocus={() => setFocused(true)}
        onBlur={(event) => {
          if (!event.currentTarget.parentElement?.contains(event.relatedTarget))
            setFocused(false);
        }}
        onKeyDown={(event) => {
          const links =
            searchRoot.current?.querySelectorAll<HTMLAnchorElement>(
              ".search-results a",
            );
          if (
            links?.length &&
            ["ArrowDown", "ArrowUp", "Enter"].includes(event.key)
          ) {
            event.preventDefault();
            if (event.key === "Enter") links[selectedIndex]?.click();
            else
              setSelectedIndex(
                (index) =>
                  (index +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    links.length) %
                  links.length,
              );
          }
          if (event.key === "Escape") {
            setFocused(false);
            event.currentTarget.blur();
          }
        }}
      />
      {input && (
        <button
          aria-label="清空搜索"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setInput("");
            setValue("");
          }}
        >
          <X size={14} />
        </button>
      )}
      {focused && value && (
        <div
          className="search-results"
          id="global-search-results"
          role="listbox"
          aria-label="本地搜索结果"
        >
          {results.isPending
            ? "正在查询本地索引…"
            : results.error
              ? results.error.message
              : results.data?.items.length
                ? `找到 ${results.data.items.length} 个本地结果`
                : "暂无本地结果"}
          {rows(results.data?.items).map((item, index) => (
            <Link
              id={`search-option-${index}`}
              state={sourceContext(location)}
              role="option"
              aria-selected={selectedIndex === index}
              key={`${item.kind ?? item.type}-${item.id}`}
              to={
                typeof item.href === "string" &&
                item.href.startsWith("/") &&
                !item.href.startsWith("//")
                  ? item.href
                  : (item.kind ?? item.type) === "person" ||
                      (item.kind ?? item.type) === "owner"
                    ? `/people/${item.id}`
                    : (item.kind ?? item.type) === "company"
                      ? `/companies/${item.id}`
                      : `/analysis/monthly?ticker=${encodeURIComponent(item.ticker ?? item.name ?? item.label)}`
              }
              onClick={() => {
                setFocused(false);
                setInput("");
              }}
            >
              {display(item.name ?? item.label)}{" "}
              {item.ticker ? `· ${item.ticker}` : ""}
              <small>
                {" "}
                ·{" "}
                {(
                  {
                    company: "公司",
                    issuer: "公司",
                    person: "交易者",
                    owner: "交易者",
                    security: "证券",
                  } as Record<string, string>
                )[item.kind ?? item.type] ?? "证券"}
              </small>
            </Link>
          ))}
          <Link
            id={`search-option-${rows(results.data?.items).length}`}
            role="option"
            aria-selected={selectedIndex === rows(results.data?.items).length}
            to={`/analysis/monthly?ticker=${encodeURIComponent(value.toUpperCase())}`}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              setFocused(false);
              setInput("");
            }}
          >
            前往行情分析
          </Link>
          <small>搜索只读本地数据，不触发外部下载。</small>
        </div>
      )}
    </div>
  );
}
function SystemPanel({ open, close }: { open: boolean; close: () => void }) {
  const query = useQuery({
    queryKey: ["system"],
    queryFn: () => api<System>("/system"),
    enabled: open,
    refetchInterval: (state) => systemRefreshInterval(state.state.data?.storage?.inventory_status),
  });
  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="系统状态"
      description="本地服务、存储与备份状态。"
    >
      <ErrorNotice error={query.error} retry={query.refetch} />
      {query.isPending ? (
        <Loading />
      ) : (
        query.data && (
          <>
            <dl className="definition-list">
              <dt>运行环境</dt>
              <dd>
                {query.data.mode === "development"
                  ? "Linux 本地服务"
                  : query.data.mode}
              </dd>
              <dt>版本</dt>
              <dd>{query.data.version}</dd>
              <dt>后台服务</dt>
              <dd>
                <i
                  className={`status-dot ${query.data.worker.online ? "green" : "amber"}`}
                />
                {query.data.worker.online === null ? "状态未知（数据库读取失败）" : query.data.worker.online ? "在线" : "离线，任务仍保留"}
              </dd>
              <dt>最近心跳</dt>
              <dd><Timestamp value={query.data.worker.last_seen} /></dd>
              <dt>可用磁盘</dt>
              <dd>
                {typeof query.data.storage.free_bytes === "number"
                  ? `${formatNumber(query.data.storage.free_bytes / 1024 ** 3)} GiB`
                  : "未知（磁盘测量失败）"}
              </dd>
              <dt>登记来源对象</dt>
              <dd>
                {query.data.storage.inventory_status === "fresh"
                  ? `${query.data.storage.objects} 个 · 内容长度 ${formatNumber(query.data.storage.bytes / 1024 ** 2)} MiB；目录分配 ${formatNumber(query.data.storage.paths?.evidence?.allocated_bytes / 1024 ** 2)} MiB`
                  : "盘点未完成或已过期"}
              </dd>
              <dt>容量盘点</dt>
              <dd>
                {query.data.storage.inventory_status === "fresh"
                  ? `已测量 ${query.data.storage.inventory_measured_at}；恢复预估 ${formatNumber(query.data.storage.restore_estimated_temporary_bytes / 1024 ** 3)} GiB，保留 ${formatNumber(query.data.storage.min_free_bytes / 1024 ** 3)} GiB；${query.data.storage.restore_capacity_sufficient === null ? "估算空间未知" : query.data.storage.restore_capacity_sufficient ? "估算空间足够" : "估算空间不足"}`
                  : `${query.data.storage.inventory_status === "failed" ? "盘点失败" : query.data.storage.inventory_status === "stale" ? "盘点已过期" : "盘点进行中"}；容量结论未知${query.data.storage.inventory_error ? `：${query.data.storage.inventory_error}` : ""}`}
              </dd>
              <dt>数据库迁移</dt>
              <dd>
                {typeof query.data.migration === "string"
                  ? query.data.migration
                  : JSON.stringify(query.data.migration)}
              </dd>
            </dl>
            <div className="notice notice-info">
              {query.data.automatic_collection_scope}
            </div>
            <p className="muted">
              当前服务运行在 Linux，页面通过本机连接访问。
            </p>
          </>
        )
      )}
    </Modal>
  );
}
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
    } else if (
      previousPath.current !== location.pathname &&
      !(
        location.pathname === "/data" &&
        new URLSearchParams(location.search).has("events")
      )
    )
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
function AppShell() {
  const [mobileNav, setMobileNav] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);
  const [taskCategory, setTaskCategory] = useState("active");
  const [systemOpen, setSystemOpen] = useState(false);
  const location = useLocation();
  useReadingScroll();
  const jobs = useBatches("active", "", "", "personal");
  const active = (jobs.data?.counts?.running ?? 0) + (jobs.data?.counts?.waiting ?? 0);
  const attention = jobs.data?.counts?.attention ?? 0;
  const activityLabel = jobs.isPending ? "读取任务…" : jobs.isError ? "任务状态暂不可用"
    : active ? `我的任务 ${active}` : attention ? `待处理 ${attention}` : "我的任务";
  const currentTitle = location.pathname.startsWith("/analysis")
    ? "分析中心"
    : (navigation.find((item) => item.to === location.pathname)?.label ??
      (location.pathname.startsWith("/companies/")
        ? "公司历史"
        : location.pathname.startsWith("/people/")
          ? "人员历史"
          : location.pathname.startsWith("/transactions/")
            ? "交易与价格"
            : location.pathname.startsWith("/market/")
              ? "市场详情"
              : location.pathname.startsWith("/data")
                ? "数据与任务"
                : "页面未找到"));
  useEffect(() => {
    document.title = `${currentTitle} · IIRP`;
  }, [location.pathname, currentTitle]);
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="IIRP 首页">
          <span className="brand-mark">I</span>
          <strong>IIRP</strong>
        </Link>
        <Navigation />
        <button
          className="system-link"
          onClick={() => setSystemOpen(true)}
          title="系统状态"
        >
          <Activity size={20} strokeWidth={1.7} />
          <span>系统状态</span>
        </button>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <Button
            variant="ghost"
            className="mobile-menu"
            aria-label="打开导航"
            onClick={() => setMobileNav(true)}
          >
            <Menu size={21} />
          </Button>
          <span className="topbar-title">{currentTitle}</span>
          <GlobalSearch />
          <Button
            className="task-entry"
            aria-label={`后台任务，${activityLabel}`}
            variant="ghost"
            onClick={() => {
              setTaskCategory(taskEntryCategory(jobs.data?.counts));
              setTasksOpen(true);
            }}
          >
            <ClipboardList size={18} />
            <span>
              {jobs.isPending
                ? "读取任务…"
                : jobs.error
                  ? "任务状态不可用"
                  : activityLabel}
            </span>
          </Button>
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>
          <FreshnessStrip />
          <Suspense fallback={<Loading label="加载研究视图…" />}>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/insiders" element={<InsiderPage />} />
              <Route
                path="/analysis/monthly"
                element={<AnalysisPage key="monthly" />}
              />
              <Route
                path="/analysis/interval"
                element={<AnalysisPage key="interval" />}
              />
              <Route
                path="/analysis/earnings"
                element={<AnalysisPage key="earnings" />}
              />
              <Route path="/analysis/events" element={<EventsPage />} />
              <Route path="/data" element={<DataPage />} />
              <Route path="/data/diagnostics" element={<DiagnosticsPage />} />
              <Route
                path="/companies/:id"
                element={<EntityPage kind="company" />}
              />
              <Route
                path="/people/:id"
                element={<EntityPage kind="person" />}
              />
              <Route path="/transactions/:id" element={<TransactionPage />} />
              <Route path="/market/:symbol" element={<MarketDetailPage />} />
              <Route
                path="*"
                element={
                  <section className="panel">
                    <EmptyState
                      title="这个页面尚不存在"
                      description="从导航进入已建立的工作台入口。"
                    >
                      <Link to="/" className="text-link">
                        回到首页
                      </Link>
                    </EmptyState>
                  </section>
                }
              />
            </Routes>
          </Suspense>
        </main>
      </div>
      <Modal
        open={mobileNav}
        onOpenChange={setMobileNav}
        title="IIRP"
        description="研究工作台"
        drawer
      >
        <Navigation close={() => setMobileNav(false)} />
        <Button
          variant="ghost"
          onClick={() => {
            setMobileNav(false);
            setSystemOpen(true);
          }}
        >
          <Activity size={18} />
          系统状态
        </Button>
      </Modal>
      <Modal
        open={tasksOpen}
        onOpenChange={setTasksOpen}
        title="我的任务"
        description="显示你发起的任务。自动采集与完整历史在“数据与任务”查看。"
        drawer
      >
        <BatchList compact initialCategory={taskCategory} onCategoryChange={setTaskCategory} />
        <Link
          className="text-link drawer-footer-link"
          to={`/data?category=${taskCategory === "active" ? ((jobs.data?.counts?.running ?? 0) ? "running" : "waiting") : taskCategory}`}
          onClick={() => setTasksOpen(false)}
        >
          打开数据与任务
        </Link>
      </Modal>
      <SystemPanel open={systemOpen} close={() => setSystemOpen(false)} />
    </div>
  );
}
export function App() {
  return (
    <NoticeProvider>
      <AppShell />
    </NoticeProvider>
  );
}
