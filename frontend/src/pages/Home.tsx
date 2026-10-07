import { useTranslation } from "react-i18next";
import { CollectionButton } from "../components/CollectionControls";
import { InsiderStream } from "../components/home/InsiderStream";
import { MarketStrip } from "../components/home/MarketStrip";

/** Market strip on top, the live Insider waterfall below. */
export function HomePage() {
  return (
    <>
      <MarketStrip />
      <InsiderStream />
    </>
  );
}

/** The same waterfall with manual SEC requests; the lookup page arrives in S5b. */
export function InsiderPage() {
  const { t } = useTranslation();
  return (
    <>
      <div className="flex items-center justify-end gap-2">
        <CollectionButton kind="sec_history">{t("ui.insiders.backfill")}</CollectionButton>
        <CollectionButton kind="sec_latest" primary>{t("ui.insiders.fetch_latest")}</CollectionButton>
      </div>
      <InsiderStream />
    </>
  );
}
