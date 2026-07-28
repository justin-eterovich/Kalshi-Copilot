import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import OrderBookLadder from "./OrderBookLadder";
import PriceChart from "./PriceChart";
import TapeView from "./TapeView";
import TradeTicket from "./TradeTicket";
import {
  api,
  asClock,
  asCount,
  asTimeToClose,
  centsNum,
  type Candle,
  type MarketDetail,
  type MarketRow,
  type OrderbookResponse,
  type Trade,
  type TradingState,
} from "./api";
import { FEED_STATUS_TITLE, useLiveFeed } from "./useLiveFeed";

const PERIODS = [
  { label: "1m", sec: 60, lookback: 12 },
  { label: "1h", sec: 3600, lookback: 24 * 14 },
  { label: "1d", sec: 86400, lookback: 24 * 180 },
];

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{children}</span>
    </div>
  );
}

export default function MarketPage() {
  const { ticker = "" } = useParams();

  const [market, setMarket] = useState<MarketDetail | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [book, setBook] = useState<OrderbookResponse | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [siblings, setSiblings] = useState<MarketRow[]>([]);
  const [period, setPeriod] = useState(PERIODS[0]);
  const [error, setError] = useState<string | null>(null);
  const [chartNote, setChartNote] = useState<string | null>(null);
  const [candleSource, setCandleSource] = useState<string | null>(null);
  const [quoteAt, setQuoteAt] = useState<Date | null>(null);
  const [trading, setTrading] = useState<TradingState | null>(null);

  const watch = useMemo(() => (ticker ? [ticker] : []), [ticker]);

  const feedStatus = useLiveFeed(watch, (message) => {
    if (message.channel !== "copilot:ticks") return;
    const data = message.data as Record<string, string>;
    if (data.ticker !== ticker) return;
    setMarket((prev) => (prev ? { ...prev, ...data } : prev));
  });

  /**
   * The headline quote.
   *
   * Polled, not fetched once. It used to update only on websocket ticks — and
   * the websocket needs credentials even for public channels, while the whole
   * point of this page is that it works without a key. So the default state
   * was a 30px accent price frozen at page load, sitting directly above a
   * ladder that was five seconds fresh, with only a small "offline" pill to
   * say so.
   */
  const loaded = useRef(false);

  const loadQuote = useCallback(async () => {
    if (!ticker) return;
    try {
      setMarket(await api.market(ticker));
      loaded.current = true;
      setQuoteAt(new Date());
      setError(null);
    } catch (e) {
      // Only fail the page outright if there was never anything to show. Now
      // that this polls, a single transient error must not replace a working
      // page with a banner.
      if (!loaded.current) setError(String(e));
    }
  }, [ticker]);

  const loadSiblings = useCallback(() => {
    if (!ticker) return;
    api.siblings(ticker).then((r) => setSiblings(r.markets)).catch(() => {});
  }, [ticker]);

  const loadCandles = useCallback(async () => {
    if (!ticker) return;
    try {
      const result = await api.candles(ticker, period.sec, period.lookback);
      setCandles(result.candles);
      setCandleSource(result.source);
      setChartNote(
        result.candles.length === 0
          ? "No candles for this window. Kalshi returns none for markets that have never traded."
          : null,
      );
    } catch {
      setCandleSource(null);
      setChartNote("Could not load candles.");
    }
  }, [ticker, period]);

  const loadDepthAndTape = useCallback(async () => {
    if (!ticker) return;
    api.orderbook(ticker).then(setBook).catch(() => setBook(null));
    api.tape(ticker, 60).then((r) => setTrades(r.trades)).catch(() => {});
  }, [ticker]);

  useEffect(() => {
    loaded.current = false;
    loadQuote();
    // Same cadence as the book and tape below it, so the page is internally
    // consistent rather than two ages of data stacked on top of each other.
    const id = setInterval(loadQuote, 5000);
    return () => clearInterval(id);
  }, [loadQuote]);

  useEffect(() => {
    loadSiblings();
  }, [loadSiblings]);

  useEffect(() => {
    loadCandles();
  }, [loadCandles]);

  useEffect(() => {
    loadDepthAndTape();
    // Book and tape read through to REST, so poll modestly.
    const id = setInterval(loadDepthAndTape, 5000);
    return () => clearInterval(id);
  }, [loadDepthAndTape]);

  const loadTradingState = useCallback(() => {
    api.tradingState().then(setTrading).catch(() => setTrading(null));
  }, []);

  useEffect(() => {
    loadTradingState();
  }, [loadTradingState]);

  if (error) {
    return (
      <div className="banner">
        {error}
        <div style={{ marginTop: 8 }}>
          <Link to="/" className="btn">
            ← back to screener
          </Link>
        </div>
      </div>
    );
  }

  if (!market) return <p className="muted">loading {ticker}…</p>;

  return (
    <div>
      <div className="market-head">
        <div>
          <Link to="/" className="crumb">
            ← screener
          </Link>
          <h1 className="market-title">{market.title || market.ticker}</h1>
          <div className="market-sub">
            <span className="mono">{market.ticker}</span>
            {market.category && <span className="chip">{market.category}</span>}
            {market.status && <span className="chip">{market.status}</span>}
            <span
              className={feedStatus === "live" ? "pill ok" : "pill warn"}
              title={FEED_STATUS_TITLE}
            >
              socket {feedStatus}
            </span>
          </div>
        </div>
        <div className="market-quote">
          <div className="quote-last">{centsNum(market.last_price)}¢</div>
          <div className="quote-legs">
            <span className="up">{centsNum(market.yes_bid)}</span>
            <span className="muted"> / </span>
            <span className="down">{centsNum(market.yes_ask)}</span>
            <span className="muted"> ¢ bid/ask</span>
          </div>
          <div className="quote-age muted">
            {quoteAt ? `quote read ${asClock(quoteAt.toISOString())}` : "loading…"}
          </div>
        </div>
      </div>

      <section className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-head">
          <h2>
            Price{" "}
            {/* The ladder said where its data came from and the chart did
                not, though the API returns it. README promises this per
                panel. */}
            {candleSource && (
              <span className="muted src-tag">
                {candleSource === "kalshi" ? "live" : "cached"}
              </span>
            )}
          </h2>
          <div className="seg">
            {PERIODS.map((p) => (
              <button
                key={p.label}
                className={p.sec === period.sec ? "seg-btn active" : "seg-btn"}
                onClick={() => setPeriod(p)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
        <PriceChart candles={candles} />
        {chartNote && <p className="muted" style={{ marginTop: 8 }}>{chartNote}</p>}
      </section>

      <div className="split-3">
        <section className="panel">
          <h2>Order book</h2>
          <OrderBookLadder book={book} />
        </section>

        <section className="panel">
          <h2>
            Tape <span className="muted src-tag">polled · 5s</span>
          </h2>
          <TapeView trades={trades} />
        </section>

        <TradeTicket
          market={market}
          state={trading}
          onProposed={loadTradingState}
        />
      </div>

      <div className="split" style={{ marginTop: 12 }}>
        <section className="panel">
          <h2>Details</h2>
          <Row k="yes">{market.yes_sub_title || "—"}</Row>
          <Row k="no">{market.no_sub_title || "—"}</Row>
          <Row k="type">{market.market_type || "—"}</Row>
          <Row k="24h volume">{asCount(market.volume_24h)}</Row>
          <Row k="open interest">{asCount(market.open_interest)}</Row>
          {/* Same field the screener renders at 0dp with a bar; showing
              "95.5" here and "96" there is two renderings of one number on
              one page load. */}
          <Row k="liquidity score">
            {market.liquidity_score === null
              ? "—"
              : market.liquidity_score.toFixed(0)}
          </Row>
          <Row k="closes">{asTimeToClose(market.hours_to_close)}</Row>
          <Row k="tick structure">{market.price_level_structure || "—"}</Row>
          {market.event && (
            <Row k="exclusive set">
              {market.event.mutually_exclusive === null
                ? "unknown"
                : market.event.mutually_exclusive
                  ? "yes — set-arb candidate"
                  : "no"}
            </Row>
          )}
        </section>

        <section className="panel">
          <h2>Settlement rules</h2>
          {market.rules_primary ? (
            <p className="rules">{market.rules_primary}</p>
          ) : (
            <p className="muted">No rules text stored.</p>
          )}
        </section>
      </div>

      {siblings.length > 1 && (
        <section className="panel" style={{ marginTop: 12 }}>
          <h2>
            Same event ({siblings.length} legs)
            {market.event?.mutually_exclusive && " · mutually exclusive"}
          </h2>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>ticker</th>
                  <th>outcome</th>
                  <th className="num">bid ¢</th>
                  <th className="num">ask ¢</th>
                  <th className="num">last ¢</th>
                  <th className="num">24h vol</th>
                </tr>
              </thead>
              <tbody>
                {siblings.map((s) => (
                  <tr key={s.ticker} className={s.ticker === ticker ? "self" : ""}>
                    <td className="mono">
                      <Link to={`/market/${encodeURIComponent(s.ticker)}`}>
                        {s.ticker}
                      </Link>
                    </td>
                    <td className="title">{s.yes_sub_title || s.title || "—"}</td>
                    <td className="num">{centsNum(s.yes_bid)}</td>
                    <td className="num">{centsNum(s.yes_ask)}</td>
                    <td className="num accent">{centsNum(s.last_price)}</td>
                    <td className="num">{asCount(s.volume_24h)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
