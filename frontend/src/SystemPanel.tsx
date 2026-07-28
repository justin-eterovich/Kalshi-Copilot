import type { Health, SystemStatus } from "./api";

/**
 * Build progress.
 *
 * This list is hand-maintained and there is no endpoint behind it, so it rots
 * silently: it sat at M6 while M7, M8 and M9 were deployed and serving, and
 * the dashboard is the operator's only status surface. **It must be updated
 * in the same change that finishes a milestone**, against the status table in
 * README.md, which is the source of truth.
 */
const MILESTONES: [string, string, boolean][] = [
  ["M0", "Scaffold, compose stack, fees module", true],
  ["M1", "Ingest + storage", true],
  ["M2", "Dashboard core", true],
  ["M3", "HITL approval + execution rail", true],
  ["M4", "Detectors wave 1", true],
  // "in-tab alerts", not "PWA notifications": Web Push needs a secure
  // context and the dashboard is plain HTTP on a LAN address by design.
  ["M5", "Risk layer + in-tab alerts", true],
  ["M6", "BTC vol engine + detectors wave 2", true],
  ["M7", "Weather engine", true],
  ["M8", "News + catalyst engine", true],
  ["M9", "Backtester + hardening", true],
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
      {/* The fee-unverified banner used to live here, on the third tab. It is
          now in App.tsx beside the safe-mode banner, where every page sees
          it — it is the state in which nothing can be proposed at all. */}

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
              <Row k="base maker rate">{system.fees.base_maker_rate}</Row>
              <Row k="series listed">{system.fees.series_listed}</Row>
              <Row k="verified">
                <Pill
                  ok={!!system.fees.verified_on}
                  warn={!system.fees.verified_on}
                >
                  {system.fees.verified_on ?? "never"}
                </Pill>
              </Row>
              <Row k="fee-free series">
                {system.fees.fee_free_series.length || "none"}
              </Row>
              <Row k="default safe">
                <Pill ok={system.fees.default_is_safe} warn={!system.fees.default_is_safe}>
                  {system.fees.default_is_safe ? "yes" : "NO — unlisted series refused"}
                </Pill>
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
          <p className="muted" style={{ marginTop: 8 }}>
            Built and tested is not the same as running: every detector ships
            disabled, and several engines need history before they price
            anything.
          </p>
        </section>
      </div>
    </div>
  );
}
