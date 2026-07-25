import { useCallback, useEffect, useState } from "react";
import {
  api,
  asCents,
  asCount,
  asTimeToClose,
  type CatalogStats,
  type MarketsPage,
} from "./api";

const SORTS: { key: string; label: string }[] = [
  { key: "volume_24h", label: "24h vol" },
  { key: "volume", label: "volume" },
  { key: "open_interest", label: "OI" },
  { key: "close_time", label: "close" },
  { key: "last_price", label: "price" },
  { key: "first_seen_at", label: "listed" },
];

const PAGE_SIZE = 50;

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={warn ? "stat-value warn" : "stat-value"}>{value}</div>
    </div>
  );
}

export default function CatalogBrowser() {
  const [stats, setStats] = useState<CatalogStats | null>(null);
  const [page, setPage] = useState<MarketsPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("active");
  const [sort, setSort] = useState("volume_24h");
  const [offset, setOffset] = useState(0);

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.catalogStats());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const loadMarkets = useCallback(async () => {
    try {
      setPage(
        await api.markets({
          q: query || undefined,
          status: status || undefined,
          sort,
          limit: PAGE_SIZE,
          offset,
        }),
      );
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [query, status, sort, offset]);

  // Stats poll on their own cadence; the table reloads when filters change.
  useEffect(() => {
    loadStats();
    const id = setInterval(loadStats, 5000);
    return () => clearInterval(id);
  }, [loadStats]);

  useEffect(() => {
    const id = setTimeout(loadMarkets, 200); // debounce typing
    return () => clearTimeout(id);
  }, [loadMarkets]);

  const total = page?.total ?? 0;
  const showingTo = Math.min(offset + PAGE_SIZE, total);

  return (
    <div>
      {error && <div className="banner">API error: {error}</div>}

      <section className="panel" style={{ marginBottom: 12 }}>
        <h2>Ingest scoreboard</h2>
        {stats ? (
          <div className="stats">
            <Stat label="markets" value={stats.markets.toLocaleString()} />
            <Stat label="active" value={stats.markets_active.toLocaleString()} />
            <Stat label="events" value={stats.events.toLocaleString()} />
            <Stat label="candles" value={stats.candles.toLocaleString()} />
            <Stat label="tape rows" value={stats.tape.toLocaleString()} />
            <Stat label="book snaps" value={stats.orderbook_snaps.toLocaleString()} />
            <Stat
              label="trade lag"
              value={
                stats.trade_lag_sec === null
                  ? "no trades yet"
                  : `${stats.trade_lag_sec.toFixed(0)}s`
              }
              warn={stats.trade_lag_sec === null || stats.trade_lag_sec > 120}
            />
          </div>
        ) : (
          <p className="muted">loading…</p>
        )}
      </section>

      {stats && stats.recent_listings.length > 0 && (
        <section className="panel" style={{ marginBottom: 12 }}>
          <h2>Newest listings</h2>
          <p className="muted" style={{ marginTop: -6 }}>
            A market nobody has looked at yet is the cheapest edge on the platform.
          </p>
          <div className="chips">
            {stats.recent_listings.map((m) => (
              <span className="chip" key={m.ticker} title={m.title ?? ""}>
                {m.ticker}
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="panel">
        <h2>Catalog</h2>

        <div className="controls">
          <input
            className="input"
            placeholder="search ticker or title…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOffset(0);
            }}
          />
          <select
            className="input"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="active">active</option>
            <option value="closed">closed</option>
            <option value="settled">settled</option>
            <option value="">any status</option>
          </select>
          <select
            className="input"
            value={sort}
            onChange={(e) => {
              setSort(e.target.value);
              setOffset(0);
            }}
          >
            {SORTS.map((s) => (
              <option value={s.key} key={s.key}>
                sort: {s.label}
              </option>
            ))}
          </select>
        </div>

        {page && page.markets.length === 0 ? (
          <p className="muted">
            No markets stored yet. The ingest service performs a full catalog sync
            on first run — give it a minute, then refresh.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>ticker</th>
                  <th>title</th>
                  <th className="num">bid</th>
                  <th className="num">ask</th>
                  <th className="num">last</th>
                  <th className="num">spread</th>
                  <th className="num">24h vol</th>
                  <th className="num">OI</th>
                  <th className="num">closes</th>
                </tr>
              </thead>
              <tbody>
                {page?.markets.map((m) => (
                  <tr key={m.ticker}>
                    <td className="mono">{m.ticker}</td>
                    <td className="title" title={m.title ?? ""}>
                      {m.yes_sub_title || m.title || "—"}
                    </td>
                    <td className="num">{asCents(m.yes_bid)}</td>
                    <td className="num">{asCents(m.yes_ask)}</td>
                    <td className="num accent">{asCents(m.last_price)}</td>
                    <td className="num">{asCents(m.spread)}</td>
                    <td className="num">{asCount(m.volume_24h)}</td>
                    <td className="num">{asCount(m.open_interest)}</td>
                    <td className="num">{asTimeToClose(m.hours_to_close)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {total > 0 && (
          <div className="pager">
            <button
              className="btn"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              ← prev
            </button>
            <span className="muted">
              {offset + 1}–{showingTo} of {total.toLocaleString()}
            </span>
            <button
              className="btn"
              disabled={showingTo >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              next →
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
