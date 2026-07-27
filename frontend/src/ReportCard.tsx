/**
 * Per-detector evidence — the panel the README sends you to before going live.
 *
 * Its job is to be hard to misread in the optimistic direction. Three things
 * follow from that and none of them are decoration:
 *
 * 1. **The verdict is shown before the number.** A mean of +250¢ over four
 *    trades is the most persuasive thing this system can display and it means
 *    nothing; leading with the figure and appending a caveat gets the caveat
 *    skipped. The pill reads "insufficient evidence" and the mean is greyed.
 * 2. **The interval is always shown when there is one.** An expectancy without
 *    its interval is a point estimate pretending to be a result.
 * 3. **Claimed edge sits beside realised edge.** The gap between what a
 *    detector says it finds and what it has actually delivered is the single
 *    most informative column here, and it only exists side by side.
 *
 * Money arrives as decimal strings and is formatted, never parsed into a
 * number for arithmetic — same rule as everywhere else in this frontend.
 */

import { useEffect, useState } from "react";
import {
  api,
  asDollars,
  asSignedCents,
  routeLabel,
  type DetectorReport,
  type ReportCardResponse,
} from "./api";

function verdictPill(v: DetectorReport["verdict"]): {
  text: string;
  cls: string;
} {
  switch (v) {
    case "edge_shown":
      return { text: "edge shown", cls: "pill ok" };
    case "losing":
      return { text: "losing", cls: "pill bad" };
    case "no_edge_shown":
      return { text: "no edge", cls: "pill warn" };
    default:
      // "untested", not "pending" or "too early" — those read as a promise
      // that the verdict is on its way. The full sentence under the table
      // says how far off it is; the pill only has to avoid overstating.
      return { text: "untested", cls: "pill" };
  }
}

function interval(r: DetectorReport): string {
  if (r.ci_low_cents === null || r.ci_high_cents === null) return "—";
  return `[${asSignedCents(r.ci_low_cents)}, ${asSignedCents(r.ci_high_cents)}]`;
}

/** signals → proposals → decided → fills, as one compact string. */
function funnelText(r: DetectorReport): string {
  const f = r.funnel;
  return `${f.signals} → ${f.proposals} → ${f.decided} → ${f.fills}`;
}

export default function ReportCard() {
  const [data, setData] = useState<ReportCardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        setData(await api.reportCard());
        setError(null);
      } catch (e) {
        setError(String(e));
      }
    };
    load();
    // Deliberately slower than the 3s trading poll. This aggregate changes on
    // the timescale of settled trades, not seconds, and there is no reason to
    // re-run it 20 times a minute.
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  if (error) {
    return (
      <section className="panel" style={{ marginTop: 12 }}>
        <h2>Report card</h2>
        <p className="error">{error}</p>
      </section>
    );
  }
  if (!data) return null;

  const rows = data.detectors;
  const proven = rows.filter((r) => r.verdict === "edge_shown");

  return (
    <section className="panel" style={{ marginTop: 12 }}>
      <div className="panel-head">
        <h2>Report card</h2>
        <span className="muted">
          what each detector claimed, and what it delivered — net of fees
        </span>
      </div>

      <p className="muted">
        A detector needs <strong>{data.min_trades}</strong> closed decisions
        before any verdict is reported, and an interval that excludes zero
        before it reads as an edge. One approval counts once however many legs
        it had: a five-leg arbitrage is one thesis with one outcome, and
        counting the legs would narrow the interval on perfectly correlated
        results. Routes are never summed.
      </p>

      {rows.length === 0 ? (
        <p className="muted">
          No detector has emitted a signal in the last {data.window_days} days.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>detector</th>
                <th>route</th>
                <th>funnel</th>
                <th className="num">trades</th>
                <th className="num">claimed</th>
                <th className="num">realised</th>
                <th className="num">95% interval</th>
                <th className="num">total</th>
                <th className="num">max dd</th>
                <th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const pill = verdictPill(r.verdict);
                const untested = r.verdict === "insufficient_evidence";
                return (
                  <tr key={`${r.detector}-${r.route}`}>
                    <td className="mono">{r.detector}</td>
                    <td
                      className={
                        r.route === "live_exchange" ? "warn" : "muted"
                      }
                    >
                      {routeLabel(r.route)}
                    </td>
                    <td className="mono muted" title="signals → proposals → decided → fills">
                      {funnelText(r)}
                    </td>
                    <td className="num">{r.trades}</td>
                    <td className="num muted">
                      {r.avg_claimed_edge_cents === null
                        ? "—"
                        : asSignedCents(r.avg_claimed_edge_cents)}
                    </td>
                    {/* Greyed while untested: the figure is real arithmetic
                        over too few samples, and it must not read as a
                        result just because it is a number. */}
                    <td className={untested ? "num muted" : "num"}>
                      {r.trades === 0
                        ? "—"
                        : asSignedCents(r.mean_pnl_cents)}
                    </td>
                    <td className={untested ? "num muted" : "num"}>
                      {interval(r)}
                    </td>
                    <td className="num">
                      {r.trades === 0 ? "—" : asDollars(r.total_pnl_cents)}
                    </td>
                    <td className="num muted">
                      {r.trades === 0 ? "—" : asDollars(r.max_drawdown_cents)}
                    </td>
                    <td>
                      <span className={pill.cls}>{pill.text}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* The pill is a glance; this is the actual conclusion, in words, for
          anything that has traded at all. A verdict with no sentence behind
          it invites the reader to supply their own. */}
      {rows.some((r) => r.trades > 0) && (
        <ul className="rules">
          {rows
            .filter((r) => r.trades > 0)
            .map((r) => (
              <li key={`${r.detector}-${r.route}-why`}>
                <span className="mono">{r.detector}</span>{" "}
                <span className="muted">({routeLabel(r.route)})</span> —{" "}
                {r.headline}
              </li>
            ))}
        </ul>
      )}

      {rows.some((r) => r.unattributed > 0) && (
        <p className="muted">
          Some settlements were dropped rather than divided: when two detectors
          have traded the same market on the same route there is no defensible
          way to split the outcome, so it counts for neither.{" "}
          {rows
            .filter((r) => r.unattributed > 0)
            .map((r) => `${r.detector}: ${r.unattributed}`)
            .join(", ")}
          .
        </p>
      )}

      <div className="stats" style={{ marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">detectors with proven edge</span>
          <span
            className={proven.length > 0 ? "stat-value ok" : "stat-value"}
          >
            {proven.length} of {rows.length}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">evidence floor</span>
          <span className="stat-value">{data.min_trades} decisions</span>
        </div>
        <div className="stat">
          <span className="stat-label">window</span>
          <span className="stat-value">{data.window_days}d</span>
        </div>
      </div>

      {proven.length === 0 && rows.length > 0 && (
        <p className="note">
          No detector has demonstrated positive expectancy net of fees. Nothing
          here has earned real money yet.
        </p>
      )}
    </section>
  );
}
