import { useEffect, useState } from "react";

/** Shape of GET /api/system. */
interface SystemStatus {
  environment: string;
  trading_mode: string;
  live_trading_armed: boolean;
  kill_switch: boolean;
  credentials_present: boolean;
  enabled_detectors: string[];
  heartbeats: Record<string, boolean>;
  fees: {
    verified_on: string | null;
    schedule_revision: string | null;
    base_taker_rate: number;
    maker_rate_fraction: number;
    unverified_categories: string[];
  };
  endpoints: { rest: string; ws: string };
}

interface Health {
  status: string;
  checks: Record<string, boolean>;
}

const MILESTONES: [string, string, boolean][] = [
  ["M0", "Scaffold, compose stack, fees module", true],
  ["M1", "Ingest + storage", false],
  ["M2", "Dashboard core", false],
  ["M3", "HITL approval + execution rail", false],
  ["M4", "Detectors wave 1", false],
  ["M5", "Risk layer + PWA notifications", false],
  ["M6", "BTC engine + detectors wave 2", false],
  ["M7", "Weather engine", false],
  ["M8", "News + catalyst engine", false],
  ["M9", "Backtester + hardening", false],
];

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{children}</span>
    </div>
  );
}

function Pill({ ok, warn, children }: { ok?: boolean; warn?: boolean; children: React.ReactNode }) {
  const cls = ok ? "pill ok" : warn ? "pill warn" : "pill bad";
  return <span className={cls}>{children}</span>;
}

export default function App() {
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [s, h] = await Promise.all([
          fetch("/api/system").then((r) => r.json()),
          fetch("/api/health").then((r) => r.json()),
        ]);
        setSystem(s);
        setHealth(h);
        setError(null);
      } catch (e) {
        setError(String(e));
      }
    };
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const live = system?.live_trading_armed ?? false;

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">
          kalshi<span className="dot">·</span>copilot
        </span>
        {system && (
          <>
            <Pill ok={!live} warn={live}>
              {system.environment}
            </Pill>
            <Pill ok={system.trading_mode === "paper"} warn={system.trading_mode === "live"}>
              {system.trading_mode}
            </Pill>
          </>
        )}
        <span className="spacer" />
        {health && (
          <Pill ok={health.status === "ok"}>{health.status}</Pill>
        )}
      </header>

      <main>
        {error && <div className="banner">Cannot reach the API: {error}</div>}

        <div className={live ? "banner" : "banner safe"}>
          {live ? (
            <>
              <strong>Live trading armed.</strong> Real orders are possible. Every
              single one still requires your explicit per-trade approval — there is
              no auto-trade path in this system.
            </>
          ) : (
            <>
              <strong>Safe mode.</strong> No real order can reach Kalshi. Live
              trading needs <code>KALSHI_ENV=prod</code> and{" "}
              <code>LIVE_TRADING=true</code>, and every trade is still approved by
              hand.
            </>
          )}
        </div>

        {system?.fees.verified_on === null && (
          <div className="banner">
            <strong>Fee schedule unverified.</strong> Run{" "}
            <code>python scripts/refresh_fee_schedule.py</code> to confirm the
            multipliers against Kalshi's official PDF. Categories with an unknown
            multiplier
            {system.fees.unverified_categories.length > 0 && (
              <> ({system.fees.unverified_categories.join(", ")})</>
            )}{" "}
            are excluded from proposals rather than priced with a guess.
          </div>
        )}

        <div className="grid">
          <section className="panel">
            <h2>Services</h2>
            {health &&
              Object.entries(health.checks).map(([name, ok]) => (
                <Row k={name} key={name}>
                  <Pill ok={ok}>{ok ? "up" : "down"}</Pill>
                </Row>
              ))}
            {system &&
              Object.entries(system.heartbeats).map(([name, ok]) => (
                <Row k={name} key={name}>
                  <Pill ok={ok}>{ok ? "beating" : "silent"}</Pill>
                </Row>
              ))}
          </section>

          <section className="panel">
            <h2>Safety posture</h2>
            {system && (
              <>
                <Row k="environment">{system.environment}</Row>
                <Row k="trading mode">{system.trading_mode}</Row>
                <Row k="live armed">
                  <Pill ok={!system.live_trading_armed} warn={system.live_trading_armed}>
                    {system.live_trading_armed ? "yes" : "no"}
                  </Pill>
                </Row>
                <Row k="kill switch">
                  <Pill ok={!system.kill_switch} warn={system.kill_switch}>
                    {system.kill_switch ? "engaged" : "off"}
                  </Pill>
                </Row>
                <Row k="credentials">
                  <Pill ok={system.credentials_present} warn={!system.credentials_present}>
                    {system.credentials_present ? "loaded" : "missing"}
                  </Pill>
                </Row>
              </>
            )}
          </section>

          <section className="panel">
            <h2>Fees</h2>
            {system && (
              <>
                <Row k="base taker rate">{system.fees.base_taker_rate}</Row>
                <Row k="maker fraction">{system.fees.maker_rate_fraction}</Row>
                <Row k="verified">
                  <Pill ok={!!system.fees.verified_on} warn={!system.fees.verified_on}>
                    {system.fees.verified_on ?? "never"}
                  </Pill>
                </Row>
                <Row k="unverified cats">
                  {system.fees.unverified_categories.join(", ") || "none"}
                </Row>
              </>
            )}
          </section>

          <section className="panel">
            <h2>Detectors</h2>
            {system && system.enabled_detectors.length > 0 ? (
              system.enabled_detectors.map((d) => (
                <Row k={d} key={d}>
                  <Pill ok>on</Pill>
                </Row>
              ))
            ) : (
              <p style={{ color: "var(--text-faint)", margin: 0 }}>
                None enabled. Turn them on one at a time in <code>config.yaml</code>{" "}
                and let the report card earn your trust first.
              </p>
            )}
          </section>

          <section className="panel">
            <h2>Endpoints</h2>
            {system && (
              <>
                <Row k="rest">{system.endpoints.rest}</Row>
                <Row k="ws">{system.endpoints.ws}</Row>
              </>
            )}
          </section>

          <section className="panel">
            <h2>Build progress</h2>
            <ul className="milestones">
              {MILESTONES.map(([id, label, done]) => (
                <li key={id} className={done ? "done" : ""}>
                  <span className="mark">{done ? "✓" : "·"}</span>
                  <span>
                    {id} — {label}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </main>

      <footer>
        LAN-only. No order reaches Kalshi without your explicit per-trade approval.
      </footer>
    </div>
  );
}
