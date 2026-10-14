import { test, expect, sample, translated, homeRows } from "./fixtures";

// The full route and interaction suite runs in both languages and all three clocks.
test("sidebar pages and analysis tabs", async ({ page, text }) => {
  await page.goto("/");
  await homeRows(page);
  await translated(page);
  const links = await page.locator("aside nav a").evaluateAll(nodes => nodes.map(node => ({ href: node.getAttribute("href")!, label: (node as HTMLElement).innerText.trim() })));
  for (const { href, label } of links) {
    await page.locator(`aside nav a[href="${href}"]`).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe(new URL(href, "http://localhost").pathname);
    const key = href === "/" ? "home" : href.startsWith("/analysis") ? "analysis" : href.split("/")[1];
    await expect(page.locator("body > #root header h1").first()).toHaveText(text(`ui.page.${key}`) || label);
    if (href === "/") await homeRows(page);
    else if (href === "/insiders" || href === "/sectors")
      await expect(page.locator("main table:visible").first()).toBeVisible();
    else if (href === "/notable")
      await expect(page.locator("[data-notable-table]")).toBeVisible();
    else if (href.startsWith("/analysis"))
      await expect(page.locator("main input").first()).toBeVisible();
    else if (href === "/data") {
      await expect(page.getByText(text("ui.data.tasks"), { exact: true }).first()).toBeVisible();
      await expect(page.locator("#political-sources-heading")).toBeVisible();
      for (const source of ["house", "senate", "oge"])
        await expect(page.getByText(text(`ui.data.political.source.${source}`), { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: text("ui.data.political.check_now"), exact: true })).toHaveCount(3);
      await page.getByRole("button", { name: text("ui.data.category.history"), exact: true }).click();
      await expect(page.locator("main table:visible").first()).toBeVisible();
    }
    await translated(page);
  }
  for (const kind of ["monthly", "interval", "earnings", "events"]) {
    await page.goto(`/analysis/${kind}`);
    await expect(page.locator("body > #root header h1").first()).toHaveText(text("ui.page.analysis"));
    await expect(page.locator("main input:visible, main textarea:visible").first()).toBeVisible();
    await translated(page);
  }
});

test("company, person, transaction and market details", async ({ page, text }) => {
  for (const [href, key] of [
    ["/companies/0000000123", "company"], ["/people/0000000456", "person"],
    [`/transactions/${sample.transaction}`, "transaction"], ["/market/%5EGSPC", "market"],
  ]) {
    await page.goto(href);
    await expect(page.locator("body > #root header h1").first()).toHaveText(text(`ui.page.${key}`));
    await expect(page.locator("main canvas:visible").first()).toBeVisible();
    if (key === "company" || key === "person") await expect(page.locator("main table:visible").first()).toBeVisible();
    await translated(page);
  }
});

test("completed monthly and event results", async ({ page, text }) => {
  for (const [kind, id] of [["monthly", sample.monthly], ["events", sample.event_dates]]) {
    await page.goto(`/analysis/${kind}?${kind === "monthly" ? "id" : "a"}=${id}`);
    await expect(page.locator("body > #root header h1").first()).toHaveText(text("ui.page.analysis"));
    await expect(page.locator("main table:visible").first()).toBeVisible();
    await expect(page.locator("main canvas:visible").first()).toBeVisible();
    await expect(page.locator("[data-page-contents]")).toBeVisible();
    await translated(page);
  }
});

test("dialogs, columns and URL preferences persist", async ({ page, text }) => {
  await page.goto("/");
  await homeRows(page);
  await page.getByRole("button", { name: text("ui.settings.open"), exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: text("ui.nav.system"), exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.locator("[data-column-settings]").first().click();
  const menu = page.locator("[data-column-menu]");
  const hidden = menu.getByRole("checkbox", { checked: false }).first();
  const label = await hidden.locator("..").innerText();
  await hidden.check();
  await page.keyboard.press("Escape");
  await page.reload();
  await homeRows(page);
  await page.locator("[data-column-settings]").first().click();
  await expect(menu.getByRole("checkbox", { name: label.trim(), exact: true })).toBeChecked();
  await page.keyboard.press("Escape");
  await page.locator('aside a[href="/insiders"]').click();
  await page.getByRole("tab", { name: new RegExp(text("ui.overview.tab.large_buys")) }).click();
  await expect(page).toHaveURL(/tab=large_buys/);
  await page.getByRole("combobox", { name: text("ui.overview.sector_label"), exact: true }).selectOption("information_technology");
  await expect(page).toHaveURL(/sector=information_technology/);
  await page.reload();
  await expect(page.getByRole("tab", { name: new RegExp(text("ui.overview.tab.large_buys")) })).toHaveAttribute("aria-selected", "true");
  await page.goto("/companies/0000000123");
  await page.getByRole("radio", { name: new RegExp(text("ui.entity.rows.all")) }).click();
  await expect(page).toHaveURL(/rows=all/);
  await page.reload();
  await expect(page.locator("main table:visible").first()).toBeVisible();
  await page.getByRole("radio", { name: new RegExp(text("ui.entity.rows.focus")) }).click();
  await expect(page).not.toHaveURL(/rows=all/);
  await page.goto(`/analysis/monthly?id=${sample.monthly}`);
  const contents = page.locator("[data-page-contents]");
  await expect(contents).toBeVisible();
  const destination = contents.getByRole("button").last();
  await destination.click();
  await expect(destination).toHaveAttribute("aria-current", "location");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
  await translated(page);
});

test("home quote states and price notes", async ({ page, text }) => {
  await page.goto("/");
  await homeRows(page);
  const status = (key: string) => text(`ui.overview.quote_status.${key}`).split("{{day}}")[0].trim();
  // The seeded SAMP quote is always due again; DEMO's is the last close only at the weekend.
  await expect(page.getByText(status("delayed")).first()).toBeVisible();
  const weekend = [0, 6].includes(new Date(sample.clock).getUTCDay());
  await expect(page.getByText(status("closed"))).toHaveCount(weekend ? 1 : 0);
  await page.locator("main [data-group-id] [data-current-price]").first().hover();
  await expect(page.locator("[data-slot=tooltip-content]").first()).toContainText(text("ui.overview.comparison_rule"));
  await translated(page);
});

test("watchlist stars, home filter and news", async ({ page, text }, info) => {
  await page.goto("/");
  await homeRows(page);
  await expect(page.getByText(text("ui.watchlist.news.title"), { exact: true })).toBeVisible();
  await page.locator("[data-watchlist-news-view]").click();
  await expect(page.locator("[data-news-line]:visible").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: new RegExp(text("ui.watchlist.filter.label")) }).click();
  await expect(page).toHaveURL(/watchlist=true/);
  await homeRows(page);
  // Parallel language projects own different watched-code writes.
  const existing = info.project.name === "en";
  const ticker = existing ? "DEMO" : "TEST";
  await page.goto(existing ? "/companies/0000000123" : "/companies/0000000125");
  const star = page.locator(`main [data-watch-star=${ticker}]`);
  await expect(star).toHaveAttribute("aria-pressed", String(existing));
  const change = async (watched: boolean) => {
    await Promise.all([page.waitForResponse(response => watched
      ? response.url().endsWith("/api/v1/watchlist") && response.request().method() === "POST"
      : response.url().endsWith(`/api/v1/watchlist/${ticker}`) && response.request().method() === "DELETE"), star.click()]);
    await expect(star).toHaveAttribute("aria-pressed", String(watched));
  };
  await change(!existing);
  await page.reload();
  await expect(star).toHaveAttribute("aria-pressed", String(!existing));
  await change(existing);
  await translated(page);
});

test("switch languages without showing message keys", async ({ page }, info) => {
  await page.goto("/");
  await homeRows(page);
  await page.evaluate(() => {
    (window as any).__untranslated = [];
    new MutationObserver(() => {
      const value = document.body.innerText.match(/\bui\.[a-z_]+(?:\.[a-z_]+)+/g);
      if (value) (window as any).__untranslated.push(...value);
    }).observe(document.body, { subtree: true, childList: true, characterData: true });
  });
  await page.getByRole("radio", { name: info.project.name === "en" ? "中文" : "English", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", info.project.name === "en" ? "zh-CN" : "en");
  await homeRows(page);
  await translated(page);
  expect(await page.evaluate(() => (window as any).__untranslated)).toEqual([]);
});

test("leaving Insider keeps the destination while its code and old results load", async ({ page, text }) => {
  let unblockCode!: () => void;
  let codeRequested!: () => void;
  const codeGate = new Promise<void>(resolve => { unblockCode = resolve; });
  const requested = new Promise<void>(resolve => { codeRequested = resolve; });
  let unblockResults!: () => void;
  let resultsRequested!: () => void;
  const resultGate = new Promise<void>(resolve => { unblockResults = resolve; });
  const oldResults = new Promise<void>(resolve => { resultsRequested = resolve; });
  await page.route(/\/assets\/Notable-[^/]+\.js$/, async route => {
    codeRequested();
    await codeGate;
    await route.continue();
  });
  await page.route("**/api/v1/watchlist", async route => {
    resultsRequested();
    await resultGate;
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), items: [] } });
  });
  try {
    await page.goto("/insiders?watchlist=true");
    await expect(page.locator("body > #root header h1").first()).toHaveText(text("ui.page.insiders"));
    await oldResults;
    await page.locator('aside nav a[href="/notable"]').click();
    await requested;
    const completedResults = page.waitForResponse(response => response.url().endsWith("/api/v1/watchlist"));
    unblockResults();
    await completedResults;
    // The late empty list changes the effective filter and reruns default normalization.
    await expect(page.locator("[data-watch-filter]")).toBeDisabled();
    await expect(page).toHaveURL(/\/notable$/);
    unblockCode();
    await expect(page.locator("body > #root header h1").first()).toHaveText(text("ui.page.notable"));
    await expect(page.locator("[data-notable-table]")).toBeVisible();
    await expect(page).toHaveURL(/\/notable$/);
  } finally {
    unblockResults();
    unblockCode();
  }
  await translated(page);
});
