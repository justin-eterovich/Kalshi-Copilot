import { asCount, type OrderbookResponse } from "./api";

/**
 * Depth ladder.
 *
 * Kalshi quotes both sides as *bids*: `yes` are bids to buy YES, `no` are bids
 * to buy NO. A NO bid at price p is economically an offer to sell YES at
 * (1 - p), so the ask side is derived that way — which is what makes the two
 * columns comparable on one price axis.
 *
 * **This is a second copy of a mapping the codebase says must have exactly
 * one.** `backend/app/trading/direction.py` owns the real one, and CLAUDE.md
 * says never to inline it. This copy is display-only — it cannot cause an
 * order to exist and nothing downstream reads it — but if the convention ever
 * changes, this is the copy nobody greps for. It stays here only because a
 * depth ladder cannot be drawn without putting both sides on one axis, and
 * routing a chart through the order rail would be worse.
 */

interface Level {
  price: number;
  size: number;
  cumulative: number;
}

function build(levels: [string, string][], invert: boolean): Level[] {
  const parsed = levels
    .map(([p, s]) => ({
      price: invert ? (1 - Number(p)) * 100 : Number(p) * 100,
      size: Number(s),
    }))
    .filter((l) => Number.isFinite(l.price) && Number.isFinite(l.size) && l.size > 0);

  // Bids best-first (highest); asks best-first (lowest).
  parsed.sort((a, b) => (invert ? a.price - b.price : b.price - a.price));

  let running = 0;
  return parsed.map((l) => {
    running += l.size;
    return { ...l, cumulative: running };
  });
}

function Side({
  levels,
  side,
  max,
}: {
  levels: Level[];
  side: "bid" | "ask";
  max: number;
}) {
  return (
    <div className="ladder-side">
      <div className="ladder-head">
        <span>{side === "bid" ? "bid ¢ (buy YES)" : "ask ¢ (sell YES)"}</span>
        <span className="num">size</span>
      </div>
      {levels.length === 0 && <div className="ladder-empty">no resting size</div>}
      {levels.slice(0, 12).map((l) => (
        <div className={`ladder-row ${side}`} key={`${side}-${l.price}`}>
          <span
            className="ladder-depth"
            style={{ width: `${max > 0 ? (l.size / max) * 100 : 0}%` }}
          />
          <span className="ladder-price">{l.price.toFixed(1)}</span>
          {/* Same count language as every other size in the app; this column
              used to be the one place a raw toLocaleString appeared. */}
          <span className="ladder-size num">{asCount(String(l.size))}</span>
        </div>
      ))}
    </div>
  );
}

export default function OrderBookLadder({
  book,
}: {
  book: OrderbookResponse | null;
}) {
  if (!book) {
    return <p className="muted">No orderbook available.</p>;
  }

  const bids = build(book.yes ?? [], false);
  const asks = build(book.no ?? [], true);

  const max = Math.max(
    1,
    ...bids.slice(0, 12).map((l) => l.size),
    ...asks.slice(0, 12).map((l) => l.size),
  );

  const bestBid = bids[0]?.price ?? null;
  const bestAsk = asks[0]?.price ?? null;
  const spread = bestBid !== null && bestAsk !== null ? bestAsk - bestBid : null;

  return (
    <div>
      <div className="ladder-summary">
        <span>
          bid <strong className="up">{bestBid?.toFixed(1) ?? "—"}¢</strong>
        </span>
        <span>
          ask <strong className="down">{bestAsk?.toFixed(1) ?? "—"}¢</strong>
        </span>
        <span>
          spread <strong>{spread !== null ? spread.toFixed(1) : "—"}¢</strong>
        </span>
        <span className="muted">
          {book.source === "kalshi" ? "live" : "cached"}
          {book.seq !== null && book.seq !== undefined ? ` · seq ${book.seq}` : ""}
        </span>
      </div>

      <div className="ladder">
        <Side levels={bids} side="bid" max={max} />
        <Side levels={asks} side="ask" max={max} />
      </div>
    </div>
  );
}
