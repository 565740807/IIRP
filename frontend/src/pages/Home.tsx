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
