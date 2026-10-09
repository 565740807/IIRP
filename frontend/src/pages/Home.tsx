import { InsiderStream } from "../components/home/InsiderStream";
import { MarketStrip } from "../components/home/MarketStrip";
import { SectorStrip } from "../components/home/SectorStrip";
import { SecContactBanner } from "../components/shell/SecContact";

/** SEC contact notice when needed, the index and sector strips, then the live Insider waterfall. */
export function HomePage() {
  return (
    <>
      <SecContactBanner />
      <MarketStrip />
      <SectorStrip />
      <InsiderStream />
    </>
  );
}
