import assert from "node:assert/strict";
import { test } from "node:test";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

// One bundle, so the marks and the server renderer share a single React.
const { outputFiles } = buildSync({
  stdin: {
    contents: 'export * from "@/lib/tickerMarks"; export { createElement } from "react"; export { renderToStaticMarkup } from "react-dom/server";',
    resolveDir: fileURLToPath(new URL("../src", import.meta.url)), loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "esm",
  tsconfig: fileURLToPath(new URL("../tsconfig.json", import.meta.url)),
  // The server renderer requires Node built-ins at run time.
  banner: { js: `import { createRequire } from "node:module"; const require = createRequire(${JSON.stringify(fileURLToPath(import.meta.url))});` },
});
const marks = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
const { createElement: h, renderToStaticMarkup: render, TickerMarks, TickerBatch, TickerMarkSources } = marks;

test("codes compare like the backend: upper case, dot read as dash", () => {
  assert.equal(marks.tickerKey(" brk.b "), "BRK-B");
  assert.equal(marks.tickerKey(null), "");
  assert.deepEqual(marks.tickerBatches(["msft", "BRK.B", "brk-b", null, "", "AAPL"]), [["AAPL", "BRK-B", "MSFT"]]);
  const many = Array.from({ length: 450 }, (_, index) => `T${String(index).padStart(3, "0")}`);
  assert.deepEqual(marks.tickerBatches(many).map((batch) => batch.length), [200, 200, 50]);
});

test("without an optional feature nothing is rendered, not even an empty box", () => {
  const html = render(h(TickerBatch, { tickers: ["AAPL"] }, h(TickerMarks, { ticker: "AAPL" })));
  assert.equal(html, "");
  assert.equal(render(h(TickerMarks, { ticker: "AAPL" })), "");
});

test("an installed mark gets the code and the whole batch of its table", () => {
  const seen = [];
  const Mark = ({ ticker, batch }) => { seen.push([ticker, batch.join(",")]); return h("i", null, ticker); };
  const html = render(h(TickerMarkSources.Provider, { value: [Mark] },
    h(TickerBatch, { tickers: ["msft", "BRK.B", "AAPL"] }, h(TickerMarks, { ticker: "brk.b" }), h(TickerMarks, { ticker: "MSFT" })),
    h(TickerMarks, { ticker: "TSLA" }),
    h(TickerMarks, { ticker: null })));
  assert.match(html, /data-ticker-marks/);
  assert.equal((html.match(/data-ticker-marks/g) ?? []).length, 3);
  assert.deepEqual(seen, [["BRK-B", "AAPL,BRK-B,MSFT"], ["MSFT", "AAPL,BRK-B,MSFT"], ["TSLA", "TSLA"]]);
});
