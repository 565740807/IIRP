import { test, expect, translated } from "./fixtures";

const MEMBER = "/notable/house/SYNTHETIC%20Member%7CZZ01";
const withN = (value: string, n: number) => value.replace("{{n}}", String(n));

test("followed people timeline, filters in the link and reading marks", async ({ page, text }) => {
  await page.goto("/notable");
  await expect(page.locator("body > #root header h1").first()).toHaveText(text("ui.page.notable"));
  const table = page.locator("[data-notable-table]");
  await expect(table).toBeVisible();
  await expect(table.locator("[data-trade-row=sec]").first()).toBeVisible();
  await expect(table.locator("[data-trade-row=house]").first()).toBeVisible();
  // A scanned row is marked neutrally; a row with a stated doubt names it on hover.
  await expect(table.locator("[data-mark=scanned]").first()).toBeVisible();
  await table.locator("[data-mark=review]").first().hover();
  await expect(page.locator("[data-slot=tooltip-content]").first()).toContainText(text("ui.notable.reason.direction_unknown"));
  await page.locator("[data-filter=side]").getByRole("radio", { name: text("ui.notable.side.buy"), exact: true }).click();
  await expect(page).toHaveURL(/side=buy/);
  await page.locator("[data-filter=person]").selectOption("house:SYNTHETIC Member|ZZ01");
  await expect(page).toHaveURL(/person=house/);
  await page.reload();
  await expect(page.locator("[data-filter=person]")).toHaveValue("house:SYNTHETIC Member|ZZ01");
  await expect(table.locator("[data-trade-row]")).toHaveCount(2);
  await page.getByRole("button", { name: text("ui.notable.filter.clear"), exact: true }).first().click();
  await expect(page).not.toHaveURL(/person=/);
  // "All" adds a House member nobody follows.
  await page.locator("[data-filter=scope]").getByRole("radio", { name: text("ui.notable.filter.all"), exact: true }).click();
  await expect(page).toHaveURL(/scope=all/);
  await expect(table.getByRole("link", { name: "SYNTHETIC Other", exact: true }).first()).toBeVisible();
  await translated(page);
});

test("price windows, n in the link and a row opening its trade", async ({ page, text }) => {
  await page.goto("/notable");
  const table = page.locator("[data-notable-table]");
  await expect(table.locator("thead")).toContainText(withN(text("ui.notable.window_head.published"), 5));
  await page.getByRole("group", { name: text("ui.window.n_label") }).getByRole("button", { name: "10", exact: true }).click();
  await expect(page).toHaveURL(/n=10/);
  await expect(table.locator("thead")).toContainText(withN(text("ui.notable.window_head.before"), 10));
  // A government row opens its own trade page: the report's row, both bases and the original.
  await table.locator("[data-trade-row=house]").filter({ hasText: "DEMO" }).first().locator("td").nth(4).click();
  await expect(page).toHaveURL(/\/notable\/trades\/.+n=10/);
  await expect(page.locator("[data-raw-row]")).toContainText("SYNTHETIC DEMO - Common Stock");
  await expect(page.locator("[data-basis=published]")).toBeVisible();
  await expect(page.locator("[data-original-link]")).toHaveAttribute("href", /disclosures-clerk\.house\.gov/);
  await translated(page);
  await page.goBack();
  await table.locator("[data-trade-row=sec]").first().locator("td").nth(4).click();
  await expect(page).toHaveURL(/\/transactions\//);
  await translated(page);
});

test("person page, company block and following", async ({ page, text }, info) => {
  await page.goto(MEMBER);
  await expect(page.locator("[data-person-name]")).toContainText("SYNTHETIC Member");
  await expect(page.locator("[data-notable-table] [data-trade-row]")).toHaveCount(3);
  await expect(page.locator("[data-person-summary]")).toContainText(text("ui.notable.summary.not_advice"));
  await expect(page.getByText(text("ui.notable.reports_title"), { exact: true })).toBeVisible();
  await page.goto("/companies/0000000123");
  await expect(page.locator("[data-company-notable]")).toBeVisible();
  await expect(page.locator("[data-company-notable-row]").first()).toBeVisible();
  await page.goto("/people/0000000456");
  await expect(page.locator('main [data-follow-star="sec:0000000456"]')).toHaveAttribute("aria-pressed", "true");
  // Parallel language projects follow and unfollow different people that no other test reads.
  const [href, key, followed] = info.project.name === "en"
    ? ["/notable/house/SYNTHETIC%20Other%7CZZ02", "house:SYNTHETIC Other|ZZ02", false]
    : ["/notable/sec/0000315090", "sec:0000315090", true];
  await page.goto(href);
  const star = page.locator(`main [data-follow-star="${key}"]`).first();
  await expect(star).toHaveAttribute("aria-pressed", String(followed));
  const flip = async (follow: boolean) => {
    await Promise.all([page.waitForResponse(response => response.url().includes("/api/v1/notable/people")
      && response.request().method() === (follow ? "POST" : "DELETE")), star.click()]);
    await expect(star).toHaveAttribute("aria-pressed", String(follow));
  };
  await flip(!followed);
  await page.reload();
  await expect(star).toHaveAttribute("aria-pressed", String(!followed));
  await flip(followed);
  await translated(page);
});

test("settings list the followed people", async ({ page, text }) => {
  await page.goto("/notable");
  await expect(page.locator("[data-notable-table]")).toBeVisible();
  await page.getByRole("button", { name: text("ui.settings.open"), exact: true }).click();
  await page.getByRole("tab", { name: text("ui.settings.tab.notable") }).click();
  const list = page.locator("[data-notable-list]");
  await expect(list).toBeVisible();
  await expect(list.locator("[data-notable-person='oge:Donald J Trump|president']")).toBeVisible();
  await expect(page.locator("[data-restore-defaults]")).toBeVisible();
  await translated(page);
});


test("report previews, expansion and asset types persist in both pages", async ({ page, text }) => {
  for (const href of ["/notable?person=house%3ASYNTHETIC+Member%7CZZ01", MEMBER]) {
    await page.goto(href);
    const table = page.locator("[data-notable-table]");
    await expect(table.locator("[data-report-group]")).toHaveCount(1);
    await expect(table.locator("[data-trade-row]")).toHaveCount(3);
    const choice = page.locator("[data-filter=assets]");
    await expect(choice.getByRole("radio", { name: text("ui.notable.assets.equities"), exact: true })).toHaveAttribute("aria-checked", "true");
    await table.locator("[data-expand-report]").click();
    await expect(table.locator("[data-trade-row]")).toHaveCount(4);
    await expect(table).not.toContainText("SYNTHETIC municipal bond");
    const counts = await table.locator("[data-report-counts]").innerText();
    await choice.getByRole("radio", { name: text("ui.notable.assets.all"), exact: true }).click();
    await expect(page).toHaveURL(/assets=all/);
    await page.reload();
    await expect(choice.getByRole("radio", { name: text("ui.notable.assets.all"), exact: true })).toHaveAttribute("aria-checked", "true");
    await table.locator("[data-expand-report]").click();
    await expect(table.locator("[data-trade-row]")).toHaveCount(5);
    await expect(table).toContainText("SYNTHETIC municipal bond");
    await expect(table.locator("[data-report-counts]")).toHaveText(counts);
  }
  await translated(page);
});
