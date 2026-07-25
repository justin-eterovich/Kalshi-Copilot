import type { Health, SystemStatus } from "./api";

const MILESTONES: [string, string, boolean][] = [
  ["M0", "Scaffold, compose stack, fees module", true],
  ["M1", "Ingest + storage", true],
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

export default function SystemPanel({
  system,
  health,
}: {
  system: SystemStatus | null;
  health: Health | null;
}) {
  return (
    <div>
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

      {system && !system.credentials_present && (
        <div className="banner">
          <strong>No Kalshi credentials.</strong> REST public market data still
          syncs the catalog, but the websocket requires authentication even for
          public channels — so no live tape, book, or candles until a key is
          configured. See the README runbook.
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
                <Pill
                  ok={!system.live_trading_armed}
                  warn={system.live_trading_armed}
                >
                  {system.live_trading_armed ? "yes" : "no"}
                </Pill>
              </Row>
              <Row k="kill switch">
                <Pill ok={!system.kill_switch} warn={system.kill_switch}>
                  {system.kill_switch ? "engaged" : "off"}
                </Pill>
              </Row>
              <Row k="credentials">
                <Pill
                  ok={system.credentials_present}
                  warn={!system.credentials_present}
                >
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
                <Pill
                  ok={!!system.fees.verified_on}
                  warn={!system.fees.verified_on}
                >
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
            <p className="muted">
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
    </div>
  );
}
