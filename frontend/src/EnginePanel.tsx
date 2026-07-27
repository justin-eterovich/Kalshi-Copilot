/**
 * Why a detector is quiet.
 *
 * A silent detector and a broken one look identical from the outside, and the
 * two normal reasons for silence are both invisible without this panel: no
 * fresh reference price for the underlying, and not enough settled
 * observations to clear the calibration sample floor.
 *
 * The feed table also makes one specific failure impossible to repeat. The
 * stale-quote detector once priced Ethereum contracts against the Bitcoin
 * spot — an ETH strike of $1,969 next to BTC at $65,154 reads as decisively
 * resolved and produced a 72c phantom edge. Listing each underlying with its
 * own feed makes "we have no ETH price" a visible fact rather than an
 * assumption nobody checked.
 */

import type { EngineState } from "./api";

function age(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 90) return `${seconds.toFixed(0)}s ago`;
  if (seconds < 5400) return `${(seconds / 60).toFixed(0)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

export default function EnginePanel({ engine }: { engine: EngineState | null }) {
  if (!engine) return null;

  const { calibration: cal } = engine;
  const pct = Math.min(100, (cal.settled / Math.max(1, cal.min_samples)) * 100);
  const ready = cal.buckets.filter((b) => b.ready).length;

  return (
    <section className="panel" style={{ marginTop: 12 }}>
      <div className="panel-head">
        <h2>Engine</h2>
        <span className="muted">
          why a detector is quiet — usually a missing feed or too few samples
        </span>
      </div>

      <h3 className="sub">Reference feeds</h3>
      {!engine.bitcoin_enabled && (
        <p className="muted">
          <code>bitcoin.enabled</code> is false, so the spot poller is not
          running and every price-model detector refuses for lack of a
          reference.
        </p>
      )}
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>underlying</th>
              <th className="num">last</th>
              <th>source</th>
              <th>seen</th>
              <th>usable</th>
            </tr>
          </thead>
          <tbody>
            {engine.reference_feeds.map((f) => (
              <tr key={f.symbol}>
                <td className="mono">{f.symbol}</td>
                <td className="num">{f.price ?? "—"}</td>
                <td className="muted">{f.source ?? "—"}</td>
                <td className="muted">{age(f.age_sec)}</td>
                <td className={f.fresh ? "up" : "muted"}>
                  {f.fresh ? "fresh" : "no feed — markets refused"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="sub">Calibration coverage</h3>
      <div className="meter">
        <div className="meter-head">
          <span className="meter-label">
            settled observations toward the signalling floor
          </span>
          <span className="meter-figure">
            {cal.settled} / {cal.min_samples}
          </span>
        </div>
        <div className="meter-track">
          <div
            className={`meter-fill ${pct >= 100 ? "ok" : "warn"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="meter-hint">
          {cal.observations} market{cal.observations === 1 ? "" : "s"} observed,
          one row each — a market is never sampled twice, so the count is
          markets and not ticks. {cal.settled} have settled;{" "}
          {ready === 0
            ? "no price bucket has enough yet, so the screen says nothing."
            : `${ready} bucket${ready === 1 ? "" : "s"} past the floor.`}
        </p>
      </div>
    </section>
  );
}
