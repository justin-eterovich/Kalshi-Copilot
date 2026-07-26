import { useEffect, useState } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import MarketPage from "./MarketPage";
import Screener from "./Screener";
import SystemPanel from "./SystemPanel";
import Trades from "./Trades";
import {
  api,
  routeLabel,
  type Health,
  type SystemStatus,
  type TradingState,
} from "./api";

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
  const [trading, setTrading] = useState<TradingState | null>(null);
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
      try {
        setTrading(await api.tradingState());
      } catch {
        /* trading state is additive; the header degrades without it */
      }
    };
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const live = system?.live_trading_armed ?? false;
  const pending = trading?.pending_proposals ?? 0;
  const path = location.pathname;
  const onSystem = path.startsWith("/system");
  const onTrades = path.startsWith("/trades");
  const onMarkets = !onSystem && !onTrades;

  // The approval queue has to be visible from anywhere: a proposal expires in
  // seconds, and a badge you only see on one page is a badge you miss.
  useEffect(() => {
    document.title = pending > 0 ? `(${pending}) kalshi-copilot` : "kalshi-copilot";
  }, [pending]);

  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          kalshi<span className="dot">·</span>copilot
        </Link>

        <nav className="tabs">
          <Link to="/" className={onMarkets ? "tab active" : "tab"}>
            markets
          </Link>
          <Link to="/trades" className={onTrades ? "tab active" : "tab"}>
            trades
            {pending > 0 && <span className="badge">{pending}</span>}
          </Link>
          <Link to="/system" className={onSystem ? "tab active" : "tab"}>
            system
          </Link>
        </nav>

        <span className="spacer" />

        {trading && (
          <Pill ok={!trading.real_money} warn={trading.real_money}>
            {routeLabel(trading.execution_route)}
          </Pill>
        )}
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
          <Route path="/trades" element={<Trades />} />
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
