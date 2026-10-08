import { InsiderStream } from "../components/home/InsiderStream";
import { MarketStrip } from "../components/home/MarketStrip";
import { SecContactBanner } from "../components/shell/SecContact";

/** SEC contact notice when needed, the market strip, then the live Insider waterfall. */
export function HomePage() {
  return (
    <>
      <SecContactBanner />
      <MarketStrip />
      <InsiderStream />
    </>
  );
}
