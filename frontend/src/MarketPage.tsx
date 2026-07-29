import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import OrderBookLadder from "./OrderBookLadder";
import PriceChart from "./PriceChart";
import TapeView from "./TapeView";
import TradeTicket from "./TradeTicket";
import {
  api,
  ApiError,
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

/**
 * The API caps `lookback_hours` at 90 days for **every** period — the cap is
 * on the window, not on the candle count, because at `period_sec=60` a
 * 180-day window is ~259,000 rows. Nothing here may exceed it: the "1d" tab
 * asked for 24*180 = 4320h, the API answered 422 on every market, and the
 * catch below reported it as "Could not load candles" — a data problem that
 * did not exist. Named as a constant so the next edit trips over the ceiling
 * instead of the endpoint.
 */
const MAX_LOOKBACK_HOURS = 24 * 90;

const PERIODS = [
  // Candles per request, so a tab cannot quietly become a 250k-row fetch:
  //   1m -> 720, 1h -> 336, 1d -> 90.
  { label: "1m", sec: 60, lookback: 12 },
  { label: "1h", sec: 3600, lookback: 24 * 14 },
  { label: "1d", sec: 86400, lookback: MAX_LOOKBACK_HOURS },
];

function Row({
  k,
  title,
  children,
}: {
  k: string;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="row" title={title}>
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
    } catch (e) {
      setCandleSource(null);
      // "Could not load candles" reads as "this market has no data", which
      // sent an operator hunting for a data problem when the request itself
      // was out of range. A 422 is the client's fault and the note has to say
      // so, or the bug hides behind a plausible-looking empty chart.
      setChartNote(
        e instanceof ApiError && e.status === 422
          ? `Chart request refused by the API (${e.message}). This is a UI bug, not missing data.`
          : `Could not load candles${e instanceof ApiError ? ` — ${e.message}` : ""}.`,
      );
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
        {/* Two sources, named as two sources.
            This headline reads Kalshi's *summary* quote fields; the ladder
            below reads the L2 book. They genuinely disagree — measured 6¢
            apart on one page load — and neither is wrong to publish: the
            summary is what the exchange is saying, and reconciling it
            server-side would mean storing a number Kalshi never sent. So the
            fix is to stop presenting them as one voice. Both now say which
            feed they are, and the tradeable one says so. */}
        <div className="market-quote">
          <div className="quote-last">{centsNum(market.last_price)}¢</div>
          <div className="quote-legs">
            <span className="up">{centsNum(market.yes_bid)}</span>
            <span className="muted"> / </span>
            <span className="down">{centsNum(market.yes_ask)}</span>
            <span className="muted"> ¢ bid/ask</span>
            {/* Server-decided, in Decimal. Three states, and `null` is
                "one-sided, never checked" — not "sound". */}
            {market.quote_crossed === true && (
              <span
                className="crossed-flag"
                title="Crossed: the summary bid is above the summary ask. The payload that produces this also reports negative bid sizes on this exchange, so treat BOTH numbers here as unreliable — not just the one that looks wrong. Read the order book below."
              >
                crossed
              </span>
            )}
            {market.quote_crossed === null && (
              <span
                className="quote-flag"
                title="One side of the summary quote is missing, so it was not checked for a crossed quote. Absent, not sound."
              >
                one-sided
              </span>
            )}
          </div>
          <div className="quote-age muted">
            {quoteAt
              ? `summary quote read ${asClock(quoteAt.toISOString())}`
              : "loading…"}
          </div>
          <div className="quote-source">
            summary feed — trade against the{" "}
            <strong>order book</strong> below
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
          {/* Names its feed, in the same idiom the Price and Tape panels
              already use ("live"/"cached", "polled · 5s"). Not a green pill:
              a status-coloured badge on this panel reads as "this market is
              tradeable", which is a claim about the market rather than about
              which of two feeds to believe. The ladder prints its own
              live/cached freshness a line below. */}
          <h2>
            Order book <span className="muted src-tag">L2 depth</span>
          </h2>
          {/* Stated here rather than only in the headline above, because this
              is the panel an operator is reading when the two numbers
              disagree. It is not a bug on either side: two feeds, two
              refreshes, and the summary one is the one that goes crossed. */}
          <p className="muted quote-source-note">
            The bid/ask in the page header is Kalshi's <strong>summary</strong>{" "}
            quote — a different feed from this snapshot, and one that has been
            measured 6¢ away from it on the same page load. When they disagree,
            <strong> this book wins</strong>: it is the depth an order meets.
          </p>
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
              one page load.

              Labelled "attention", not "liquidity": volume and open interest
              carry half the weight, so a market with no bid at any price
              scored 50.5/100 and read as "medium liquidity" to anyone
              skimming. The formula is deliberate and correct — the word was
              the thing overpromising. */}
          <Row
            k="attention score"
            title="An ordering for attention, not a measure of tradability. 24h volume and open interest carry half the weight, so a market with no bid at all can still score around 50. Read bid/ask above for whether you can actually trade it."
          >
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
                    {/* Flagged here too, and this is the table where it bites
                        hardest: set arbitrage sums the legs' asks, so one
                        crossed leg quietly moves the total that decides
                        whether a set looks free. Same server flag, same word,
                        same colour as the screener. */}
                    <td
                      className={s.quote_crossed === true ? "num warn" : "num"}
                      title={
                        s.quote_crossed === true
                          ? "Crossed summary quote on this leg — bid above ask. Both numbers on this row are unreliable."
                          : undefined
                      }
                    >
                      {centsNum(s.yes_ask)}
                      {s.quote_crossed === true && (
                        <span className="crossed-flag">crossed</span>
                      )}
                    </td>
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
