import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  asCount,
  asTimeToClose,
  centsNum,
  type CatalogStats,
  type MarketRow,
  type MarketsPage,
} from "./api";
import { FEED_STATUS_TITLE, useLiveFeed } from "./useLiveFeed";

const SORTS = [
  { key: "volume_24h", label: "24h vol" },
  { key: "volume", label: "volume" },
  { key: "open_interest", label: "open interest" },
  { key: "liquidity", label: "liquidity" },
  { key: "close_time", label: "close time" },
  { key: "last_price", label: "price" },
  { key: "first_seen_at", label: "newest listing" },
];

const PAGE_SIZE = 60;

function Stat({
  label,
  value,
  warn,
  title,
}: {
  label: string;
  value: string;
  warn?: boolean;
  title?: string;
}) {
  return (
    <div className="stat" title={title}>
      <div className="stat-label">{label}</div>
      <div className={warn ? "stat-value warn" : "stat-value"}>{value}</div>
    </div>
  );
}

function ScoreBar({ score }: { score: number | null }) {
  if (score === null) return <span className="muted">—</span>;
  // Clamped at both ends. Only the top was, so a negative score would emit
  // `width:-450%` — an invalid declaration the browser drops, leaving a
  // full-looking bar beside a number reading "-450". The server clamps too;
  // this is the half that was missing when the -450 bug last happened.
  const width = Math.max(0, Math.min(100, score));
  return (
    <span className="score">
      <span className="score-bar" style={{ width: `${width}%` }} />
      <span className="score-num">{score.toFixed(0)}</span>
    </span>
  );
}

export default function Screener() {
  const [stats, setStats] = useState<CatalogStats | null>(null);
  const [page, setPage] = useState<MarketsPage | null>(null);
  const [categories, setCategories] = useState<{ category: string; count: number }[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("active");
  const [sort, setSort] = useState("volume_24h");
  const [maxSpread, setMaxSpread] = useState("");
  const [offset, setOffset] = useState(0);

  // Live prices merged over the fetched page, keyed by ticker.
  const [live, setLive] = useState<Record<string, Partial<MarketRow>>>({});
  const tickers = useMemo(() => page?.markets.map((m) => m.ticker) ?? [], [page]);

  const feedStatus = useLiveFeed(tickers, (message) => {
    if (message.channel !== "copilot:ticks") return;
    const data = message.data as Record<string, string>;
    if (!data.ticker) return;
    setLive((prev) => ({ ...prev, [data.ticker]: { ...prev[data.ticker], ...data } }));
  });

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
          category: category || undefined,
          status: status || undefined,
          sort,
          max_spread: maxSpread || undefined,
          limit: PAGE_SIZE,
          offset,
        }),
      );
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [query, category, status, sort, maxSpread, offset]);

  useEffect(() => {
    loadStats();
    api.categories().then((r) => setCategories(r.categories)).catch(() => {});
    const id = setInterval(loadStats, 8000);
    return () => clearInterval(id);
  }, [loadStats]);

  // Debounce so typing does not fire a query per keystroke.
  const first = useRef(true);
  useEffect(() => {
    const delay = first.current ? 0 : 250;
    first.current = false;
    const id = setTimeout(loadMarkets, delay);
    return () => clearTimeout(id);
  }, [loadMarkets]);

  const total = page?.total ?? 0;
  const showingTo = Math.min(offset + PAGE_SIZE, total);

  const resetPaging = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value);
    setOffset(0);
  };

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
            <Stat
              label="no category"
              value={stats.markets_uncategorised.toLocaleString()}
              warn={stats.markets_uncategorised > 0}
            />
            {/* "socket", not "feed": this is the relay connection, and it
                stays green if ingest stops publishing behind it. */}
            <Stat
              label="socket"
              value={feedStatus}
              warn={feedStatus !== "live"}
              title={FEED_STATUS_TITLE}
            />
          </div>
        ) : (
          <p className="muted">loading…</p>
        )}
      </section>

      <section className="panel">
        <h2>Screener</h2>

        <div className="controls">
          <input
            className="input"
            placeholder="search ticker or title…"
            value={query}
            onChange={(e) => resetPaging(setQuery)(e.target.value)}
          />
          <select
            className="input"
            value={category}
            onChange={(e) => resetPaging(setCategory)(e.target.value)}
          >
            <option value="">all categories</option>
            {categories.map((c) => (
              <option key={c.category} value={c.category}>
                {c.category} ({c.count.toLocaleString()})
              </option>
            ))}
          </select>
          <select
            className="input"
            value={status}
            onChange={(e) => resetPaging(setStatus)(e.target.value)}
          >
            <option value="active">active</option>
            <option value="closed">closed</option>
            <option value="settled">settled</option>
            <option value="">any status</option>
          </select>
          <select
            className="input"
            value={sort}
            onChange={(e) => resetPaging(setSort)(e.target.value)}
          >
            {SORTS.map((s) => (
              <option value={s.key} key={s.key}>
                sort: {s.label}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={maxSpread}
            onChange={(e) => resetPaging(setMaxSpread)(e.target.value)}
          >
            <option value="">any spread</option>
            <option value="0.01">≤ 1¢</option>
            <option value="0.02">≤ 2¢</option>
            <option value="0.05">≤ 5¢</option>
            <option value="0.10">≤ 10¢</option>
          </select>
        </div>

        {page && page.markets.length === 0 ? (
          <p className="muted">
            No markets match. The ingest service performs a full catalog sync on
            first run — if this is a fresh install, give it a minute.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>ticker</th>
                  <th>market</th>
                  {/* Unit in the header rather than on every cell: a bare
                      "0.4" is ambiguous between 0.4¢ and $0.40 to anyone who
                      has not read the source. */}
                  <th className="num">bid ¢</th>
                  <th className="num">ask ¢</th>
                  <th className="num">last ¢</th>
                  <th className="num">spr ¢</th>
                  <th className="num">24h vol</th>
                  <th className="num">OI</th>
                  <th title="an ordering for attention, not a probability">
                    liquidity
                  </th>
                  <th className="num">closes</th>
                </tr>
              </thead>
              <tbody>
                {page?.markets.map((m) => {
                  const merged = { ...m, ...live[m.ticker] } as MarketRow;
                  const isLive = live[m.ticker] !== undefined;
                  return (
                    <tr key={m.ticker} className={isLive ? "flash" : ""}>
                      <td className="mono">
                        <Link to={`/market/${encodeURIComponent(m.ticker)}`}>
                          {m.ticker}
                        </Link>
                      </td>
                      <td className="title" title={m.title ?? ""}>
                        {m.yes_sub_title || m.title || "—"}
                      </td>
                      <td className="num">{centsNum(merged.yes_bid)}</td>
                      <td className="num">{centsNum(merged.yes_ask)}</td>
                      <td className="num accent">{centsNum(merged.last_price)}</td>
                      <td className="num">{centsNum(m.spread)}</td>
                      <td className="num">{asCount(merged.volume_24h)}</td>
                      <td className="num">{asCount(merged.open_interest)}</td>
                      <td>
                        <ScoreBar score={m.liquidity_score} />
                      </td>
                      <td className="num">{asTimeToClose(m.hours_to_close)}</td>
                    </tr>
                  );
                })}
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
