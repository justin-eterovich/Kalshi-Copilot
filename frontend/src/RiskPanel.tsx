/**
 * Portfolio risk limits, shown continuously rather than only when they fire.
 *
 * A limit that speaks only at the moment it refuses a trade teaches nobody
 * anything — by then the decision has already been made and taken away. The
 * useful version is a bar that has been visibly filling for an hour, so the
 * refusal is expected when it arrives.
 *
 * Every figure here is a decimal string of cents from the API. Numbers appear
 * only for bar widths, where a rounded pixel count is the point.
 */

import { absCents, asDollars, moneySign, type RiskResponse } from "./api";

/**
 * Parse a cents string for arithmetic.
 *
 * The only place this file is allowed to make a number out of money, and it
 * is for bar widths and sign tests — never for anything displayed. Display
 * goes through `asDollars`, which formats the string as it arrived. Zero on
 * anything unparseable, so a missing figure draws an empty bar rather than
 * a NaN-wide one.
 *
 * The rule above was violated once, quietly: the day's loss was negated
 * through this function and the resulting float was handed straight to the
 * meter's caption. `absCents` keeps that a string.
 */
function num(cents: string | null | undefined): number {
  const value = Number(cents);
  return Number.isFinite(value) ? value : 0;
}

/** A labelled usage bar. `used` and `limit` are cents strings. */
function Meter({
  label,
  used,
  limit,
  hint,
}: {
  label: string;
  used: string;
  limit: string;
  hint?: string;
}) {
  const usedNum = num(used);
  const limitNum = num(limit);
  const pct = limitNum > 0 ? (usedNum / limitNum) * 100 : 0;
  // Clamped for the bar only. The caption below still reports the true
  // figure, so an over-limit book reads as over-limit rather than as full.
  const width = Math.max(0, Math.min(100, pct));
  const tone = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "ok";

  return (
    <div className="meter">
      <div className="meter-head">
        <span className="meter-label">{label}</span>
        <span className={`meter-figure ${tone}`}>
          {asDollars(used)} / {asDollars(limit)}
        </span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${width}%` }} />
      </div>
      {hint && <p className="meter-hint">{hint}</p>}
    </div>
  );
}

export default function RiskPanel({ risk }: { risk: RiskResponse | null }) {
  if (!risk) return null;

  const { state, limits } = risk;

  if (!state) {
    return (
      <section className="panel" style={{ marginBottom: 12 }}>
        <h2>Risk</h2>
        <p className="muted">
          No usable execution route, so there is no book to measure. Every
          approval is refused upstream.
        </p>
      </section>
    );
  }

  // The daily limit is a loss, so it fills from zero as the day goes against
  // you and stays empty while the day is profitable. The magnitude is taken
  // off the string, not off a parsed float — this figure is displayed.
  const lossUsed =
    moneySign(state.daily_net_cents) < 0 ? absCents(state.daily_net_cents) : "0";

  return (
    <section className="panel" style={{ marginBottom: 12 }}>
      <h2>
        Risk <span className="muted">— {state.route}</span>
      </h2>

      {state.halted && (
        <div className="banner">
          <strong>Trading halted.</strong>{" "}
          {state.daily_loss_breached
            ? `Today's realised P&L after fees is ${asDollars(
                state.daily_net_cents,
              )}, at or past the daily loss limit. Clears at 00:00 UTC.`
            : `${state.consecutive_losses} consecutive losing closes. Cooling off until ${
                state.cooldown_until
                  ? new Date(state.cooldown_until).toLocaleTimeString()
                  : "—"
              }.`}
        </div>
      )}

      <Meter
        label={`total exposure (${(limits.max_total_exposure_pct * 100).toFixed(
          0,
        )}% of bankroll)`}
        used={state.exposure_cents}
        limit={state.exposure_limit_cents}
        hint={
          num(state.pending_cents) > 0
            ? `${asDollars(
                state.pending_cents,
              )} more is queued for approval — headroom after the queue is ${asDollars(
                state.headroom_cents,
              )}.`
            : undefined
        }
      />

      <Meter
        label={`daily loss limit (${(limits.daily_loss_limit_pct * 100).toFixed(
          0,
        )}% of bankroll)`}
        used={lossUsed}
        limit={state.daily_loss_limit_cents}
        hint={`Today: ${asDollars(
          state.daily_realized_cents,
        )} realised less ${asDollars(state.daily_fees_cents)} of fees.`}
      />

      <div className="stats">
        <div className="stat">
          <span className="stat-label">losing streak</span>
          <span
            className={
              state.consecutive_losses >= limits.cooldown_after_consecutive_losses
                ? "stat-value warn"
                : "stat-value"
            }
          >
            {state.consecutive_losses} / {limits.cooldown_after_consecutive_losses}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">bankroll</span>
          <span className="stat-value">{asDollars(state.bankroll_cents)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">per market cap</span>
          <span className="stat-value">
            {(limits.max_pct_per_market * 100).toFixed(0)}%
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Kelly fraction</span>
          <span className="stat-value">{limits.kelly_fraction}</span>
        </div>
      </div>
    </section>
  );
}
