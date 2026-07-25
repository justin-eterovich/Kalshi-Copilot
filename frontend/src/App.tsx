import { useEffect, useState } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import MarketPage from "./MarketPage";
import Screener from "./Screener";
import SystemPanel from "./SystemPanel";
import { api, type Health, type SystemStatus } from "./api";

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

export default function App() {
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const location = useLocation();

  useEffect(() => {
    const load = async () => {
      try {
        const [s, h] = await Promise.all([api.system(), api.health()]);
        setSystem(s);
        setHealth(h);
      } catch {
        /* the active panel surfaces the error */
      }
    };
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const live = system?.live_trading_armed ?? false;
  const onSystem = location.pathname.startsWith("/system");

  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          kalshi<span className="dot">·</span>copilot
        </Link>

        <nav className="tabs">
          <Link to="/" className={onSystem ? "tab" : "tab active"}>
            markets
          </Link>
          <Link to="/system" className={onSystem ? "tab active" : "tab"}>
            system
          </Link>
        </nav>

        <span className="spacer" />

        {system && (
          <>
            <Pill ok={!live} warn={live}>
              {system.environment}
            </Pill>
            <Pill
              ok={system.trading_mode === "paper"}
              warn={system.trading_mode === "live"}
            >
              {system.trading_mode}
            </Pill>
          </>
        )}
        {health && <Pill ok={health.status === "ok"}>{health.status}</Pill>}
      </header>

      <main>
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

        <Routes>
          <Route path="/" element={<Screener />} />
          <Route path="/market/:ticker" element={<MarketPage />} />
          <Route
            path="/system"
            element={<SystemPanel system={system} health={health} />}
          />
        </Routes>
      </main>

      <footer>
        LAN-only. No order reaches Kalshi without your explicit per-trade approval.
      </footer>
    </div>
  );
}
