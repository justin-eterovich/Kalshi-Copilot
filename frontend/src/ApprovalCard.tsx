/** One proposal awaiting a human decision.
 *
 * The card is deliberately dense with cost information and quiet about
 * upside. What it must make impossible is approving something by reflex:
 *
 * - Approve is a two-step interaction, never a single click.
 * - On the live route the operator has to type the market ticker, which a
 *   misclick cannot produce.
 * - The countdown is visible, and an expired proposal cannot be approved at
 *   all — the button is gone, not merely styled as disabled.
 * - The **wire form** is on screen: the traded-side view ("buy NO 30¢") and
 *   the thing actually sent ("ask @ 0.70") are different sentences, and only
 *   the second one can be checked against the exchange.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  api,
  asCentsAmount,
  asCount,
  asDollars,
  asSignedCents,
  centsNum,
  moneySign,
  type Proposal,
  type ProposalLeg,
  type TradingState,
} from "./api";

/**
 * The literal wire form of one leg, or null if the API did not send it.
 *
 * "Buy NO at 30¢" reaches Kalshi as an **ask at 0.70**, because the order API
 * quotes one book from the YES side. Both halves have to be right or the
 * position is inverted, and nothing downstream catches that: the P(1-P) fee
 * is symmetric, so a flipped direction produces the same fee, the same
 * notional and a confirmation that looks correct. Showing it here is the only
 * check a human gets — and for a detector-generated proposal it is the only
 * check anyone gets, because no one priced it by hand.
 */
function wireForm(leg: ProposalLeg): string | null {
  if (!leg.book_side || !leg.wire_price) return null;
  return `${leg.book_side} ${leg.contracts} @ ${leg.wire_price}`;
}

function useCountdown(expiresAt: string | null): number | null {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (!expiresAt) {
      setRemaining(null);
      return;
    }
    const target = new Date(expiresAt).getTime();
    const tick = () => setRemaining((target - Date.now()) / 1000);
    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [expiresAt]);

  return remaining;
}

export default function ApprovalCard({
  proposal,
  state,
  onDecided,
}: {
  proposal: Proposal;
  state: TradingState | null;
  onDecided: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remaining = useCountdown(proposal.expires_at);
  const expired = remaining !== null && remaining <= 0;
  const pending = proposal.status === "pending" && !expired;

  const needsPhrase = state?.requires_typed_confirmation ?? false;
  // A multi-leg proposal is one decision about an event; no single market
  // names it, so that is what has to be typed.
  const confirmTarget =
    proposal.leg_count > 1 && proposal.event_ticker
      ? proposal.event_ticker
      : proposal.ticker;
  const phraseOk =
    !needsPhrase || phrase.trim().toUpperCase() === confirmTarget.toUpperCase();

  const decide = async (approve: boolean) => {
    setBusy(true);
    setError(null);
    try {
      if (approve) {
        await api.approve(proposal.id, needsPhrase ? phrase : undefined);
      } else {
        await api.reject(proposal.id);
      }
      setConfirming(false);
      onDecided();
    } catch (e) {
      const err = e as ApiError;
      setError(err.detail || String(err));
      // A refusal leaves the proposal pending, so the operator can act again
      // once the reason clears. Reload so the card shows the truth.
      onDecided();
    } finally {
      setBusy(false);
    }
  };

  const urgency =
    remaining === null ? "" : remaining < 15 ? " urgent" : remaining < 40 ? " soon" : "";

  return (
    <div className={`approval${pending ? "" : " decided"}`}>
      <div className="approval-head">
        <div>
          <Link
            to={`/market/${encodeURIComponent(proposal.ticker)}`}
            className="mono approval-ticker"
          >
            {proposal.leg_count > 1 && proposal.event_ticker
              ? proposal.event_ticker
              : proposal.ticker}
          </Link>
          <div className="approval-sub">
            <span className="chip">{proposal.source}</span>
            {proposal.leg_count > 1 ? (
              <span className="chip warn">{proposal.leg_count} legs · all or none</span>
            ) : (
              proposal.legs[0] && (
                <span
                  className={
                    proposal.legs[0].action === "buy" ? "chip up" : "chip down"
                  }
                >
                  {proposal.legs[0].action} {proposal.legs[0].side.toUpperCase()}
                </span>
              )
            )}
            <span className="chip">{proposal.status}</span>
          </div>
        </div>

        {pending && remaining !== null && (
          <div className={`countdown${urgency}`} title="time left to decide">
            {Math.max(0, remaining).toFixed(0)}s
          </div>
        )}
      </div>

      <div className="approval-grid">
        <div className="stat">
          <span className="stat-label">
            {proposal.leg_count > 1 ? "legs" : "price"}
          </span>
          <span className="stat-value">
            {proposal.leg_count > 1
              ? proposal.leg_count
              : `${centsNum(proposal.legs[0]?.limit_price ?? null, 2)}¢`}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">size</span>
          <span className="stat-value">
            {asCount(proposal.legs[0]?.contracts ?? null)}
          </span>
        </div>
        {/* The worst case belongs at the decision point, not only on the
            ticket that composed it. It is also what the risk limits measure,
            and a detector proposal was never composed on a ticket at all. */}
        <div className="stat">
          <span className="stat-label">max loss</span>
          <span className="stat-value">
            {proposal.max_loss_cents === null
              ? "—"
              : asDollars(proposal.max_loss_cents)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">fee</span>
          <span className="stat-value">
            {asCentsAmount(proposal.est_fee_cents)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">net edge</span>
          <span
            className={
              proposal.net_edge_cents === null
                ? "stat-value"
                : moneySign(proposal.net_edge_cents) < 0
                  ? "stat-value down"
                  : moneySign(proposal.net_edge_cents) > 0
                    ? "stat-value up"
                    : "stat-value"
            }
          >
            {proposal.net_edge_cents === null
              ? "—"
              : asSignedCents(proposal.net_edge_cents)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">bankroll</span>
          <span className="stat-value">
            {proposal.pct_of_bankroll === null
              ? "—"
              : `${(proposal.pct_of_bankroll * 100).toFixed(2)}%`}
          </span>
        </div>
      </div>

      {/* The wire form, for a single-leg proposal. Multi-leg gets a column in
          the legs table below instead. */}
      {proposal.leg_count === 1 && proposal.legs[0] && (
        <div className="wire-row">
          <span className="wire-label">sends as</span>
          {wireForm(proposal.legs[0]) ? (
            <span className="mono wire-value">
              {wireForm(proposal.legs[0])}
            </span>
          ) : (
            <span className="wire-missing">
              not supplied by the API — the direction cannot be checked here
            </span>
          )}
        </div>
      )}

      {proposal.leg_count > 1 && (
        <div className="legs">
          <div className="legs-head">
            every leg is placed together, or the set is not a hedge
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>market</th>
                <th>direction</th>
                <th className="num">price ¢</th>
                <th className="num">size</th>
                <th title="what actually reaches Kalshi: one book, quoted from the YES side">
                  sends as
                </th>
              </tr>
            </thead>
            <tbody>
              {proposal.legs.map((leg) => (
                <tr key={leg.seq}>
                  <td className="mono">{leg.ticker}</td>
                  <td className={leg.action === "buy" ? "up" : "down"}>
                    {leg.action} {leg.side}
                  </td>
                  <td className="num">{centsNum(leg.limit_price, 2)}</td>
                  <td className="num">{asCount(leg.contracts)}</td>
                  <td className="mono">
                    {wireForm(leg) ?? (
                      <span className="wire-missing">not supplied</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {proposal.rationale && <p className="approval-why">{proposal.rationale}</p>}

      {error && <p className="error">{error}</p>}

      {!pending && proposal.decision_reason && (
        <p className="muted">{proposal.decision_reason}</p>
      )}

      {pending && !confirming && (
        <div className="approval-actions">
          <button className="btn primary" onClick={() => setConfirming(true)}>
            approve…
          </button>
          <button className="btn" disabled={busy} onClick={() => decide(false)}>
            reject
          </button>
        </div>
      )}

      {pending && confirming && (
        <div className="confirm">
          <p className={state?.real_money ? "confirm-live" : "confirm-paper"}>
            {state?.real_money ? (
              <>
                <strong>This places a REAL order with REAL money.</strong> Type
                the ticker to confirm.
              </>
            ) : (
              <>
                Places a <strong>{state?.execution_route === "simulated"
                  ? "simulated"
                  : "demo-exchange"}</strong>{" "}
                order — no real money. Confirm to proceed.
                {proposal.leg_count > 1 && (
                  <>
                    {" "}
                    <strong>
                      {proposal.leg_count} legs go out together with IOC, but the
                      exchange has no atomic multi-order primitive
                    </strong>{" "}
                    — if some fill and others do not, you are left with a
                    directional position and the card will say so.
                  </>
                )}
              </>
            )}
          </p>

          {needsPhrase && (
            <input
              className="input"
              value={phrase}
              autoFocus
              placeholder={confirmTarget}
              onChange={(e) => setPhrase(e.target.value)}
            />
          )}

          {/* Deliberately reversed. "confirm & place" used to land exactly
              where "approve…" had been, so two clicks in the same spot were
              closer to one click than the two-step design intends — and on
              the paper route there is no typed phrase to slow it down. */}
          <div className="approval-actions">
            <button
              className="btn"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                setPhrase("");
              }}
            >
              cancel
            </button>
            <button
              className="btn primary"
              disabled={busy || !phraseOk}
              onClick={() => decide(true)}
            >
              {busy ? "placing…" : "confirm & place"}
            </button>
          </div>
        </div>
      )}

      {expired && proposal.status === "pending" && (
        <p className="muted">
          Expired — the quote it was priced against is gone. Re-propose rather
          than trading a stale edge.
        </p>
      )}
    </div>
  );
}
