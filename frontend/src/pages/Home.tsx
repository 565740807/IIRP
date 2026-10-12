import { InsiderStream } from "../components/home/InsiderStream";
import { MarketStrip } from "../components/home/MarketStrip";
import { SectorBasis, SectorStrip } from "../components/home/SectorStrip";
import { SecContactBanner } from "../components/shell/SecContact";

/** SEC contact notice when needed, the index and sector strips with one status line, then the live Insider waterfall. */
export function HomePage() {
  return (
    <>
      <SecContactBanner />
      <MarketStrip basis={<SectorBasis />}>
        <SectorStrip />
      </MarketStrip>
      <InsiderStream />
    </>
  );
}
