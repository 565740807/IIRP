import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import ts from "typescript";

// Execute the production Feed handlers/effects. React rendering, HTTP and layout
// are boundary doubles; neither retry nor merge behavior is copied into tests.
function harness({ engaged = false, restoredReading = null } = {}) {
  const slots = [];
  const effects = [];
  const timers = [];
  const calls = [];
  const cache = new Map();
  let index = 0,
    dirty = true,
    tree,
    mounted = true;
  let params = new URLSearchParams("session_id=reading&filter=focus");
  let clock = 0;
  const old = {
    id: "old",
    revision_id: "v1",
    transaction_dates: ["2026-09-01"],
  };
  const reading = restoredReading ?? {
    schema: 3,
    cards: { old: { open: true, page: 2, cursors: ["", "a", "b"] } },
    cursors: [""],
    stream: {
      groups: [old],
      latestSession: "reading",
      nextCursor: "page2",
      pages: 1,
      total: 1,
    },
  };
  const storage = new Map([["iirp.feed.reading", JSON.stringify(reading)]]);
  class Element {}
  const card = Object.assign(new Element(), {
    dataset: { groupId: "old" },
    hasAttribute: () => engaged,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: clock, bottom: clock + 200 }),
  });
  const panel = {
    querySelectorAll: () => [card],
    querySelector: () => (engaged ? card : null),
    contains: () => false,
  };
  const scrolls = [];
  const window = {
    scrollY: 0,
    getSelection: () => "",
    // Record effective movement, including error-strip appearance/disappearance.
    scrollBy: (_, delta) => { if (delta) scrolls.push(delta); },
  };
  const document = { hidden: false, activeElement: null, body: {} };
  const useSlot = (init) => {
    const n = index++;
    if (!(n in slots)) slots[n] = init();
    return [slots[n], n];
  };
  const react = {
    useState: (init) => {
      const [value, n] = useSlot(() =>
        typeof init === "function" ? init() : init,
      );
      return [
        value,
        (next) => {
          const value = typeof next === "function" ? next(slots[n]) : next;
          if (!Object.is(slots[n], value)) {
            slots[n] = value;
            dirty = true;
          }
        },
      ];
    },
    useRef: (value) => useSlot(() => ({ current: value }))[0],
    useEffect: (run, deps) => {
      const [previous, n] = useSlot(() => null);
      if (
        !previous ||
        !deps ||
        deps.some((value, i) => !Object.is(value, previous.deps[i]))
      ) {
        slots[n] = { deps, cleanup: previous?.cleanup };
        effects.push(() => {
          slots[n].cleanup?.();
          slots[n].cleanup = run();
        });
      }
    },
  };
  react.useLayoutEffect = react.useEffect;
  const jsx = (type, props) => ({ type, props });
  const ui = Object.fromEntries(
    ["Button", "ErrorNotice", "Loading", "EmptyState"].map((name) => [
      name,
      name,
    ]),
  );
  const query = (key) => {
    const id = JSON.stringify(key);
    if (!cache.has(id))
      cache.set(id, {
        data:
          key[0] === "feed-updates"
            ? { version: "same-version", new_count: 1 }
            : undefined,
        error: null,
        refetch: () => {
          calls.push({ summary: true });
        },
        isPending: false,
      });
    return cache.get(id);
  };
  const client = {
    setQueryData: (key, data) => {
      query(key).data = data;
      dirty = true;
    },
    fetchQuery: ({ queryFn }) => queryFn(),
  };
  const modules = {
    react,
    "react/jsx-runtime": { jsx, jsxs: jsx },
    "../researchStorage": {
      useContextSearchParams: () => [
        params,
        (next) => {
          params = next(params);
          dirty = true;
        },
      ],
      useReadingState: () => [],
      sourceContext: () => ({}),
    },
    "@tanstack/react-query": {
      useQueryClient: () => client,
      useQuery: ({ queryKey }) => query(queryKey),
    },
    "react-router-dom": { Link: "Link", useLocation: () => ({}) },
    "../api": {
      api: (path) =>
        new Promise((resolve, reject) => calls.push({ path, resolve, reject })),
      requestId: () => "epoch",
      rows: (value) => value ?? [],
      facts: (value) => value ?? {},
      display: String,
      formatNumber: String,
    },
    "./ui": ui,
    "./Timestamp": { Timestamp: "Timestamp" },
  };
  function load(relative) {
    const source = readFileSync(new URL(relative, import.meta.url), "utf8");
    const compiled = ts.transpileModule(source, {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    const exports = {};
    new Function(
      "exports",
      "require",
      "window",
      "document",
      "HTMLElement",
      "sessionStorage",
      "setTimeout",
      compiled,
    )(
      exports,
      (name) => {
        if (!(name in modules))
          modules[name] = load(`../src/${name.split("/").at(-1)}.ts`);
        return modules[name];
      },
      window,
      document,
      Element,
      {
        getItem: (key) => storage.get(key),
        setItem: (key, value) => storage.set(key, value),
      },
      (callback, delay) => {
        timers.push({ callback, delay });
      },
    );
    return exports;
  }
  const { Feed } = load("../src/components/Feed.tsx");
  function render() {
    index = 0;
    dirty = false;
    tree = Feed();
    tree.props.ref.current = panel;
    clock =
      findAll(
        (node) =>
          typeof node.type === "function" && node.type.name === "FeedCard",
      ).length * 100 +
      findAll((node) => node.type === "ErrorNotice" && node.props.error).length * 64;
    for (const run of effects.splice(0)) run();
  }
  function findAll(predicate, node = tree) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node))
      return node.flatMap((child) => findAll(predicate, child ?? null));
    return [
      ...(predicate(node) ? [node] : []),
      ...findAll(predicate, node.props?.children ?? null),
    ];
  }
  async function flush() {
    for (let n = 0; n < 12; n++) {
      if (dirty && mounted) render();
      await setImmediate();
    }
  }
  return {
    calls,
    timers,
    storage,
    scrolls,
    query,
    flush,
    find: (predicate) => findAll(predicate)[0],
    get groups() {
      return findAll(
        (node) =>
          typeof node.type === "function" && node.type.name === "FeedCard",
      ).map((node) => node.props.group);
    },
    merge() {
      this.find(
        (node) =>
          node.type === "Button" &&
          (String(node.props.children).includes("条新内容") ||
            node.props.children === "继续合并更新"),
      ).props.onClick();
    },
    retry() {
      this.find((node) => node.type === "ErrorNotice").props.retry();
    },
    get error() {
      return this.find((node) => node.type === "ErrorNotice").props.error;
    },
    async fireTimer() {
      assert(timers.length, "bounded retry must schedule another attempt");
      timers.shift().callback();
      await flush();
    },
    engage() {
      engaged = true;
    },
    rerender() {
      dirty = true;
    },
    navigate() {
      params = new URLSearchParams("session_id=other&filter=sell");
      dirty = true;
    },
    unmount() {
      for (const slot of slots) slot?.cleanup?.();
      mounted = false;
    },
  };
}
const busy = () =>
  Object.assign(new Error("阅读服务繁忙，请稍后重试"), { status: 503 });
const success = {
  target_session_id: "new",
  version: "same-version",
  new_count: 1,
  next_cursor: null,
  groups: [{ id: "new", revision_id: "v2", transaction_dates: ["2026-09-02"] }],
  removed_ids: [],
};

test("GC busy exhaustion keeps the same version retryable from the error action", async () => {
  const h = harness();
  await h.flush();
  assert.equal(h.calls.length, 1);
  for (let n = 0; n < 3; n++) {
    h.calls[n].reject(busy());
    await h.flush();
    if (n < 2) await h.fireTimer();
  }
  assert.equal(h.calls.length, 3);
  assert.equal(h.timers.length, 0);
  assert.equal(h.error.status, 503);
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["old"],
  );
  // Re-render/poll of the same advertised version cannot restart an exhausted loop.
  h.query(["feed-updates", "reading"]).data = {
    version: "same-version",
    new_count: 1,
  };
  h.rerender();
  await h.flush();
  assert.equal(h.calls.length, 3);
  h.retry();
  h.retry();
  await h.flush();
  assert.equal(
    h.calls.length,
    4,
    "repeated manual clicks share the active merge",
  );
  assert.match(
    h.calls[3].path,
    /include_groups=true/,
    "error retry must execute the merge, not just the summary GET",
  );
  h.calls[3].resolve(success);
  await h.flush();
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["new", "old"],
  );
  assert.equal(h.error, null);
  assert.deepEqual(JSON.parse(h.storage.get("iirp.feed.reading")).cards.old, {
    open: true,
    page: 2,
    cursors: ["", "a", "b"],
  });
});

test("manual retry previously refreshed only the update summary", async () => {
  const h = harness({ engaged: true });
  await h.flush();
  h.merge();
  await h.flush();
  h.calls[0].reject(Object.assign(new Error("failed"), { status: 409 }));
  await h.flush();
  assert.equal(
    h.timers.length,
    0,
    "integrity errors are not transient GC contention",
  );
  h.retry();
  await h.flush();
  assert.match(h.calls[1].path ?? "summary-only", /include_groups=true/);
  h.calls[1].resolve(success);
  await h.flush();
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["new", "old"],
  );
  assert.deepEqual(
    h.scrolls,
    [64, -64, 100],
    "error appearance, retry dismissal and merge each retain the viewport anchor",
  );
});

test("one transient GC failure merges successfully within the bounded cycle", async () => {
  const h = harness();
  await h.flush();
  h.calls[0].reject(busy());
  await h.flush();
  assert.equal(h.timers[0].delay, 250);
  await h.fireTimer();
  h.calls[1].resolve(success);
  await h.flush();
  assert.equal(h.calls.length, 2);
  assert.equal(h.error, null);
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["new", "old"],
  );
});

for (const boundary of ["navigate", "unmount"]) {
  test(`${boundary} fences a delayed success and cancels a scheduled retry`, async () => {
    const late = harness();
    await late.flush();
    late[boundary]();
    await late.flush();
    late.calls[0].resolve(success);
    await late.flush();
    assert.deepEqual(
      JSON.parse(late.storage.get("iirp.feed.reading")).stream.groups.map(
        (g) => g.id,
      ),
      ["old"],
    );
    const waiting = harness();
    await waiting.flush();
    waiting.calls[0].reject(busy());
    await waiting.flush();
    waiting[boundary]();
    await waiting.flush();
    await waiting.fireTimer();
    assert.equal(
      waiting.calls.filter(
        (call) =>
          call.path?.includes("session_id=reading") &&
          call.path.includes("include_groups"),
      ).length,
      1,
    );
  });
}

test("reading started during backoff still retains the viewport anchor", async () => {
  const h = harness();
  await h.flush();
  h.calls[0].reject(busy());
  await h.flush();
  h.engage();
  await h.fireTimer();
  h.calls[1].resolve(success);
  await h.flush();
  assert.deepEqual(h.scrolls, [100]);
  assert.equal(
    JSON.parse(h.storage.get("iirp.feed.reading")).cards.old.open,
    true,
  );
});

test("error retry preserves a failed pagination operation", async () => {
  const h = harness({ engaged: true });
  await h.flush();
  h.find(
    (node) => node.type === "Button" && node.props.children === "加载更多",
  ).props.onClick();
  await h.flush();
  h.calls[0].reject(busy());
  await h.flush();
  h.retry();
  await h.flush();
  assert.match(h.calls[1].path, /cursor=page2/);
  assert.doesNotMatch(h.calls[1].path, /include_groups/);
  h.calls[1].resolve({ groups: success.groups, next_cursor: null });
  await h.flush();
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["old", "new"],
  );
  assert.equal(h.error, null);
});

for (const status of [409, 410, undefined]) {
  test(`status ${status} is exposed without a blind automatic retry`, async () => {
    const h = harness();
    await h.flush();
    h.calls[0].reject(Object.assign(new Error("read failed"), { status }));
    await h.flush();
    assert.equal(h.calls.length, 1);
    assert.equal(h.timers.length, 0);
    assert.equal(h.error.message, "read failed");
    assert.deepEqual(
      h.groups.map((g) => g.id),
      ["old"],
    );
  });
}

test("navigation also fences a late failure from the previous reading", async () => {
  const h = harness();
  await h.flush();
  h.navigate();
  await h.flush();
  h.calls[0].reject(busy());
  await h.flush();
  assert.equal(h.error, null);
  assert.equal(h.timers.length, 0);
});

test("a later delta page retries the same frozen target and cursor", async () => {
  const h = harness();
  await h.flush();
  h.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
  await h.flush();
  assert.deepEqual(
    h.groups.map((g) => g.id),
    ["new", "old"],
  );
  assert.match(h.calls[1].path, /target_session_id=new/);
  assert.match(h.calls[1].path, /cursor=delta-page2/);
  h.calls[1].reject(busy());
  await h.flush();
  await h.fireTimer();
  assert.equal(h.calls[2].path, h.calls[1].path);
  h.calls[2].resolve({ ...success, groups: [] });
  await h.flush();
  assert.equal(h.error, null);
  assert.equal(
    JSON.parse(h.storage.get("iirp.feed.reading")).stream.latestSession,
    "new",
  );
});

test("expanded detail preparation is also fenced on navigation", async () => {
  const h = harness({ engaged: true });
  await h.flush();
  h.merge();
  await h.flush();
  h.calls[0].resolve({
    ...success,
    groups: [{ ...success.groups[0], id: "old" }],
  });
  await h.flush();
  assert.match(h.calls[1].path, /feed\/groups\/old/);
  assert.match(h.calls[1].path, /cursor=b/);
  h.navigate();
  await h.flush();
  h.calls[1].resolve({});
  await h.flush();
  assert.equal(
    JSON.parse(h.storage.get("iirp.feed.reading")).stream.groups[0].revision_id,
    "v1",
  );
});

for (const reload of [false, true]) {
  test(`partial merge resumes its frozen target before newer removals (reload=${reload})`, async () => {
    let h = harness();
    await h.flush();
    h.calls[0].resolve({ ...success, next_cursor: "delta-page2", new_count: 2 });
    await h.flush();
    for (let n = 1; n <= 3; n++) {
      h.calls[n].reject(busy());
      await h.flush();
      if (n < 3) await h.fireTimer();
    }
    assert.equal(h.calls.length, 4);
    assert.equal(h.timers.length, 0);
    const saved = JSON.parse(h.storage.get("iirp.feed.reading"));
    assert.deepEqual(h.groups.map((g) => g.id), ["new", "old"]);
    assert.equal(saved.stream.latestSession, "reading");
    // The first-page group exits the filter now. A fresh reading -> newest
    // delta cannot report its removal, since it was absent from reading.
    if (reload) {
      h.unmount();
      h = harness({ restoredReading: saved });
      await h.flush();
    } else {
      h.retry();
      h.retry();
      await h.flush();
      assert.equal(h.calls.length, 5);
    }
    const resume = h.calls.at(-1);
    const p = new URL(resume.path, "http://test").searchParams;
    assert.equal(p.get("session_id"), "reading");
    assert.equal(p.get("target_session_id"), "new", "retry must not create a different target");
    assert.equal(p.get("cursor"), "delta-page2");
    resume.resolve({ ...success, new_count: 2, groups: [{ ...success.groups[0], id: "tail" }] });
    await h.flush();
    assert.equal(JSON.parse(h.storage.get("iirp.feed.reading")).stream.latestSession, "new");
    assert.equal(h.error, null);
    // A subsequent summary compares the fully applied frozen target with
    // the new version, so the previously added group participates in removals.
    h.query(["feed-updates", "new"]).data = { version: "v3", new_count: 1 };
    h.rerender();
    await h.flush();
    const removal = h.calls.at(-1);
    assert.equal(new URL(removal.path, "http://test").searchParams.get("session_id"), "new");
    removal.resolve({ ...success, target_session_id: "newest", version: "v3", groups: [], removed_ids: ["new"] });
    await h.flush();
    const retained = h.groups.find((g) => g.id === "new");
    assert.equal(retained._removed, true);
    assert.equal(retained.revision_id, "v2");
    assert.equal(retained._session_id, "new");
    const finished = JSON.parse(h.storage.get("iirp.feed.reading"));
    assert.deepEqual(finished.cards, saved.cards);
    assert.equal(finished.stream.latestSession, "newest");
    assert.equal(finished.stream.pendingMerge, undefined);
    assert.equal(h.query(["feed-updates", "newest"]).data.new_count, 0);
  });
}

test("restored partial reading offers continuation even when the latest summary has no delta", async () => {
  const original = harness();
  await original.flush();
  original.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
  await original.flush();
  const saved = JSON.parse(original.storage.get("iirp.feed.reading"));
  original.unmount();
  for (const engaged of [false, true]) {
    const h = harness({ engaged, restoredReading: saved });
    h.query(["feed-updates", "reading"]).data = { version: "reverted", new_count: 0 };
    await h.flush();
    if (engaged) {
      assert.equal(h.calls.length, 0, "restoration must not interrupt an expanded card");
      assert(h.find((node) => node.props.children === "继续合并更新"));
      h.merge();
      await h.flush();
    }
    assert.equal(h.calls.length, 1);
    assert.match(h.calls[0].path, /target_session_id=new/);
    assert.match(h.calls[0].path, /cursor=delta-page2/);
    // An expired/damaged frozen target must remain an error, never silently
    // create a latest snapshot or erase already displayed intermediate groups.
    h.calls[0].reject(Object.assign(new Error("阅读会话已过期"), { status: 409 }));
    await h.flush();
    assert.equal(h.error.status, 409);
    assert.equal(h.calls.length, 1);
    assert.equal(h.timers.length, 0);
    assert.deepEqual(JSON.parse(h.storage.get("iirp.feed.reading")), saved);
    h.retry();
    await h.flush();
    assert.equal(h.calls[1].path, h.calls[0].path);
    h.unmount();
  }
});

for (const boundary of ["navigate", "unmount"]) {
  test(`partial page completion after ${boundary} cannot advance saved recovery`, async () => {
    const h = harness();
    await h.flush();
    h.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
    await h.flush();
    const saved = h.storage.get("iirp.feed.reading");
    h[boundary]();
    await h.flush();
    h.calls[1].resolve({ ...success, groups: [{ ...success.groups[0], id: "late" }] });
    await h.flush();
    assert.equal(h.storage.get("iirp.feed.reading"), saved);
  });
}

test("failed expanded-detail preparation repeats the uncommitted page of the frozen delta", async () => {
  const h = harness({ engaged: true });
  await h.flush();
  h.merge();
  await h.flush();
  h.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
  await h.flush();
  const saved = h.storage.get("iirp.feed.reading");
  const next = { ...success, groups: [{ ...success.groups[0], id: "old" }] };
  h.calls[1].resolve(next);
  await h.flush();
  assert.match(h.calls[2].path, /feed\/groups\/old/);
  h.calls[2].reject(busy());
  await h.flush();
  assert.equal(h.storage.get("iirp.feed.reading"), saved);
  h.retry();
  await h.flush();
  assert.equal(h.calls[3].path, h.calls[1].path);
  h.calls[3].resolve(next);
  await h.flush();
  h.calls[4].resolve({});
  await h.flush();
  assert.equal(h.error, null);
  const finished = JSON.parse(h.storage.get("iirp.feed.reading"));
  assert.equal(finished.stream.latestSession, "new");
  assert.equal(finished.stream.pendingMerge, undefined);
  assert.equal(finished.cards.old.open, true);
});

test("old mixed-version caches without a checkpoint require an explicit new reading", async () => {
  const original = harness();
  await original.flush();
  original.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
  await original.flush();
  const saved = JSON.parse(original.storage.get("iirp.feed.reading"));
  saved.schema = 3;
  delete saved.stream.pendingMerge;
  original.unmount();
  const h = harness({ restoredReading: saved });
  await h.flush();
  assert.equal(h.calls.length, 0, "an ambiguous legacy baseline cannot safely merge automatically");
  assert.deepEqual(h.groups, saved.stream.groups);
  assert(h.find((node) => String(node.props.children).includes("无法核对旧版阅读缓存")));
  const restart = h.find((node) => node.type === "Button" && node.props.children === "开始新阅读");
  assert(restart, "the reader must explicitly choose a new snapshot");
  assert.deepEqual(JSON.parse(h.storage.get("iirp.feed.reading")), saved);
  restart.props.onClick();
  await h.flush();
  assert.deepEqual(JSON.parse(h.storage.get("iirp.feed.reading")), saved, "the old reading remains saved");
});

test("older pagination between partial failure and resume preserves both checkpoints", async () => {
  const h = harness({ engaged: true });
  await h.flush();
  h.merge();
  await h.flush();
  h.calls[0].resolve({ ...success, next_cursor: "delta-page2", removed_ids: ["old"] });
  await h.flush();
  h.calls[1].reject(Object.assign(new Error("offline"), { status: undefined }));
  await h.flush();
  h.find((node) => node.type === "Button" && node.props.children === "加载更多").props.onClick();
  await h.flush();
  h.calls[2].resolve({ groups: [{ id: "earlier" }, { id: "old", revision_id: "v0" }], next_cursor: "page3" });
  await h.flush();
  h.merge();
  await h.flush();
  assert.equal(h.calls[3].path, h.calls[1].path);
  h.calls[3].resolve({ ...success, groups: [] });
  await h.flush();
  const finished = JSON.parse(h.storage.get("iirp.feed.reading"));
  assert.equal(finished.stream.nextCursor, "page3");
  assert.equal(finished.stream.pages, 2);
  assert.equal(finished.stream.latestSession, "new");
  assert.equal(finished.stream.pendingMerge, undefined);
  assert(h.groups.some((g) => g.id === "earlier"));
  assert.equal(h.groups.find((g) => g.id === "old")._removed, true);
  assert.equal(h.groups.find((g) => g.id === "old").revision_id, "v1");
  assert.equal(finished.cards.old.open, true);
});

test("a newer announcement that resumes an older delta stays eligible for its own automatic merge", async () => {
  const h = harness();
  await h.flush();
  h.calls[0].resolve({ ...success, next_cursor: "delta-page2" });
  await h.flush();
  for (let n = 1; n <= 3; n++) {
    h.calls[n].reject(busy());
    await h.flush();
    if (n < 3) await h.fireTimer();
  }
  assert.equal(h.calls.length, 4);
  h.query(["feed-updates", "reading"]).data = { version: "newest", new_count: 1 };
  h.rerender();
  await h.flush();
  assert.equal(h.calls.length, 5);
  assert.match(h.calls[4].path, /target_session_id=new/);
  h.calls[4].resolve({ ...success, groups: [] });
  await h.flush();
  h.query(["feed-updates", "new"]).data = { version: "newest", new_count: 1 };
  h.rerender();
  await h.flush();
  assert.equal(h.calls.length, 6, "resuming the old target must not consume the newer version's merge attempt");
  assert.equal(new URL(h.calls[5].path, "http://test").searchParams.get("session_id"), "new");
  h.calls[5].resolve({ ...success, target_session_id: "newest", version: "newest", groups: [], removed_ids: ["new"] });
  await h.flush();
  assert.equal(h.groups.find((g) => g.id === "new")._removed, true);
  assert.equal(h.error, null);
  assert.equal(h.calls.length, 6);
});
