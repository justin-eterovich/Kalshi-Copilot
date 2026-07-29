import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  asCount,
  asTimeToClose,
  centsNum,
  moneySign,
  type CatalogStats,
  type MarketRow,
  type MarketsPage,
} from "./api";
import { FEED_STATUS_TITLE, useLiveFeed } from "./useLiveFeed";

const SORTS = [
  { key: "volume_24h", label: "24h vol" },
  { key: "volume", label: "volume" },
  { key: "open_interest", label: "open interest" },
  // The API parameter is still `liquidity`; the *label* is not, because the
  // number is an attention ranking and calling it liquidity overpromised on
  // markets with no bid at all. See the column header below.
  { key: "liquidity", label: "attention score" },
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

const ATTENTION_TITLE =
  "An ordering for attention, not a measure of tradability. 24h volume and " +
  "open interest carry half the weight between them, so a market with no bid " +
  "at any price still scored 50/100. Read the bid column before reading this one.";

const NO_SCORE_TITLE =
  "No score. The server refuses to rank a market for attention unless both " +
  "sides are being quoted inside (0, 1) — a populated 0.000000 is not a quote.";

const NO_BID_TITLE =
  "No bid at any price: nobody will buy this from you right now, whatever the " +
  "volume history says. This is why there is no attention score to show.";

/**
 * @param noBid nobody is bidding. Half the score's weight comes from volume
 *   and open interest, which are *history*, so a dead one-sided book used to
 *   score ~50 and read as "medium liquidity" — measured on KXGOVAK-26-NDAH,
 *   bid 0.00 / ask 0.98, scored 50.5.
 *
 *   The server now withholds the score entirely in that case, which is the
 *   better fix and makes this the `score === null` branch rather than the
 *   bar branch. The flag is kept, and moved, for two reasons: a bare "—"
 *   says *that* there is no number without saying *why*, and "no bid" is
 *   precisely the operator's question; and a live tick can still zero a bid
 *   under a score fetched a moment earlier, which is the one path where the
 *   flag and the bar appear together.
 */
function ScoreBar({ score, noBid }: { score: number | null; noBid: boolean }) {
  if (score === null) {
    return (
      <span className="score-cell" title={noBid ? NO_BID_TITLE : NO_SCORE_TITLE}>
        <span className="muted">—</span>
        {noBid && <span className="score-flag">no bid</span>}
      </span>
    );
  }
  // Clamped at both ends. Only the top was, so a negative score would emit
  // `width:-450%` — an invalid declaration the browser drops, leaving a
  // full-looking bar beside a number reading "-450". The server clamps too;
  // this is the half that was missing when the -450 bug last happened.
  const width = Math.max(0, Math.min(100, score));
  return (
    // The flag sits OUTSIDE `.score`: the bar is positioned as a percentage
    // of that box, so widening it for some rows and not others would make
    // two equal scores draw two different bars.
    <span
      className="score-cell"
      title={noBid ? `${ATTENTION_TITLE} ${NO_BID_TITLE}` : ATTENTION_TITLE}
    >
      <span className="score">
        <span className="score-bar" style={{ width: `${width}%` }} />
        <span className="score-num">{score.toFixed(0)}</span>
      </span>
      {noBid && <span className="score-flag">no bid</span>}
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
                  <th
                    className="num"
                    title="ask − bid on the summary quote. A NEGATIVE value is a crossed quote: stale, not tight. Flagged in the rows below."
                  >
                    spr ¢
                  </th>
                  <th className="num">24h vol</th>
                  <th className="num">OI</th>
                  <th title={ATTENTION_TITLE}>attention</th>
                  <th className="num">closes</th>
                </tr>
              </thead>
              <tbody>
                {page?.markets.map((m) => {
                  const merged = { ...m, ...live[m.ticker] } as MarketRow;
                  const isLive = live[m.ticker] !== undefined;
                  // A crossed book (bid above ask) is real stored data, not a
                  // display error — it is what a partial ticker snapshot
                  // leaves behind when one side goes stale. Rendered plain it
                  // is one minus sign away from a genuinely tight spread, and
                  // the tightest rows are exactly what an operator scanning
                  // this column is hunting for.
                  //
                  // Read from the server's flag, not derived here. It is
                  // decided once in Decimal; a browser-side `Number(bid) >
                  // Number(ask)` would be the float parse the money path
                  // exists to avoid. `null` means one-sided (the question was
                  // never asked), and `null` must not read as `false` — but a
                  // one-sided quote already renders "—" in the leg columns,
                  // so it needs no flag of its own here.
                  //
                  // From `m`, not `merged`: the flag and `m.spread` describe
                  // the same polled row, while `merged` may carry a newer
                  // websocket tick. Mixing them would flag a spread that is
                  // no longer the one shown.
                  const crossed = m.quote_crossed === true;
                  // A *present* bid of zero means nobody is buying at any
                  // price. A null bid means we have no bid at all, which is a
                  // different fact and must not be announced as this one.
                  const bid = merged.yes_bid;
                  const noBid = bid !== null && bid !== "" && moneySign(bid) === 0;
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
                      <td
                        className={crossed ? "num warn" : "num"}
                        title={
                          crossed
                            ? "Crossed quote: the bid is above the ask. These are Kalshi's summary quote fields, not the order book, and the same payload reports negative bid sizes on this exchange — so treat BOTH prices as unreliable, not just the one that looks wrong. Not a tight market, not free money. Open the market to read the live book."
                            : undefined
                        }
                      >
                        {centsNum(m.spread)}
                        {crossed && <span className="crossed-flag">crossed</span>}
                      </td>
                      <td className="num">{asCount(merged.volume_24h)}</td>
                      <td className="num">{asCount(merged.open_interest)}</td>
                      <td>
                        <ScoreBar score={m.liquidity_score} noBid={noBid} />
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
