/**
 * Whether the machine may trade without you, and when it may not, why.
 *
 * The thing this panel exists to prevent is an operator reaching for the wrong
 * stop in a hurry. There are two, they do different things, and they are not
 * interchangeable:
 *
 * - **The kill switch** halts *everything* — your own manual approvals
 *   included — and cancels every resting order. It lives in the topbar.
 * - **The disarm latch** stops only the machine. Manual approval keeps
 *   working, resting orders stay, positions are untouched.
 *
 * So both are rendered here, side by side, each saying what it does *not* do.
 * Someone who has just seen something alarming should be able to read one
 * sentence and know which button they want.
 *
 * Three other rules, all borrowed from the panels around it:
 *
 * 1. **Never claim a state the server has not reported.** Every pill renders
 *    what `/api/autonomy` said. A failed request leaves the last known state on
 *    screen with the error beside it, because a stop that looks engaged and is
 *    not is worse than no control.
 * 2. **The verdict leads, the number follows.** A `+61.6¢` lower bound over 16
 *    decisions is the most persuasive thing this screen can show and it is
 *    refused; leading with the figure gets the caveat skipped.
 * 3. **Money is formatted, never parsed.** Cents arrive as decimal strings and
 *    stay that way — same rule as everywhere else in this frontend.
 */

import { useState } from "react";
import {
  api,
  asCentsAmount,
  asClock,
  asSignedCents,
  routeLabel,
  type AutonomyPair,
  type AutonomyState,
} from "./api";

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{children}</span>
    </div>
  );
}

function Pill({
  ok,
  warn,
  children,
}: {
  ok?: boolean;
  warn?: boolean;
  children: React.ReactNode;
}) {
  const cls = ok ? "pill ok" : warn ? "pill warn" : "pill bad";
  return <span className={cls}>{children}</span>;
}

/** The four facts, in the order the docs list them. */
function armingRows(state: AutonomyState): [string, boolean, string][] {
  const routes = Object.entries(state.routes)
    .filter(([, on]) => on)
    .map(([name]) => name);
  return [
    ["autonomous.enabled", state.enabled, "config.yaml"],
    ["a route armed", routes.length > 0, routes.map(routeLabel).join(", ") || "none"],
    ["AUTONOMOUS_TRADING", state.env_armed, ".env, needs a restart"],
    ["live interlocks", state.live_armed, ".env, live route only"],
  ];
}

function verdictPill(v: AutonomyPair["verdict"]): { text: string; cls: string } {
  switch (v) {
    case "edge_shown":
      return { text: "edge shown", cls: "pill ok" };
    case "losing":
      return { text: "losing", cls: "pill bad" };
    case "no_edge_shown":
      return { text: "no edge", cls: "pill warn" };
    default:
      return { text: "untested", cls: "pill" };
  }
}

/**
 * Why this pair cannot trade, in one phrase — or null when it could.
 *
 * Deliberately re-derived on the client from the same numbers the gate uses,
 * rather than read from a server field. The gate does not write an audit row
 * per refusal (it evaluates every few seconds and that would swamp the trail),
 * so there is no per-pair reason to fetch. What is shown is therefore the
 * *binding condition*, not a record of a decision — which is what an operator
 * deciding whether to wait or to change something actually wants.
 */
function bindingReason(
  pair: AutonomyPair,
  state: AutonomyState,
): string | null {
  // Checked before anything measured, because the gate checks it before
  // anything measured: a hand-typed ticket is never machine-approved however
  // good its history looks, so showing an evidence-shaped reason here would
  // imply it could be, given time. It cannot.
  if (pair.detector === "manual") return "manual tickets are never auto-approved";
  const ev = state.evidence;
  if (!ev) return "no evidence snapshot yet";
  if (!ev.coverage_usable) return "backtest coverage unusable";
  if (pair.trades < ev.min_trades) {
    return `${pair.trades} of ${ev.min_trades} decisions`;
  }
  if (pair.verdict !== "edge_shown") return `verdict is ${pair.verdict.replace(/_/g, " ")}`;
  if (pair.ci_low_cents === null) return "no confidence interval";
  // String compare is not enough for sign, and parsing money to a number is
  // banned — but a leading "-" is exactly the question being asked.
  if (pair.ci_low_cents.trim().startsWith("-")) return "interval includes zero";
  if (!state.routes[pair.route as keyof AutonomyState["routes"]]) {
    return "route not armed";
  }
  return null;
}

export default function AutonomyPanel({
  state,
  onChanged,
}: {
  state: AutonomyState | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rearming, setRearming] = useState(false);
  const [phrase, setPhrase] = useState("");

  if (!state) {
    return (
      <section className="panel">
        <h2>Autonomy</h2>
        <p className="muted">Loading…</p>
      </section>
    );
  }

  const latched = state.disarmed_reason !== null;
  const armed = state.enabled && state.env_armed;
  const anyRoute = Object.values(state.routes).some(Boolean);
  const ev = state.evidence;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setRearming(false);
      setPhrase("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      // Re-read either way: after a failure the truth matters more.
      onChanged();
    }
  };

  return (
    <>
      {/* The headline state, before any detail. An operator glancing at this
          page is asking one question. */}
      <section className="panel autonomy">
        <div className="panel-head">
          <h2>Autonomy</h2>
          <Pill ok={!armed || !anyRoute} warn={armed && anyRoute}>
            {armed && anyRoute
              ? latched
                ? "armed, but latched off"
                : "ARMED — can trade unattended"
              : "off — every trade needs you"}
          </Pill>
        </div>

        {latched && (
          <div className="banner">
            <strong>The machine is latched off.</strong> {state.disarmed_reason}
            <br />
            This is <em>not</em> the kill switch: manual approvals still work and
            resting orders were left alone. The latch has no timeout and will not
            clear itself — that is deliberate, because it fires on a single
            ambiguous placement or a single unbalanced multi-leg fill, and both
            mean a person should look at the book before anything else trades.
          </div>
        )}

        {armed && anyRoute && !latched && (
          <div className="banner">
            <strong>This system can place orders without you.</strong> Proposals
            older than {state.min_proposal_age_sec}s are considered every{" "}
            {state.decision_interval_sec}s and approved if they clear the
            evidence gate and the budget below. Your veto window is that first{" "}
            {state.min_proposal_age_sec} seconds.
          </div>
        )}

        {/* The two stops, side by side, each naming what it does not do. */}
        <div className="split">
          <div>
            <h3>Stop the machine</h3>
            <p className="muted">
              Halts autonomous approvals only. Manual approval keeps working,
              resting orders stay, positions are untouched.
            </p>
            {latched ? (
              rearming ? (
                <>
                  <p className="kill-copy">
                    Clearing the latch resumes unattended trading. Type{" "}
                    <code>REARM</code> to confirm you have checked the book.
                  </p>
                  <input
                    className="input"
                    value={phrase}
                    autoFocus
                    placeholder="REARM"
                    onChange={(e) => setPhrase(e.target.value)}
                  />
                  <div className="approval-actions">
                    <button
                      className="btn"
                      disabled={busy || phrase.trim().toUpperCase() !== "REARM"}
                      onClick={() => act(() => api.rearmAutonomy(phrase))}
                    >
                      {busy ? "re-arming…" : "clear the latch"}
                    </button>
                    <button className="btn" onClick={() => setRearming(false)}>
                      cancel
                    </button>
                  </div>
                </>
              ) : (
                <button className="btn" onClick={() => setRearming(true)}>
                  clear the latch…
                </button>
              )
            ) : (
              <button
                className="btn kill-confirm"
                disabled={busy}
                onClick={() => act(api.disarmAutonomy)}
              >
                {busy ? "stopping…" : "stop autonomous trading"}
              </button>
            )}
          </div>

          <div>
            <h3>Kill switch</h3>
            <p className="muted">
              The other stop, and a bigger one: it halts <em>everything</em>{" "}
              including your own manual approvals, and cancels every resting
              order. It is in the topbar, reachable from any page.
            </p>
            <Row k="currently">
              <Pill ok={!state.kill_switch} warn={state.kill_switch}>
                {state.kill_switch ? "engaged" : "off"}
              </Pill>
            </Row>
          </div>
        </div>

        {error && <p className="error">{error}</p>}
      </section>

      <section className="panel">
        <h2>Arming</h2>
        <p className="muted">
          All four are required, and config holds only two. The environment half
          is deliberately outside this dashboard's reach — there is no auth in
          front of this page, so arming the machine takes a file edit and a
          restart. Disarming, symmetrically, is one click above.
        </p>
        {armingRows(state).map(([label, on, where]) => (
          <Row k={label} key={label}>
            <Pill ok={!on} warn={on}>
              {on ? "yes" : "no"}
            </Pill>{" "}
            <span className="muted">{where}</span>
          </Row>
        ))}
      </section>

      <section className="panel">
        <h2>Budget</h2>
        <p className="muted">
          Every ceiling defaults to <strong>0, and 0 refuses</strong> — there is
          no way to write "unlimited". Spending is derived from the audit trail
          rather than counted beside it, so it is exact across restarts.
        </p>
        <Row k="trades / hour">{state.budget.max_trades_per_hour || "0 — refuses"}</Row>
        <Row k="trades / detector / hour">
          {state.budget.max_trades_per_detector_per_hour || "0 — refuses"}
        </Row>
        <Row k="daily risk">
          {state.budget.max_daily_risk_cents
            ? asCentsAmount(String(state.budget.max_daily_risk_cents))
            : "0 — refuses"}
        </Row>
        <Row k="open positions">{state.budget.max_open_positions || "0 — refuses"}</Row>
        <Row k="working orders">{state.budget.max_working_orders || "0 — refuses"}</Row>
        <Row k="repeat cooldown">{state.budget.repeat_cooldown_sec}s</Row>
        <p className="muted" style={{ marginTop: 8 }}>
          The cooldown is per detector per market. Without it a detector
          re-derives the same edge on its next scan, the duplicate guard has
          already cleared, and one market gets traded on every scan until the
          exposure cap binds.
        </p>
      </section>

      <section className="panel autonomy">
        <div className="panel-head">
          <h2>Evidence</h2>
          {ev && (
            <span className="muted">
              {/* `asClock`, not a raw `toLocale*`: the evidence age is the
                  one thing on this panel that says whether the gate is
                  looking at a live report card or a dead worker's last one,
                  and a formatter that throws would take it — and the rest of
                  the panel — off the page entirely. */}
              measured {asClock(ev.computed_at)}
            </span>
          )}
        </div>

        {!ev ? (
          <p className="muted">
            No snapshot yet. The worker recomputes this every 300s; until one
            succeeds the machine has measured nothing and refuses everything.
          </p>
        ) : (
          <>
            {!ev.coverage_usable && (
              <p className="wire-missing">
                <strong>Backtest coverage is unusable</strong>, so every pair
                below is refused whatever its numbers say:{" "}
                {ev.coverage_refusals.join(", ") || "refused"}. Kalshi publishes
                no historical orderbook, so this only improves by letting ingest
                run for longer — it is not a threshold to lower.
              </p>
            )}
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>detector</th>
                    <th>route</th>
                    <th>verdict</th>
                    <th className="num">decisions</th>
                    <th className="num">interval low</th>
                    <th className="num">mean</th>
                    <th>binding reason</th>
                  </tr>
                </thead>
                <tbody>
                  {ev.pairs.map((p) => {
                    const v = verdictPill(p.verdict);
                    const why = bindingReason(p, state);
                    return (
                      <tr key={`${p.detector}:${p.route}`}>
                        <td>{p.detector}</td>
                        <td>{routeLabel(p.route)}</td>
                        <td>
                          <span className={v.cls}>{v.text}</span>
                        </td>
                        <td className="num">
                          {p.trades} / {ev.min_trades}
                        </td>
                        {/* The only number that decides anything. */}
                        <td className="num">{asSignedCents(p.ci_low_cents)}</td>
                        {/* Greyed on purpose: it is never the basis of a
                            decision, and showing it level with the bound
                            invites reading it as one. */}
                        <td className="num muted">{asSignedCents(p.mean_cents)}</td>
                        <td className={why ? "muted" : ""}>
                          {why ?? "would authorise"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ marginTop: 8 }}>
              The <strong>interval low</strong> is what the gate reads — the
              bootstrap confidence interval's lower bound, which must be above
              zero. The mean is never consulted: on a 20-trade binary sample it
              is the figure that lies, and a flattering average over four trades
              is the most persuasive thing this system can produce.
            </p>
          </>
        )}
      </section>

    </>
  );
}
