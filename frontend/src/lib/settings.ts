export const SETTINGS_TABS = ["display", "watchlist", "notable", "notifications", "long_prices"] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number];
export const OPEN_SETTINGS = "iirp:open-settings";

/** Open the settings dialog at one tab from anywhere on a page. */
export function openSettings(tab: SettingsTab) {
  window.dispatchEvent(new CustomEvent<SettingsTab>(OPEN_SETTINGS, { detail: tab }));
}
