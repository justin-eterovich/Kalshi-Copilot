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
import ReportCard from "./ReportCard";
import RiskPanel from "./RiskPanel";
import {
  api,
  asCentsAmount,
  asClock,
  asCount,
  asDollars,
  asSignedCents,
  asUsd,
  centsNum,
  moneySign,
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

/** A run of identical decided proposals, shown as one card plus a count. */
interface DecisionGroup {
  /** The most recent of the run — the one actually rendered. */
  lead: Proposal;
  count: number;
  /** When the run started, so "×47" reads as a duration and not a mystery. */
  since: string | null;
  sinceMs: number;
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="muted">{children}</p>;
}

/** Sign class for a money cell, with zero as its own answer. */
function pnlClass(cents: string | null | undefined): string {
  const sign = moneySign(cents);
  return sign > 0 ? "num up" : sign < 0 ? "num down" : "num";
}

/**
 * The undervalued screener's score, surfaced as a value.
 *
 * It reached the operator only as prose inside the rationale string, which is
 * the part most likely to be cut. It is an *ordering for attention* — not
 * cents, not a probability, and a 70 is not twice a 35 — so it is rendered
 * with that caveat attached rather than as a bare figure.
 */
function evidenceScore(evidence: Record<string, unknown> | null): string {
  const value = evidence?.score;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value.toFixed(1);
  }
  if (typeof value === "string" && value !== "") return value;
  return "—";
}

/**
 * One sentence about detector state, from `/api/system` rather than from a
 * string literal.
 *
 * `null` is "not known yet" and stays silent on purpose: the bug being fixed
 * here was copy that asserted a state it had not checked, and asserting the
 * opposite state without checking is the same bug.
 *
 * `/api/system` reports `enabled_detector_names(config)`, the wide list —
 * the one that includes the weather engine, which is enabled outside the
 * `detectors:` block and is invisible to `DetectorsConfig.enabled_names()`.
 */
function DetectorNote({ names }: { names: string[] | null }) {
  if (names === null) return null;
  if (names.length === 0) {
    return (
      <>
        {" "}
        No detector is enabled right now — turn them on one at a time in{" "}
        <code>config.yaml</code> and let the report card earn your trust first.
      </>
    );
  }
  return (
    <>
      {" "}
      {names.length} detector{names.length === 1 ? " is" : "s are"} enabled (
      <span className="mono">{names.join(", ")}</span>) — they propose only
      when they find an edge that survives fees, and most scans find none.
    </>
  );
}

/** Trader-language view of a signed position: "12.00" NO, not "-12.00". */
function heldSide(netContracts: string): { size: string; side: string } {
  const negative = netContracts.trim().startsWith("-");
  return {
    size: asCount(negative ? netContracts.trim().slice(1) : netContracts),
    side: negative ? "no" : "yes",
  };
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

  /**
   * Which detectors are actually running.
   *
   * Two empty states on this page told the operator "detectors ship
   * disabled" unconditionally, while the Recent Decisions panel a few
   * hundred pixels below filled with live proposals from those same
   * detectors. The shipped default is not the current state, and copy that
   * asserts one as the other is the failure mode CLAUDE.md warns about — a
   * comment that is load-bearing right up until it is wrong.
   *
   * Fetched once, not on the 3s poll: enablement comes from `config.yaml`,
   * which is read at boot, so it cannot change while this page is open.
   * `null` means "not yet known" and says nothing either way — an empty
   * array would claim "none enabled", which is a different fact.
   */
  const [detectors, setDetectors] = useState<string[] | null>(null);

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

  useEffect(() => {
    api
      .system()
      .then((s) => setDetectors(s.enabled_detectors))
      .catch(() => setDetectors(null));
  }, []);

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
  /**
   * Decided proposals, folded by (source, market, shape, outcome).
   *
   * A detector re-derives the same edge on every scan, so an undecided
   * proposal expiring on its TTL is immediately followed by an identical one
   * — the re-proposal loop CLAUDE.md documents under Autonomy. Unfolded, this
   * panel was hundreds of visually identical `expired` cards and most of a
   * 24,000px page, which is how the *interesting* rows — an approval, a
   * rejection — got buried under the boring ones.
   *
   * Folding is display only. Nothing is dropped from the audit trail, the
   * repeat count is shown rather than hidden, and pending proposals are
   * excluded before the fold ever runs: a decision that is still waiting on a
   * human must never be collapsed into another card. Status is part of the
   * key for the same reason — an `approved` never folds into an `expired`.
   */
  const decided = useMemo(() => {
    const groups = new Map<string, DecisionGroup>();
    for (const p of proposals) {
      if (p.status === "pending") continue;
      const key = `${p.source}|${p.ticker}|${p.leg_count}|${p.status}`;
      const at = p.created_at ? Date.parse(p.created_at) : NaN;
      const group = groups.get(key);
      if (!group) {
        groups.set(key, { lead: p, count: 1, since: p.created_at, sinceMs: at });
        continue;
      }
      group.count += 1;
      // Ids are monotonic, so the highest is the most recent.
      if (p.id > group.lead.id) group.lead = p;
      if (!Number.isNaN(at) && (Number.isNaN(group.sinceMs) || at < group.sinceMs)) {
        group.since = p.created_at;
        group.sinceMs = at;
      }
    }
    // Insertion order is API order, which is newest-first, so the most
    // recently active group stays at the top.
    return [...groups.values()].slice(0, 20);
  }, [proposals]);
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
          <strong>Kill switch engaged.</strong> No new proposals, and resting
          orders were cancelled. Release it from the control in the header
          when you are ready to trade again.
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
            <span className="stat-value">{asUsd(state.balance.dollars)}</span>
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

        {/* The tab has to be open for any of this to reach you. Web Push
            needs a secure context and this dashboard is plain HTTP on a LAN
            address by design, so alerting is title, favicon and audio — all
            in-tab. On the live evidence that is not a footnote: 259 of 262
            set-arb proposals so far have expired without a decision. */}
        <p className="muted" style={{ marginBottom: 10 }}>
          Alerts fire only while this tab is open — title, tab badge and a
          ping. There is no background notification: the Push API needs HTTPS
          and the dashboard is deliberately plain HTTP on the LAN. A proposal
          you are not here for expires undecided.
        </p>

        {pending.length === 0 ? (
          <Empty>
            Nothing awaiting a decision. Open a market and use the trade ticket
            to queue one.
            <DetectorNote names={detectors} />
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
                    <th className="num">price ¢</th>
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
                        {asCount(o.filled_contracts)}/{asCount(o.contracts)}
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
                    <th className="num">avg ¢</th>
                    <th className="num">unreal.</th>
                    {/* Labelled gross, because it is.
                        `realized_pnl_cents` is booked only when a fill
                        *reduces* a position, priced against avg_price; fees
                        are booked on *every* fill, including ones that only
                        open. The two therefore count different sets of fills
                        and `real. − fees` is not a net figure for this
                        position — it would charge a still-open position's
                        entry fees against a closed one's proceeds. The old
                        header said "subtract it from realised", which was
                        arithmetic the operator should not perform.
                        A genuinely net per-position figure is a backend
                        change, not something to fake in the browser. */}
                    <th
                      className="num"
                      title="Realised P&amp;L from price movement only, GROSS of fees — see the note under this table for why it is not netted here."
                    >
                      real. (gross)
                    </th>
                    {/* Hard constraint: every P&L shown must be net of fees.
                        This column was declared, returned by the API and
                        rendered by nothing, so a position 21¢ down on fees
                        read as 6/100ths of a cent down. */}
                    <th
                      className="num"
                      title="Every fee this book has paid on this market, opening fills included. Not a correction to the realised column — the two count different fills."
                    >
                      fees paid
                    </th>
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
                      <td className="num">{asCount(p.contracts)}</td>
                      <td className="num">{centsNum(p.avg_price, 2)}</td>
                      <td className={pnlClass(p.unrealized_pnl_cents)}>
                        {p.unrealized_pnl_cents === null
                          ? "—"
                          : asDollars(p.unrealized_pnl_cents)}
                      </td>
                      <td className={pnlClass(p.realized_pnl_cents)}>
                        {asDollars(p.realized_pnl_cents)}
                      </td>
                      <td className="num muted">
                        {moneySign(p.fees_paid_cents) > 0 ? "−" : ""}
                        {asCentsAmount(p.fees_paid_cents)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {positions.length > 0 && (
            /* The fees column is the last of seven and lives past the right
               edge of this panel at common widths, so the caveat has to be
               stated in prose too — a tooltip on a header nobody can see is
               not a disclosure. (`.table-scroll` now shows a scrollbar and a
               fade, so the column is at least reachable.) */
            <p className="muted" style={{ marginTop: 8 }}>
              <strong>real.</strong> is gross of fees, and{" "}
              <strong>fees paid</strong> is not its correction. A fee is
              charged the instant a fill happens — including fills that only{" "}
              <em>open</em> a position — while realised P&amp;L is booked only
              when a fill <em>reduces</em> one. Subtracting one column from the
              other charges a still-open position's entry fees against a closed
              position's proceeds, so this panel does not do it and neither
              should you. The net figures the system does compute are{" "}
              <strong>Risk → today's realised P&amp;L after fees</strong> above
              (portfolio-wide, per UTC day) and the report card's per-detector
              expectancy below.
            </p>
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
                    <th className="num">price ¢</th>
                    <th className="num">size</th>
                    <th className="num">fee</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.map((f) => (
                    <tr key={f.id}>
                      <td className="muted" title={f.ts ?? undefined}>
                        {f.ts ? asClock(f.ts) : "—"}
                      </td>
                      <td className="mono">{f.ticker}</td>
                      <td className={f.action === "buy" ? "up" : "down"}>
                        {f.action} {f.side}
                      </td>
                      <td className="num">{centsNum(f.price, 2)}</td>
                      <td className="num">{asCount(f.contracts)}</td>
                      {/* The exchange bills fractional cents. Rendered as
                          dollars these read "$0.00" — a real, billed fee
                          showing as no fee at all. */}
                      <td className="num">{asCentsAmount(f.fee_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Recent decisions</h2>
            <span className="muted">
              repeats folded — a detector re-proposes the same edge every scan
            </span>
          </div>
          {decided.length === 0 ? (
            <Empty>No decisions recorded yet.</Empty>
          ) : (
            <div className="approvals compact">
              {decided.map((g) => (
                <div key={g.lead.id} className="decision-group">
                  <ApprovalCard
                    proposal={g.lead}
                    state={state}
                    onDecided={load}
                  />
                  {g.count > 1 && (
                    <p className="repeat-note">
                      ×{g.count} identical — same market, same source, same
                      outcome
                      {g.since ? `, since ${asClock(g.since)}` : ""}. Showing
                      the most recent; every one is still its own row in the
                      audit trail.
                    </p>
                  )}
                </div>
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
            {/* className="table" was missing here and only here. Without it
                none of the table styling applies and `.table .num` never
                matches, so every numeric column loses its right alignment and
                tabular figures. Invisible until the first resolution. */}
            <table className="table">
              <thead>
                <tr>
                  <th>market</th>
                  <th>route</th>
                  <th>result</th>
                  <th className="num">held</th>
                  <th className="num">avg ¢ (YES)</th>
                  <th className="num">paid ¢</th>
                  <th className="num">P&amp;L</th>
                  <th className="num">fee</th>
                  <th>when</th>
                </tr>
              </thead>
              <tbody>
                {settlements.map((s) => {
                  const held = heldSide(s.net_contracts);
                  return (
                    <tr key={`${s.ticker}-${s.route}`}>
                      <td>
                        <Link to={`/market/${s.ticker}`}>{s.ticker}</Link>
                      </td>
                      <td>{routeLabel(s.route)}</td>
                      <td>{s.result ?? "—"}</td>
                      {/* Positions are stored signed in YES-equivalents, but
                          "-12.00" is not how a trader reads a holding — the
                          positions table above already says "12 NO" and this
                          was the one place the internal convention leaked. */}
                      <td className="num">
                        {held.size}{" "}
                        <span className={held.side === "yes" ? "up" : "down"}>
                          {held.side}
                        </span>
                      </td>
                      {/* Both are YES *prices* in dollars, not cents money —
                          a cents formatter would divide them by 100 again. */}
                      <td className="num">{centsNum(s.avg_price, 2)}</td>
                      <td className="num">{centsNum(s.settled_yes_value, 2)}</td>
                      <td className={pnlClass(s.realized_pnl_cents)}>
                        {asSignedCents(s.realized_pnl_cents)}
                      </td>
                      <td className="num muted">{asCentsAmount(s.fee_cents)}</td>
                      <td title={s.settled_at ?? undefined}>
                        {s.settled_at ? asClock(s.settled_at) : "—"}
                      </td>
                    </tr>
                  );
                })}
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
            Nothing recorded yet.
            <DetectorNote names={detectors} />
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
                  <th
                    className="num"
                    title="an ordering for attention — not cents, not a probability, and a 70 is not twice a 35"
                  >
                    score
                  </th>
                  <th className="num">conf</th>
                  <th className="num">seen</th>
                  <th>why</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((sg) => (
                  <tr key={sg.id}>
                    <td className="muted" title={sg.created_at ?? undefined}>
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
                    <td className={pnlClass(sg.net_edge_cents)}>
                      {moneySign(sg.net_edge_cents) === 0
                        ? "—"
                        : asSignedCents(sg.net_edge_cents)}
                    </td>
                    <td
                      className="num muted"
                      title="an ordering for attention — not cents, not a probability"
                    >
                      {evidenceScore(sg.evidence)}
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
                    {/* Wraps rather than truncating. The caveat that stops a
                        99.4 being read as a 99.4% chance sits at the END of
                        the rationale, so an ellipsis always ate exactly the
                        part that mattered. */}
                    <td className="audit-detail" title={sg.rationale ?? undefined}>
                      {/* The inner block exists to carry a height cap: a
                          `max-height` on a `<td>` is ignored by the table
                          layout algorithm, so it has to sit on a block child.
                          Same shape as `.audit-json` in the audit trail. */}
                      <div className="audit-text">{sg.rationale}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ReportCard />

      <NewsPanel news={news} />

      <EnginePanel engine={engine} />

      <section className="panel" style={{ marginTop: 12 }}>
        <h2>Audit trail</h2>
        <p className="muted">
          Append-only. Every proposal, decision, order, and fill. Times on this
          page are your browser's local clock — the daily loss limit resets on
          the <strong>UTC</strong> day, so the two can differ; hover a time for
          the exact timestamp.
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
                    <td className="muted" title={entry.ts ?? undefined}>
                      {entry.ts ? asClock(entry.ts) : "—"}
                    </td>
                    <td className="mono">{entry.kind}</td>
                    <td className="mono">{entry.ticker ?? "—"}</td>
                    <td>{entry.actor}</td>
                    {/* An append-only record you cannot read is not a record.
                        This column was ellipsised to one line.

                        Indented rather than dumped on one: a five-leg
                        proposal's wire form is the record most worth reading
                        and was the least readable — one unbroken string the
                        operator had to parse character by character to check
                        a side against a price. Nothing is truncated or
                        collapsed; the `<pre>` is capped in height and scrolls
                        so a 40-line payload cannot push the next row off the
                        screen. */}
                    <td className="audit-detail">
                      {entry.payload ? (
                        <pre className="audit-json">
                          {JSON.stringify(entry.payload, null, 2)}
                        </pre>
                      ) : (
                        ""
                      )}
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
