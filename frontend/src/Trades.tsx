/** The trading page: approval queue, working orders, positions, audit trail.
 *
 * The queue is the point of the whole system, so it sits at the top and the
 * page tells you plainly where an approved order would go. Proposals arrive
 * over the WebSocket relay as well as by polling, because an approval request
 * that only appears after a refresh is an approval request you miss.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import ApprovalCard from "./ApprovalCard";
import EnginePanel from "./EnginePanel";
import NewsPanel from "./NewsPanel";
import RiskPanel from "./RiskPanel";
import {
  api,
  asCents,
  asClock,
  asDollars,
  asSignedCents,
  centsNum,
  routeLabel,
  type AuditEntry,
  type FillRow,
  type OrderRow,
  type PositionRow,
  type Proposal,
  type EngineState,
  type NewsState,
  type RiskResponse,
  type SettlementRow,
  type SignalRow,
  type TradingState,
} from "./api";
import { alertOnNew, setBadge, unlockAudio } from "./notify";
import { useLiveFeed } from "./useLiveFeed";

const WORKING = new Set(["pending", "resting", "partially_filled"]);

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="muted">{children}</p>;
}

export default function Trades() {
  const [state, setState] = useState<TradingState | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [fills, setFills] = useState<FillRow[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [risk, setRisk] = useState<RiskResponse | null>(null);
  const [settlements, setSettlements] = useState<SettlementRow[]>([]);
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [news, setNews] = useState<NewsState | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Null until the first poll lands — an empty set here would be read as "the
  // queue was empty last time" and would swallow the first alert.
  const seenProposals = useRef<Set<number> | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, p, o, pos, f, a, sig, r, settled, eng, nws] = await Promise.all([
        api.tradingState(),
        api.proposals(),
        api.orders(),
        api.positions(),
        api.fills(),
        api.audit(),
        api.signals(),
        api.risk(),
        api.settlements(),
        api.engine(),
        api.news(),
      ]);
      setState(s);
      setProposals(p.proposals);
      setOrders(o.orders);
      setPositions(pos.positions);
      setFills(f.fills);
      setAudit(a.entries);
      setSignals(sig.signals);
      setRisk(r);
      setSettlements(settled.settlements);
      setEngine(eng);
      setNews(nws);
      setError(null);

      // A proposal lives about two minutes. If the tab is in the background
      // for that long the decision is missed entirely, so the arrival has to
      // be able to interrupt.
      seenProposals.current = alertOnNew(
        p.proposals.filter((x) => x.status === "pending").map((x) => x.id),
        seenProposals.current,
      );
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    // The countdown is local, but expiry and fills happen server-side.
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  // Leaving the page must clear the badge; a stale "(3)" in the tab strip is
  // worse than none, because it is the thing being trusted at a glance.
  useEffect(() => () => setBadge(0), []);

  // Browsers refuse to start an AudioContext before a real user gesture, so
  // the first click anywhere on the page arms the ping. Until then the
  // favicon and title badges carry the signal on their own.
  useEffect(() => {
    const arm = () => unlockAudio();
    window.addEventListener("pointerdown", arm, { once: true });
    window.addEventListener("keydown", arm, { once: true });
    return () => {
      window.removeEventListener("pointerdown", arm);
      window.removeEventListener("keydown", arm);
    };
  }, []);

  // Proposals and orders bypass the per-market tick filter by design.
  useLiveFeed([], (message) => {
    if (
      message.channel === "copilot:proposals" ||
      message.channel === "copilot:orders"
    ) {
      load();
    }
  });

  const pending = useMemo(
    () => proposals.filter((p) => p.status === "pending"),
    [proposals],
  );
  const decided = useMemo(
    () => proposals.filter((p) => p.status !== "pending").slice(0, 20),
    [proposals],
  );
  const working = useMemo(
    () => orders.filter((o) => WORKING.has(o.status)),
    [orders],
  );

  return (
    <div>
      {error && <div className="banner">{error}</div>}

      {state?.route_blocked_by && (
        <div className="banner">
          <strong>No usable execution route.</strong> Approvals will be
          refused: <code>{state.route_blocked_by}</code>. Check{" "}
          <code>trading.mode</code> in <code>config.yaml</code> against{" "}
          <code>KALSHI_ENV</code> and <code>LIVE_TRADING</code>.
        </div>
      )}

      {state?.kill_switch && (
        <div className="banner">
          <strong>Kill switch engaged.</strong> No new proposals, and working
          orders are being cancelled.
        </div>
      )}

      <div className="stats">
        <div className="stat">
          <span className="stat-label">route</span>
          <span
            className={state?.real_money ? "stat-value warn" : "stat-value"}
          >
            {routeLabel(state?.execution_route ?? null)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">awaiting you</span>
          <span className={pending.length ? "stat-value warn" : "stat-value"}>
            {pending.length}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">working orders</span>
          <span className="stat-value">{working.length}</span>
        </div>
        <div className="stat">
          <span className="stat-label">open positions</span>
          <span className="stat-value">{positions.length}</span>
        </div>
        {state?.balance?.dollars && (
          <div className="stat">
            <span className="stat-label">balance</span>
            <span className="stat-value">
              ${Number(state.balance.dollars).toFixed(2)}
            </span>
          </div>
        )}
        <div className="stat">
          <span className="stat-label">proposal ttl</span>
          <span className="stat-value">{state?.proposal_ttl_sec ?? "—"}s</span>
        </div>
      </div>

      {/* Above the queue on purpose: the limits are what will refuse an
          approval, so they belong where they are read before deciding, not
          buried under the decision. */}
      <RiskPanel risk={risk} />

      <section className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-head">
          <h2>Approval queue</h2>
          <span className="muted">
            every order requires your explicit per-trade approval
          </span>
        </div>

        {pending.length === 0 ? (
          <Empty>
            Nothing awaiting a decision. Open a market and use the trade ticket
            to queue one; detectors start filling this queue in M4.
          </Empty>
        ) : (
          <div className="approvals">
            {pending.map((p) => (
              <ApprovalCard
                key={p.id}
                proposal={p}
                state={state}
                onDecided={load}
              />
            ))}
          </div>
        )}
      </section>

      <div className="split">
        <section className="panel">
          <h2>Working orders</h2>
          {working.length === 0 ? (
            <Empty>No orders working.</Empty>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>ticker</th>
                    <th>side</th>
                    <th className="num">price</th>
                    <th className="num">filled</th>
                    <th>status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {working.map((o) => (
                    <tr key={o.id}>
                      <td className="mono">
                        <Link to={`/market/${encodeURIComponent(o.ticker)}`}>
                          {o.ticker}
                        </Link>
                      </td>
                      <td className={o.action === "buy" ? "up" : "down"}>
                        {o.action} {o.side}
                      </td>
                      <td className="num">{centsNum(o.limit_price, 2)}</td>
                      <td className="num">
                        {o.filled_contracts}/{o.contracts}
                      </td>
                      <td>{o.status}</td>
                      <td>
                        <button
                          className="btn"
                          onClick={async () => {
                            await api.cancelOrder(o.id);
                            load();
                          }}
                        >
                          cancel
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="panel">
          <h2>Positions</h2>
          {positions.length === 0 ? (
            <Empty>Flat.</Empty>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>ticker</th>
                    <th>side</th>
                    <th className="num">size</th>
                    <th className="num">avg</th>
                    <th className="num">unreal.</th>
                    <th className="num">real.</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={`${p.ticker}-${p.route}`}>
                      <td className="mono">
                        <Link to={`/market/${encodeURIComponent(p.ticker)}`}>
                          {p.ticker}
                        </Link>
                      </td>
                      <td className={p.side === "yes" ? "up" : "down"}>
                        {p.side}
                      </td>
                      <td className="num">{p.contracts}</td>
                      <td className="num">{centsNum(p.avg_price, 2)}</td>
                      <td
                        className={
                          p.unrealized_pnl_cents === null
                            ? "num"
                            : Number(p.unrealized_pnl_cents) >= 0
                              ? "num up"
                              : "num down"
                        }
                      >
                        {p.unrealized_pnl_cents === null
                          ? "—"
                          : asDollars(p.unrealized_pnl_cents)}
                      </td>
                      <td
                        className={
                          Number(p.realized_pnl_cents) >= 0 ? "num up" : "num down"
                        }
                      >
                        {asDollars(p.realized_pnl_cents)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      <div className="split" style={{ marginTop: 12 }}>
        <section className="panel">
          <h2>Fills</h2>
          {fills.length === 0 ? (
            <Empty>No fills yet.</Empty>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>time</th>
                    <th>ticker</th>
                    <th>side</th>
                    <th className="num">price</th>
                    <th className="num">size</th>
                    <th className="num">fee</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.map((f) => (
                    <tr key={f.id}>
                      <td className="muted">{f.ts ? asClock(f.ts) : "—"}</td>
                      <td className="mono">{f.ticker}</td>
                      <td className={f.action === "buy" ? "up" : "down"}>
                        {f.action} {f.side}
                      </td>
                      <td className="num">{centsNum(f.price, 2)}</td>
                      <td className="num">{f.contracts}</td>
                      <td className="num">{asDollars(f.fee_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="panel">
          <h2>Recent decisions</h2>
          {decided.length === 0 ? (
            <Empty>No decisions recorded yet.</Empty>
          ) : (
            <div className="approvals compact">
              {decided.map((p) => (
                <ApprovalCard
                  key={p.id}
                  proposal={p}
                  state={state}
                  onDecided={load}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="panel" style={{ marginTop: 12 }}>
        <div className="panel-head">
          <h2>Settlements</h2>
          <span className="muted">
            outcomes, not decisions — where a held-to-resolution thesis is
            finally scored
          </span>
        </div>
        {settlements.length === 0 ? (
          <Empty>
            Nothing has settled yet. A position held to resolution shows its
            result here, not in Fills.
          </Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>market</th>
                  <th>route</th>
                  <th>result</th>
                  <th className="num">held</th>
                  <th className="num">avg</th>
                  <th className="num">paid</th>
                  <th className="num">P&amp;L</th>
                  <th>when</th>
                </tr>
              </thead>
              <tbody>
                {settlements.map((s) => (
                  <tr key={`${s.ticker}-${s.route}`}>
                    <td>
                      <Link to={`/market/${s.ticker}`}>{s.ticker}</Link>
                    </td>
                    <td>{routeLabel(s.route)}</td>
                    <td>{s.result ?? "—"}</td>
                    <td className="num">{s.net_contracts}</td>
                    {/* Both are YES prices in dollars, not cents figures —
                        asDollars would divide them by 100 a second time. */}
                    <td className="num">{asCents(s.avg_price)}</td>
                    <td className="num">{asCents(s.settled_yes_value)}</td>
                    <td
                      className={
                        Number(s.realized_pnl_cents) < 0 ? "num bad" : "num ok"
                      }
                    >
                      {asSignedCents(s.realized_pnl_cents)}
                    </td>
                    <td>{s.settled_at ? asClock(s.settled_at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel" style={{ marginTop: 12 }}>
        <div className="panel-head">
          <h2>Detector signals</h2>
          <span className="muted">
            observations, not recommendations — some never become proposals
          </span>
        </div>
        {signals.length === 0 ? (
          <Empty>
            Nothing yet. Detectors ship disabled; enable one at a time in{" "}
            <code>config.yaml</code> and let the report card earn your trust.
          </Empty>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>time</th>
                  <th>detector</th>
                  <th>ticker</th>
                  <th>side</th>
                  <th className="num">net edge</th>
                  <th className="num">conf</th>
                  <th className="num">seen</th>
                  <th>why</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((sg) => (
                  <tr key={sg.id}>
                    <td className="muted">
                      {sg.created_at ? asClock(sg.created_at) : "—"}
                    </td>
                    <td className="mono">{sg.detector}</td>
                    {/* Not every signal is about a market. The calibration
                        screen reports on a price *band* and names itself
                        BUCKET-5C, which has no market page to link to — a
                        link there is a guaranteed 404. */}
                    <td className="mono">
                      {sg.ticker.startsWith("BUCKET-") ? (
                        <span className="muted">{sg.ticker}</span>
                      ) : (
                        <Link to={`/market/${encodeURIComponent(sg.ticker)}`}>
                          {sg.ticker}
                        </Link>
                      )}
                    </td>
                    <td className={sg.side === "yes" ? "up" : "down"}>{sg.side}</td>
                    <td
                      className={
                        Number(sg.net_edge_cents) > 0 ? "num up" : "num"
                      }
                    >
                      {Number(sg.net_edge_cents) === 0
                        ? "—"
                        : asSignedCents(sg.net_edge_cents)}
                    </td>
                    <td className="num">{(sg.confidence * 100).toFixed(0)}%</td>
                    {/* Repeats fold into the row instead of adding one, so a
                        high count is a persisting edge rather than clutter. */}
                    <td
                      className="num muted"
                      title={
                        sg.last_seen_at
                          ? `last seen ${asClock(sg.last_seen_at)}`
                          : undefined
                      }
                    >
                      {sg.seen_count > 1 ? `${sg.seen_count}×` : "—"}
                    </td>
                    <td className="audit-detail">{sg.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <NewsPanel news={news} />

      <EnginePanel engine={engine} />

      <section className="panel" style={{ marginTop: 12 }}>
        <h2>Audit trail</h2>
        <p className="muted">
          Append-only. Every proposal, decision, order, and fill.
        </p>
        {audit.length === 0 ? (
          <Empty>Nothing recorded yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>time</th>
                  <th>kind</th>
                  <th>ticker</th>
                  <th>actor</th>
                  <th>detail</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((entry) => (
                  <tr key={entry.id}>
                    <td className="muted">
                      {entry.ts ? asClock(entry.ts) : "—"}
                    </td>
                    <td className="mono">{entry.kind}</td>
                    <td className="mono">{entry.ticker ?? "—"}</td>
                    <td>{entry.actor}</td>
                    <td className="audit-detail">
                      {entry.payload ? JSON.stringify(entry.payload) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
