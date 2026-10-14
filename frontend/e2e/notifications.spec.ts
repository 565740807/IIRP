import { test, expect, translated } from "./fixtures";

test("bell, notification list and opening one", async ({ page, text }) => {
  await page.goto("/notable");
  const bell = page.locator("[data-notification-bell]");
  await bell.click();
  const list = page.locator("[data-notification-list]");
  await expect(list).toBeVisible();
  await expect(list.locator("[data-notification]")).toHaveCount(3);
  await expect(list.locator("[data-notification=rule_cluster]")).toContainText("DEMO");
  await translated(page);
  // A report with several rows opens the person's page.
  await list.locator("[data-notification=trade]").filter({ hasText: "SYNTHETIC Member" }).click();
  await expect(page).toHaveURL(/\/notable\/house\/SYNTHETIC%20Member/);
  await expect(page.locator("[data-person-name]")).toContainText("SYNTHETIC Member");
  await expect(page.locator("[data-notification-list]")).toHaveCount(0);
  // Closing the list marks what it showed as read.
  await expect(bell.locator("[data-unread]")).toHaveCount(0);
  // The settings link in the list opens the notifications tab.
  await bell.click();
  await page.locator("[data-notification-list]").getByRole("button", { name: text("ui.notifications.settings"), exact: true }).click();
  await expect(page.locator("[data-settings-notifications]")).toBeVisible();
});

test("notification settings: rules, desktop and phone switches", async ({ page, context, text }, info) => {
  await page.goto("/notable");
  await page.getByRole("button", { name: text("ui.settings.open"), exact: true }).click();
  await page.getByRole("tab", { name: text("ui.settings.tab.notifications") }).click();
  const section = page.locator("[data-settings-notifications]");
  await expect(section.locator("[data-switch=main]")).toHaveAttribute("aria-checked", "true");
  await expect(section.locator("[data-switch=rules]")).toHaveAttribute("aria-checked", "true");
  await expect(section.locator("[data-notification-card=desktop]")).toContainText(text("ui.settings.notifications.desktop_note"));
  if (info.project.name === "en") {
    // Phone pushes: a topic appears, the server points at a stand-in service, and a test is queued.
    const phone = section.locator("[data-switch=phone]");
    await expect(phone).toHaveAttribute("aria-checked", "false");
    await phone.click();
    await expect(phone).toHaveAttribute("aria-checked", "true");
    await expect(section.locator("[data-ntfy-topic]")).toHaveText(/^iirp-[A-Za-z0-9]{30,}$/);
    const server = section.locator("[data-ntfy-server]");
    await server.fill("http://127.0.0.1:9/stand-in");
    await server.press("Enter");
    await expect(server).toHaveValue("http://127.0.0.1:9/stand-in");
    const queued = page.waitForResponse((response) => response.url().endsWith("/api/v1/notifications/test") && response.status() === 202);
    await section.locator("[data-send-test]").click();
    await queued;
    await expect(section.locator("[data-push-status]")).toBeVisible();
    await phone.click();
    await expect(phone).toHaveAttribute("aria-checked", "false");
  } else {
    // Desktop notifications ask the browser; granted here, the switch stays on.
    await context.grantPermissions(["notifications"]);
    const desktop = section.locator("[data-switch=desktop]");
    await desktop.click();
    await expect(desktop).toHaveAttribute("aria-checked", "true");
  }
  await translated(page);
});
