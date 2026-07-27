/** The trading page: approval queue, working orders, positions, audit trail.
 *
 * The queue is the point of the whole system, so it sits at the top and the
 * page tells you plainly where an approved order would go. Proposals arrive
 * over the WebSocket relay as well as by polling, because an approval request
 * that only appears after a refresh is an approval request you miss.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import ApprovalCard from "./ApprovalCard";
import {
  api,
  asClock,
  asDollars,
  centsNum,
  routeLabel,
  type AuditEntry,
  type FillRow,
  type OrderRow,
  type PositionRow,
  type Proposal,
  type TradingState,
} from "./api";
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
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, p, o, pos, f, a] = await Promise.all([
        api.tradingState(),
        api.proposals(),
        api.orders(),
        api.positions(),
        api.fills(),
        api.audit(),
      ]);
      setState(s);
      setProposals(p.proposals);
      setOrders(o.orders);
      setPositions(pos.positions);
      setFills(f.fills);
      setAudit(a.entries);
      setError(null);
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
