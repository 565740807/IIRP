import { InsiderStream } from "../components/home/InsiderStream";
import { MarketStrip } from "../components/home/MarketStrip";
import { SectorBasis, SectorStrip } from "../components/home/SectorStrip";
import { SecContactBanner } from "../components/shell/SecContact";
import { WatchlistNews } from "../components/watchlist/WatchlistNews";

/**
 * SEC contact notice when needed, the index and sector strips with one status line,
 * a line of watchlist news when there is any, then the live Insider waterfall.
 */
export function HomePage() {
  return (
    <>
      <SecContactBanner />
      <MarketStrip basis={<SectorBasis />}>
        <SectorStrip />
      </MarketStrip>
      <WatchlistNews />
      <InsiderStream />
    </>
  );
}
