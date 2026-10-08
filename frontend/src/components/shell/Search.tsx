import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Search as SearchIcon, X } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { tm } from "@/i18n";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";

/** Local search only: companies, people and securities already saved. */
export function Search() {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(input.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [input]);
  const results = useQuery({
    queryKey: ["search", term],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/search", { params: { query: { q: term } }, signal })),
    enabled: term.length > 0,
    staleTime: 30_000,
  });
  const items = results.data?.items ?? [];
  const options = [
    ...items.map((item) => ({ key: `${item.kind}-${item.id}`, href: item.href, item })),
    { key: "analysis", href: `/analysis/monthly?ticker=${encodeURIComponent(term.toUpperCase())}`, item: null },
  ];
  const close = () => {
    setOpen(false);
    setInput("");
  };
  return (
    <div ref={root} className="relative w-full max-w-md">
      <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <input
        role="combobox"
        aria-expanded={open && !!term}
        aria-controls="search-results"
        aria-activedescendant={open && term ? `search-option-${active}` : undefined}
        aria-label={t("ui.search.placeholder")}
        placeholder={t("ui.search.placeholder")}
        value={input}
        onChange={(event) => {
          setInput(event.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={(event) => {
          if (!root.current?.contains(event.relatedTarget)) setOpen(false);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            event.currentTarget.blur();
          }
          if (!term || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
          event.preventDefault();
          if (event.key === "Enter") {
            navigate(options[active].href, { state: sourceContext(location) });
            close();
          } else setActive((index) => (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length);
        }}
        className="h-8 w-full rounded-md border border-input bg-card pr-8 pl-8 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30"
      />
      {input && (
        <button
          aria-label={t("ui.search.clear")}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => setInput("")}
          className="absolute top-1/2 right-2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground"
        >
          <X className="size-3.5" />
        </button>
      )}
      {open && term && (
        <div
          id="search-results"
          role="listbox"
          aria-label={t("ui.search.results")}
          className="absolute top-10 right-0 left-0 z-50 overflow-hidden rounded-lg border bg-popover p-1 shadow-lg"
        >
          <p className="px-2 py-1.5 text-xs text-muted-foreground" role="status">
            {results.isPending
              ? t("ui.search.searching")
              : results.error
                ? results.error.message
                : items.length
                  ? t("ui.search.found", { count: items.length })
                  : t("ui.search.none")}
          </p>
          {options.map((option, index) => (
            <Link
              key={option.key}
              id={`search-option-${index}`}
              role="option"
              aria-selected={active === index}
              to={option.href}
              state={sourceContext(location)}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => setActive(index)}
              onClick={close}
              className={cn(
                "flex items-baseline gap-2 rounded-md px-2 py-1.5 text-sm",
                active === index && "bg-accent",
              )}
            >
              {option.item ? (
                <>
                  <span className="truncate font-medium">{option.item.name}</span>
                  {option.item.ticker && <span className="text-xs text-muted-foreground tabular-nums">{option.item.ticker}</span>}
                  <span className="ml-auto text-xs text-muted-foreground">{t(`ui.search.kind.${option.item.kind}`)}</span>
                </>
              ) : (
                <span className="text-muted-foreground">{t("ui.search.analysis", { ticker: term.toUpperCase() })}</span>
              )}
            </Link>
          ))}
          {results.data && <p className="px-2 pt-1 pb-1.5 text-xs text-muted-foreground">{tm(results.data.data.message)}</p>}
        </div>
      )}
    </div>
  );
}
