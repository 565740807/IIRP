import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import * as echarts from "echarts/core";
import { CandlestickChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { components } from "../generated/api";
import { api, formatTime, percent, requestId } from "../api";
import { researchHref } from "../researchStorage";
import { useEventResearch, useEventSets } from "../researchQueries";
import { researchKeys } from "../queryIdentity";
import { Button, ErrorNotice, Loading, useNotice } from "../components/common";
import "../analysis.css";

echarts.use([CandlestickChart, GridComponent, TooltipComponent, CanvasRenderer]);

type Kind = "earnings" | "custom";
type Prompt = components["schemas"]["EventPromptOutput"];
type Defaults = components["schemas"]["EventDefaultsOutput"];
type Validation = components["schemas"]["EventValidateOutput"];
type SetCreated = components["schemas"]["EventSetCreated"];
type SetOutput = components["schemas"]["EventSetOutput"];
type Analysis = components["schemas"]["EventAnalysisOutput"];
type TickerResult = components["schemas"]["EventTickerResult"];
type Stats = components["schemas"]["EventStatistics"];
type Row = components["schemas"]["EventRow"];

const tabs = [
  { id: "monthly", label: "月度分析" },
  { id: "interval", label: "区间分析" },
  { id: "earnings", label: "财报行情" },
  { id: "events", label: "自定义事件分析" },
];
const sessionLabels: Record<string, string> = {
  before_open: "盘前",
  during: "盘中",
  after_close: "盘后",
  unknown: "未知（按当天）",
};
const noteLabels: Record<string, string> = {
  session_unknown: "时段未知，按当天处理",
  market_closed_on_date: "当天休市，顺延到下一交易日",
};
const windowRows = (n: number) => [
  ["before", `事前 ${n} 日`, "C(R−1) / C(R−1−n) − 1"],
  ["reaction", "反应日", "C(R) / C(R−1) − 1"],
  ["gap", "开盘跳空", "O(R) / C(R−1) − 1"],
  ["after", `事后 ${n} 日`, "C(R+n) / C(R) − 1"],
] as const;

function Change({ value }: { value?: string | null }) {
  const n = Number(value);
  const tone = value == null || !Number.isFinite(n) || n === 0 ? "" : n > 0 ? "ev-up" : "ev-down";
  return <span className={tone}>{percent(value)}</span>;
}

function ratio(stats?: Stats) {
  if (!stats || !stats.n) return "—";
  return `${stats.up}/${stats.n}（${Math.round(Number(stats.up_ratio) * 100)}%）`;
}

function fill(template: string, values: Record<string, string>) {
  return Object.entries(values).reduce(
    (text, [key, value]) => (value.trim() ? text.split(`{{${key}}}`).join(value.trim()) : text),
    template,
  );
}

function AnalysisTabs({ kind }: { kind: Kind }) {
  const current = kind === "earnings" ? "earnings" : "events";
  return (
    <div className="analysis-tabs">
      {tabs.map((t) => (
        <Link
          key={t.id}
          to={t.id === "monthly" || t.id === "interval" ? researchHref(t.id) : `/analysis/${t.id}`}
          className={current === t.id ? "selected" : ""}
        >
          {t.label}
        </Link>
      ))}
    </div>
  );
}

function PromptPanel({ kind }: { kind: Kind }) {
  const notice = useNotice();
  const client = useQueryClient();
  const [language, setLanguage] = useState<"zh" | "en">("zh");
  const [tickers, setTickers] = useState("AAPL");
  const [years, setYears] = useState("8");
  const [event, setEvent] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const key = ["event-prompt", kind, language];
  const prompt = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => api<Prompt>(`/events/prompts/${kind}?language=${language}`, "GET", undefined, signal),
  });
  const save = useMutation({
    mutationFn: (text: string | null) =>
      text === null
        ? api<Prompt>(`/events/prompts/${kind}?language=${language}`, "DELETE")
        : api<Prompt>(`/events/prompts/${kind}?language=${language}`, "PUT", { text }),
    onSuccess: (data) => {
      client.setQueryData(key, data);
      setEditing(null);
      notice({ text: data.is_default ? "已恢复默认模板" : "模板已保存" });
    },
  });
  const filled = prompt.data
    ? fill(prompt.data.text, { tickers, years, event })
    : "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(filled);
      notice({ text: "提示词已复制，请粘贴到其他 AI 平台" });
    } catch {
      notice({ text: "浏览器不允许自动复制，请手动选中文本复制", error: true });
    }
  };
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>① 复制提示词，到其他 AI 平台询问日期</h2>
        <div className="inline-controls">
          <label>
            语言
            <select value={language} onChange={(e) => setLanguage(e.target.value as "zh" | "en")}>
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </label>
        </div>
      </div>
      <div className="analysis-form">
        <label>
          股票代码（逗号分隔）
          <input value={tickers} onChange={(e) => setTickers(e.target.value.toUpperCase())} />
        </label>
        <label>
          最近几年
          <input type="number" min="1" max="30" value={years} onChange={(e) => setYears(e.target.value)} />
        </label>
        {kind === "custom" && (
          <label>
            事件说明
            <input value={event} placeholder="例如：Apple WWDC 开幕主题演讲" onChange={(e) => setEvent(e.target.value)} />
          </label>
        )}
      </div>
      <ErrorNotice error={prompt.error ?? save.error} />
      {editing === null ? (
        <>
          <textarea className="ev-text" readOnly rows={10} value={filled} aria-label="提示词" />
          <div className="button-row">
            <Button variant="primary" onClick={() => void copy()} disabled={!prompt.data}>
              复制提示词
            </Button>
            <Button onClick={() => setEditing(prompt.data?.text ?? "")} disabled={!prompt.data}>
              编辑模板
            </Button>
            <small>
              {prompt.data?.is_default ? "默认模板" : `自定义模板 · ${formatTime(prompt.data?.updated_at)}`}
              。{"{{tickers}}"}、{"{{years}}"}
              {kind === "custom" ? `、{{event}}` : ""} 会替换为上面填写的内容。
            </small>
          </div>
        </>
      ) : (
        <>
          <textarea className="ev-text" rows={14} value={editing} onChange={(e) => setEditing(e.target.value)} aria-label="编辑模板" />
          <div className="button-row">
            <Button variant="primary" busy={save.isPending} onClick={() => save.mutate(editing)}>
              保存模板
            </Button>
            <Button onClick={() => setEditing(null)}>取消</Button>
            <Button variant="ghost" busy={save.isPending} disabled={prompt.data?.is_default} onClick={() => save.mutate(null)}>
              恢复默认
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

function WindowPicker({ value, onChange, defaults }: { value: number; onChange: (n: number) => void; defaults?: Defaults }) {
  return (
    <label>
      前后交易日 n
      <span className="inline-controls">
        {[1, 3, 5, 10, 20].map((n) => (
          <button type="button" key={n} className={`chip ${value === n ? "selected" : ""}`} onClick={() => onChange(n)}>
            {n}
          </button>
        ))}
        <input
          type="number"
          className="ev-n"
          min={defaults?.min ?? 1}
          max={defaults?.max ?? 60}
          value={value}
          aria-label="自定义 n"
          onChange={(e) => onChange(e.target.valueAsNumber || 1)}
        />
      </span>
    </label>
  );
}

function PastePanel({ kind, defaults, onCreated }: { kind: Kind; defaults?: Defaults; onCreated: (created: SetCreated) => void }) {
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [n, setN] = useState<number | null>(null);
  const [checked, setChecked] = useState<Validation | null>(null);
  const [checkError, setCheckError] = useState<Error | null>(null);
  const sessions = n ?? defaults?.window_sessions[kind] ?? 5;
  useEffect(() => {
    if (!text.trim()) {
      setChecked(null);
      return;
    }
    let current = true;
    const timer = setTimeout(() => {
      api<Validation>("/events/validate", "POST", { kind, text })
        .then((value) => current && (setChecked(value), setCheckError(null)))
        .catch((error: Error) => current && setCheckError(error));
    }, 400);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [text, kind]);
  const create = useMutation({
    mutationFn: () =>
      api<SetCreated>("/events/sets", "POST", {
        kind, text, title: title.trim() || null, request_id: requestId(), analyze: true, n: sessions,
      }),
    onSuccess: (created) => {
      setText("");
      setTitle("");
      onCreated(created);
    },
  });
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>② 粘贴 AI 返回的 JSON</h2>
        <span>粘贴后自动校验</span>
      </div>
      <textarea
        className="ev-text"
        rows={8}
        value={text}
        placeholder='{"events":[{"ticker":"AAPL","date":"2025-10-30","session":"after_close", …}]}'
        onChange={(e) => setText(e.target.value)}
        aria-label="粘贴 JSON"
      />
      <ErrorNotice error={checkError} />
      {checked && !checked.valid && (
        <div role="alert" className="notice notice-error ev-errors">
          <strong>有 {checked.errors.length} 处需要修改：</strong>
          <ul>
            {checked.errors.map((e, i) => (
              <li key={i}>
                {e.index ? `第 ${e.index} 条` : "整体"}
                {e.field ? ` · ${e.field}` : ""}：{e.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {checked?.valid && (
        <>
          <p className="context-baseline">
            校验通过：{checked.events.length} 条，{checked.tickers.join("、")}。R 为反应日（盘后公布顺延到下一交易日）。
          </p>
          <EventPreview events={checked.events} kind={kind} />
          <div className="analysis-form">
            <label>
              名称（可选）
              <input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="留空自动命名" />
            </label>
            <WindowPicker value={sessions} onChange={setN} defaults={defaults} />
          </div>
          <ErrorNotice error={create.error} />
          <div className="button-row">
            <Button variant="primary" busy={create.isPending} onClick={() => create.mutate()}>
              ③ 保存并开始分析
            </Button>
            <small>每只股票只下载一次行情（24 小时缓存）。</small>
          </div>
        </>
      )}
    </section>
  );
}

function EventPreview({ events, kind }: { events: components["schemas"]["EventItem"][]; kind: Kind }) {
  return (
    <div className="table-scroll ev-preview">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>股票</th>
            <th>日期</th>
            <th>时段</th>
            <th>反应日 R</th>
            <th>名称</th>
            {kind === "earnings" && <th>财年/季</th>}
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{e.ticker}</td>
              <td>{e.date}</td>
              <td>{sessionLabels[e.session]}</td>
              <td>{e.reaction_date ?? "—"}</td>
              <td>{e.name}</td>
              {kind === "earnings" && <td>{e.fiscal_year ? `FY${e.fiscal_year} Q${e.fiscal_quarter}` : "—"}</td>}
              <td>{e.note ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SavedSets({ kind, defaults, onAnalysis }: { kind: Kind; defaults?: Defaults; onAnalysis: (id: string) => void }) {
  const sets = useEventSets(kind);
  const client = useQueryClient();
  const notice = useNotice();
  const [open, setOpen] = useState<string | null>(null);
  const [n, setN] = useState<number | null>(null);
  const sessions = n ?? defaults?.window_sessions[kind] ?? 5;
  const detail = useQuery({
    queryKey: researchKeys.set(open ?? "", ""),
    enabled: !!open,
    queryFn: ({ signal }) => api<SetOutput>(`/events/sets/${open}`, "GET", undefined, signal),
  });
  const analyze = useMutation({
    mutationFn: (id: string) =>
      api<components["schemas"]["EventAnalysisCreated"]>(`/events/sets/${id}/analyses`, "POST", { request_id: requestId(), n: sessions }),
    onSuccess: (created) => onAnalysis(created.analysis_id),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/events/sets/${id}`, "DELETE"),
    onSuccess: () => {
      setOpen(null);
      void client.invalidateQueries({ queryKey: researchKeys.sets(kind) });
      notice({ text: "事件集已删除；已有分析结果不受影响" });
    },
  });
  const items = sets.data?.items ?? [];
  return (
    <section className="panel">
      <div className="section-heading">
        <h2>已保存的事件集</h2>
        <WindowPicker value={sessions} onChange={setN} defaults={defaults} />
      </div>
      <ErrorNotice error={sets.error ?? analyze.error ?? remove.error} />
      {sets.isLoading && <Loading />}
      {!sets.isLoading && !items.length && (
        <p className="context-baseline">还没有保存的事件集。粘贴 JSON 并开始分析后会出现在这里，长期保存，可随时删除。</p>
      )}
      <div className="table-scroll">
        <table>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>
                  <button className="text-link" onClick={() => setOpen(open === item.id ? null : item.id)}>
                    {item.title}
                  </button>
                  <br />
                  <small>
                    {item.tickers.join("、")} · {item.event_count} 条 · {item.first_date} — {item.last_date}
                  </small>
                </td>
                <td className="button-row">
                  <Button variant="primary" busy={analyze.isPending && analyze.variables === item.id} onClick={() => analyze.mutate(item.id)}>
                    分析（n={sessions}）
                  </Button>
                  <Button
                    variant="ghost"
                    onClick={() => {
                      if (window.confirm(`删除「${item.title}」？已有分析结果不受影响。`)) remove.mutate(item.id);
                    }}
                  >
                    删除
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open && detail.data && (
        <>
          <EventPreview events={detail.data.events} kind={kind} />
          {detail.data.analyses.length > 0 && (
            <p className="context-baseline">
              最近分析：
              {detail.data.analyses.map((a) => (
                <button key={a.id} className="text-link" onClick={() => onAnalysis(a.id)}>
                  {formatTime(a.created_at)}（n={a.n}）
                </button>
              ))}
            </p>
          )}
          <details>
            <summary>复制为 JSON（可修改后重新粘贴）</summary>
            <textarea
              className="ev-text"
              readOnly
              rows={6}
              value={JSON.stringify({ events: detail.data.events.map(({ reaction_date: _r, ...rest }) => Object.fromEntries(Object.entries(rest).filter(([, v]) => v != null))) })}
            />
          </details>
        </>
      )}
    </section>
  );
}

function Candles({ row }: { row: Row }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const chart = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.setOption({
      animation: false,
      grid: { left: 48, right: 12, top: 12, bottom: 40 },
      tooltip: { trigger: "axis" },
      xAxis: {
        type: "category",
        data: row.candles.map((c) => `${c.offset === 0 ? "R" : c.offset > 0 ? `R+${c.offset}` : `R${c.offset}`}\n${c.date.slice(5)}`),
      },
      yAxis: { type: "value", scale: true },
      series: [{
        type: "candlestick",
        // Blue up, orange down (never red/green).
        itemStyle: { color: "#1d4ed8", color0: "#c2410c", borderColor: "#1d4ed8", borderColor0: "#c2410c" },
        data: row.candles.map((c) => (c.open == null ? "-" : [Number(c.open), Number(c.close), Number(c.low), Number(c.high)])),
      }],
    });
    const resize = () => chart.resize();
    addEventListener("resize", resize);
    return () => {
      removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [row]);
  return <div ref={el} className="ev-candles" role="img" aria-label={`${row.name} R−n 到 R+n 日 K 线`} />;
}

function SummaryTable({ summary, n }: { summary: TickerResult["summary"]; n: number }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>窗口</th>
            <th>N</th>
            <th>中位</th>
            <th>上涨比例</th>
            <th>平均</th>
            <th>中间 50%</th>
            <th>最差 / 最好</th>
          </tr>
        </thead>
        <tbody>
          {windowRows(n).map(([key, label, formula]) => {
            const s = summary[key];
            return (
              <tr key={key}>
                <td title={formula}>{label}</td>
                <td>{s?.n ?? 0}</td>
                <td><Change value={s?.median} /></td>
                <td>{ratio(s)}</td>
                <td><Change value={s?.mean} /></td>
                <td>
                  <Change value={s?.q25} /> ~ <Change value={s?.q75} />
                </td>
                <td>
                  <Change value={s?.min} /> / <Change value={s?.max} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TickerSection({ result }: { result: TickerResult }) {
  const [open, setOpen] = useState<number | null>(null);
  const n = result.n;
  return (
    <>
      <SummaryTable summary={result.summary} n={n} />
      {result.quarters && result.quarters.length > 0 && (
        <>
          <h3>按财季</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>财季</th>
                  {windowRows(n).filter(([k]) => k !== "gap").map(([key, label]) => (
                    <th key={key}>{label}：中位 · 上涨/N</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.quarters.map((q) => (
                  <tr key={q.fiscal_quarter}>
                    <td>Q{q.fiscal_quarter}</td>
                    {(["before", "reaction", "after"] as const).map((key) => (
                      <td key={key}>
                        <Change value={q.summary[key]?.median} /> · {ratio(q.summary[key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <h3>逐条事件</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>日期 · 时段</th>
              <th>名称</th>
              <th>反应日 R</th>
              <th>事前 {n} 日</th>
              <th>反应日</th>
              <th>跳空</th>
              <th>事后 {n} 日</th>
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <Fragment key={i}>
                <tr className={open === i ? "selected-row" : ""}>
                  <td>
                    {row.date} · {sessionLabels[row.session]}
                  </td>
                  <td>
                    <button className="text-link" onClick={() => setOpen(open === i ? null : i)} title="查看 K 线">
                      {row.name}
                    </button>
                    {row.fiscal_year ? <small> FY{row.fiscal_year} Q{row.fiscal_quarter}</small> : null}
                    {row.notes.map((note) => (
                      <small key={note} className="ev-note">{noteLabels[note] ?? note}</small>
                    ))}
                  </td>
                  <td>{row.reaction_date}</td>
                  {(["before", "reaction", "gap", "after"] as const).map((key) => {
                    const w = row.windows[key];
                    return (
                      <td key={key} title={`${w.start_date} → ${w.end_date}`}>
                        {w.status === "pending" ? <small>待 {w.end_date}</small> : w.status === "missing_price" ? <small>缺价格</small> : <Change value={w.value} />}
                      </td>
                    );
                  })}
                </tr>
                {open === i && (
                  <tr>
                    <td colSpan={7}>
                      <Candles row={row} />
                      {row.note && <small>{row.note}</small>}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function AnalysisView({ id, onReplace }: { id: string; onReplace: (id: string) => void }) {
  const query = useEventResearch(id);
  const client = useQueryClient();
  const checked = useRef<string | null>(null);
  const refresh = useMutation({
    mutationFn: (force: boolean) => api<Analysis>(`/events/analyses/${id}/refresh?force=${force}`, "POST"),
    onSuccess: (data) => {
      client.setQueryData(researchKeys.events(data.id, ""), data);
      if (data.id !== id) onReplace(data.id);
    },
  });
  const data = query.data as Analysis | undefined;
  // Expired prices (24 h) are fetched again automatically, once per opened research.
  useEffect(() => {
    if (data?.freshness?.expired && checked.current !== id) {
      checked.current = id;
      refresh.mutate(false);
    }
  }, [data?.freshness?.expired, id, refresh]);
  const [ticker, setTicker] = useState<string | null>(null);
  if (query.isLoading) return <Loading label="读取分析…" />;
  if (query.error) return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  if (!data) return null;
  const selected = data.tickers.find((t) => t.symbol === ticker) ?? data.tickers[0];
  const fresh = data.freshness ?? {};
  return (
    <section className="panel analysis-results" id="event-results">
      <div className="section-heading">
        <div>
          <h2>{data.title}</h2>
          <p className="context-baseline">
            {data.event_kind === "earnings" ? "财报" : "自定义事件"} · {data.event_count} 条 · n={data.n} 个交易日 · 价格截至 {data.cutoff_date}
            {fresh.price_fetched_at ? ` · 行情获取于 ${formatTime(String(fresh.price_fetched_at))}，${formatTime(String(fresh.price_expires_at))} 后过期` : ""}
          </p>
        </div>
        <Button busy={refresh.isPending} onClick={() => refresh.mutate(true)}>
          重新获取
        </Button>
      </div>
      <ErrorNotice error={refresh.error} />
      <div className="analysis-tabs">
        {data.tickers.map((t) => (
          <a
            key={t.symbol}
            href="#event-results"
            className={t === selected ? "selected" : ""}
            onClick={(e) => {
              e.preventDefault();
              setTicker(t.symbol);
            }}
          >
            {t.symbol}
            {t.result ? "" : t.status === "PARTIAL" || t.status === "FAILED" ? " · 未完成" : " · 获取中…"}
          </a>
        ))}
      </div>
      {selected?.result ? (
        <TickerSection key={selected.symbol} result={selected.result} />
      ) : selected ? (
        selected.status === "PARTIAL" || selected.status === "FAILED" ? (
          <p className="notice notice-error">{selected.symbol}：{selected.wait_reason ?? "行情未能获取"}。可点“重新获取”。</p>
        ) : (
          <Loading label={`正在获取 ${selected.symbol} 的行情（${selected.price_start} — ${selected.price_end}）并计算…`} />
        )
      ) : null}
      <details className="result-notes">
        <summary>计算方法</summary>
        <p>
          反应日 R：盘前或盘中公布为当天（休市则顺延到下一交易日），盘后公布为下一交易日，时段未知按当天处理并标注。基准为 R
          前一交易日收盘 C(R−1)。事前 n 日 = C(R−1)/C(R−1−n) − 1；反应日 = C(R)/C(R−1) − 1；开盘跳空 = O(R)/C(R−1) − 1；事后 n 日 =
          C(R+n)/C(R) − 1。三段互不重叠。价格为仅拆股调整的日线，24 小时缓存；未到的交易日留空并写预计日期。上涨比例 = 大于 0 的样本数 / N。
        </p>
      </details>
    </section>
  );
}

export function EventsPage({ kind }: { kind: Kind }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const defaults = useQuery({
    queryKey: ["event-defaults"],
    queryFn: ({ signal }) => api<Defaults>("/events/defaults", "GET", undefined, signal),
    staleTime: Infinity,
  });
  const analysis = params.get("a") ?? "";
  const show = (id: string) => {
    setParams({ a: id });
    setTimeout(() => document.getElementById("event-results")?.scrollIntoView({ block: "start" }), 50);
  };
  const intro = useMemo(
    () =>
      kind === "earnings"
        ? "用提示词向其他 AI 询问财报首次公布的日期与时段，把返回的 JSON 粘贴回来，按日期从 Yahoo 取价，计算事前、反应日和事后的涨跌。"
        : "用提示词向其他 AI 询问任意事件的日期（如发布会、政策公布），把返回的 JSON 粘贴回来，按日期取价并统计前后涨跌。",
    [kind],
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>分析中心</h1>
          <p>{intro}</p>
        </div>
      </div>
      <AnalysisTabs kind={kind} />
      {analysis && <AnalysisView id={analysis} onReplace={(id) => navigate(`?a=${id}`, { replace: true })} />}
      <PromptPanel kind={kind} />
      <PastePanel
        kind={kind}
        defaults={defaults.data}
        onCreated={(created) => {
          void client.invalidateQueries({ queryKey: researchKeys.sets(kind) });
          if (created.analysis) show(created.analysis.analysis_id);
        }}
      />
      <SavedSets kind={kind} defaults={defaults.data} onAnalysis={show} />
    </>
  );
}
