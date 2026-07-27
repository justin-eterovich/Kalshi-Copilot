/** Manual trade ticket.
 *
 * Creating a proposal here sends nothing to any exchange — it puts a request
 * in the approval queue, which is the only path to an order.
 *
 * The cost preview comes from the backend on every edit rather than being
 * computed in the browser. That is not laziness: the fee formula lives in one
 * module by design, and a second implementation in JavaScript would be a
 * second set of numbers to disagree with the first. It also means prices stay
 * strings the whole way, so nothing loses precision on the round trip.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  api,
  asDollars,
  asSignedCents,
  routeLabel,
  type MarketDetail,
  type TicketInput,
  type TicketQuote,
  type TradingState,
} from "./api";

type Side = "yes" | "no";
type Action = "buy" | "sell";

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{children}</span>
    </div>
  );
}

/** Best executable price for a side, as a starting point for the form. */
function suggestedPrice(market: MarketDetail, side: Side, action: Action): string {
  const yesBid = market.yes_bid;
  const yesAsk = market.yes_ask;
  // Buying lifts the offer; selling hits the bid. Anything else pre-fills a
  // price that cannot trade.
  if (side === "yes") {
    const price = action === "buy" ? yesAsk : yesBid;
    return price ?? market.last_price ?? "0.50";
  }
  const price = action === "buy" ? market.no_ask : market.no_bid;
  if (price) return price;
  // No fallback derivation here on purpose. This used to compute
  // `(1 - Number(source)).toFixed(4)` — a float round trip whose result went
  // straight into `limit_price`, and a truncation to 4dp on an API that
  // carries 6. The API now derives the NO side exactly in Decimal and sends
  // it as a string, so an absent quote here means the YES side was absent
  // too and there is genuinely nothing to suggest.
  return "0.50";
}

export default function TradeTicket({
  market,
  state,
  onProposed,
}: {
  market: MarketDetail;
  state: TradingState | null;
  onProposed: () => void;
}) {
  const [side, setSide] = useState<Side>("yes");
  const [action, setAction] = useState<Action>("buy");
  const [price, setPrice] = useState("");
  const [contracts, setContracts] = useState("10");
  const [fairPrice, setFairPrice] = useState("");
  const [rationale, setRationale] = useState("");

  const [quote, setQuote] = useState<TicketQuote | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // Re-seed the price when the direction changes, but leave a price the
  // operator has edited alone.
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!touched) setPrice(suggestedPrice(market, side, action));
  }, [market, side, action, touched]);

  const ticket: TicketInput = useMemo(
    () => ({
      ticker: market.ticker,
      side,
      action,
      limit_price: price,
      contracts,
      fair_price: fairPrice || null,
      rationale: rationale || null,
    }),
    [market.ticker, side, action, price, contracts, fairPrice, rationale],
  );

  const refreshQuote = useCallback(async () => {
    if (!price || !contracts) {
      setQuote(null);
      return;
    }
    try {
      const result = await api.quoteTicket(ticket);
      setQuote(result.quote);
      setError(null);
      setBlocked(null);
    } catch (e) {
      setQuote(null);
      const err = e as ApiError;
      // A market whose fee multiplier is unverified cannot be priced at all,
      // and that is a different thing from a typo in the form.
      if (
        err.code === "unverified_fee_schedule" ||
        err.code === "unknown_series"
      ) {
        setBlocked(err.detail || String(err));
      } else {
        setError(err.detail || String(err));
      }
    }
  }, [ticket, price, contracts]);

  useEffect(() => {
    // Debounced: the operator is still typing.
    const id = setTimeout(refreshQuote, 250);
    return () => clearTimeout(id);
  }, [refreshQuote]);

  const propose = async () => {
    setBusy(true);
    setNote(null);
    try {
      const result = await api.propose(ticket);
      setNote(
        `Proposal #${result.proposal.id} queued — approve it to place the order.`,
      );
      onProposed();
    } catch (e) {
      const err = e as ApiError;
      setError(err.detail || String(err));
    } finally {
      setBusy(false);
    }
  };

  const killed = state?.kill_switch ?? false;
  const canPropose = quote !== null && !busy && !killed && !blocked;

  return (
    <section className="panel ticket">
      <div className="panel-head">
        <h2>Trade ticket</h2>
        {state && (
          <span
            className={state.real_money ? "pill bad" : "pill ok"}
            title="Where an approved order would actually go"
          >
            {routeLabel(state.execution_route)}
          </span>
        )}
      </div>

      {killed && (
        <div className="banner">
          <strong>Kill switch engaged.</strong> No new proposals are accepted
          and resting orders are being cancelled.
        </div>
      )}

      {blocked && (
        <div className="banner">
          <strong>This market cannot be priced.</strong> {blocked}
        </div>
      )}

      <div className="seg ticket-seg">
        <button
          className={side === "yes" ? "seg-btn active" : "seg-btn"}
          onClick={() => {
            setSide("yes");
            setTouched(false);
          }}
        >
          YES
        </button>
        <button
          className={side === "no" ? "seg-btn active" : "seg-btn"}
          onClick={() => {
            setSide("no");
            setTouched(false);
          }}
        >
          NO
        </button>
      </div>

      <div className="seg ticket-seg">
        <button
          className={action === "buy" ? "seg-btn active" : "seg-btn"}
          onClick={() => {
            setAction("buy");
            setTouched(false);
          }}
        >
          buy
        </button>
        <button
          className={action === "sell" ? "seg-btn active" : "seg-btn"}
          onClick={() => {
            setAction("sell");
            setTouched(false);
          }}
        >
          sell
        </button>
      </div>

      <label className="field">
        <span>limit price (dollars)</span>
        <input
          className="input"
          value={price}
          inputMode="decimal"
          placeholder="0.5600"
          onChange={(e) => {
            setTouched(true);
            setPrice(e.target.value);
          }}
        />
      </label>

      <label className="field">
        <span>contracts</span>
        <input
          className="input"
          value={contracts}
          inputMode="decimal"
          placeholder="10"
          onChange={(e) => setContracts(e.target.value)}
        />
      </label>

      <label className="field">
        <span>fair value (optional)</span>
        <input
          className="input"
          value={fairPrice}
          inputMode="decimal"
          placeholder="your estimate, e.g. 0.62"
          onChange={(e) => setFairPrice(e.target.value)}
        />
      </label>

      <label className="field">
        <span>rationale (optional)</span>
        <input
          className="input"
          value={rationale}
          placeholder="why this trade"
          onChange={(e) => setRationale(e.target.value)}
        />
      </label>

      {error && <p className="error">{error}</p>}

      {quote && (
        <div className="quote-box">
          <Row k="cost">{asDollars(quote.notional_cents)}</Row>
          <Row k="fee">
            {asDollars(quote.est_fee_cents)}
            <span className="muted"> ({quote.is_taker ? "taker" : "maker"})</span>
          </Row>
          <Row k="total">
            <strong>{asDollars(quote.total_cost_cents)}</strong>
          </Row>
          <Row k="breakeven">{Number(quote.breakeven_cents).toFixed(2)}¢</Row>
          <Row k="max win">{asDollars(quote.max_win_cents)}</Row>
          <Row k="max loss">{asDollars(quote.max_loss_cents)}</Row>
          {quote.net_edge_cents !== null && (
            <Row k="net edge">
              <span
                className={Number(quote.net_edge_cents) >= 0 ? "up" : "down"}
              >
                {asSignedCents(quote.net_edge_cents)}
              </span>
              <span className="muted"> /contract, after fees</span>
            </Row>
          )}
          <Row k="sends as">
            <span className="mono">
              {quote.wire.book_side} {quote.wire.count} @ {quote.wire.yes_price}
            </span>
          </Row>
        </div>
      )}

      <button className="btn primary" disabled={!canPropose} onClick={propose}>
        {busy ? "queueing…" : "propose trade"}
      </button>

      {note && <p className="note">{note}</p>}

      <p className="muted ticket-foot">
        This queues a proposal. Nothing reaches Kalshi until you approve it
        individually on the trades page.
      </p>
    </section>
  );
}
