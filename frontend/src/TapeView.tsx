import { asClock, asCount, centsNum, type Trade } from "./api";

/**
 * Public trade tape.
 *
 * Kalshi's tape is anonymous — the API exposes no trader identity — so the
 * only signal here is shape: size, price, and which side crossed the spread.
 * The taker side is coloured because persistent one-sided aggression is what
 * the whale-flow detector keys on later.
 */
export default function TapeView({
  trades,
  limit = 40,
}: {
  trades: Trade[];
  limit?: number;
}) {
  if (trades.length === 0) {
    return <p className="muted">No trades recorded yet.</p>;
  }

  const sizes = trades
    .map((t) => Number(t.count ?? 0))
    .filter((n) => Number.isFinite(n) && n > 0);
  const median =
    sizes.length > 0
      ? sizes.slice().sort((a, b) => a - b)[Math.floor(sizes.length / 2)]
      : 0;

  return (
    <div className="tape">
      <div className="tape-head">
        <span>time</span>
        <span className="num">price</span>
        <span className="num">size</span>
        <span>taker</span>
      </div>
      {trades.slice(0, limit).map((t, i) => {
        const size = Number(t.count ?? 0);
        // Highlight prints well above this market's own norm.
        const big = median > 0 && size >= median * 5;
        return (
          <div
            className={`tape-row ${t.taker_side === "yes" ? "up" : "down"}${big ? " big" : ""}`}
            key={`${t.ts}-${i}`}
          >
            <span className="tape-time">{asClock(t.ts)}</span>
            <span className="num">{centsNum(t.yes_price)}</span>
            <span className="num">{asCount(t.count)}</span>
            <span className="tape-side">{t.taker_side ?? "—"}</span>
          </div>
        );
      })}
    </div>
  );
}
