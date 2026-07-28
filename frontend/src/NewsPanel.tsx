/**
 * Catalysts and headlines.
 *
 * The catalyst table exists to answer one question the rest of the dashboard
 * cannot: **when does the window shut?** On Kalshi's scheduled economic
 * releases the market closes minutes *before* the number publishes — CPI's
 * market stops trading at 08:25 for an 08:30 print — so a catalyst is a
 * deadline, not an event to trade. A row that has gone past its close is
 * shown as missed rather than as an opportunity, because the naive version of
 * this panel is one that lights up exactly when it is too late to act.
 *
 * The headline list carries no score, direction or sentiment, and that is
 * structural rather than unfinished. By the time an item reaches an RSS feed
 * the market has moved.
 */

import { Link } from "react-router-dom";
import { asUsd, type CatalystRow, type NewsState } from "./api";

function countdown(minutes: number): string {
  if (minutes <= 0) return "closed";
  if (minutes < 90) return `${Math.round(minutes)}m`;
  if (minutes < 2880) return `${(minutes / 60).toFixed(1)}h`;
  return `${Math.round(minutes / 1440)}d`;
}

function stateLabel(c: CatalystRow): { text: string; cls: string } {
  switch (c.state) {
    case "open":
      return { text: "open", cls: "ok" };
    case "closing_soon":
      return { text: "closing soon", cls: "warn" };
    case "closed_pending_settlement":
      // Deliberately worded as a miss. The number is public and the market is
      // untradeable; there is nothing here to act on.
      return { text: "window shut — awaiting settlement", cls: "muted" };
    default:
      return { text: "settled", cls: "muted" };
  }
}

export default function NewsPanel({ news }: { news: NewsState | null }) {
  if (!news) return null;

  const { budget } = news;
  const live = news.catalysts.filter((c) => c.actionable);
  const missed = news.catalysts.filter((c) => !c.actionable);

  return (
    <section className="panel" style={{ marginTop: 12 }}>
      <div className="panel-head">
        <h2>Catalysts</h2>
        <span className="muted">
          when the window shuts — on scheduled releases the market closes
          before the number lands
        </span>
      </div>

      {news.catalysts.length === 0 ? (
        <p className="muted">
          No scheduled catalysts among the tracked series. The calendar names
          the releases it knows; an unlisted series is not guessed at.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>market</th>
                <th>catalyst</th>
                <th className="num">markets</th>
                <th>closes</th>
                <th className="num">in</th>
                <th>release (inferred)</th>
                <th>state</th>
              </tr>
            </thead>
            <tbody>
              {[...live, ...missed].map((c) => {
                const s = stateLabel(c);
                return (
                  <tr key={c.ticker}>
                    <td className="mono">
                      <Link to={`/market/${encodeURIComponent(c.ticker)}`}>
                        {c.ticker}
                      </Link>
                    </td>
                    <td>{c.label}</td>
                    <td className="num muted">{c.market_count}</td>
                    <td className="muted">
                      {new Date(c.close_time).toLocaleString()}
                    </td>
                    <td className={c.actionable ? "num warn" : "num muted"}>
                      {countdown(c.minutes_to_close)}
                    </td>
                    <td className="muted">
                      {c.expected_release
                        ? new Date(c.expected_release).toLocaleTimeString()
                        : "—"}
                    </td>
                    <td className={s.cls}>{s.text}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="sub">Headlines</h3>
      {/* Independent statements, not an if/else chain. As a chain only the
          first branch ever rendered, so "the headline engine is off" sat
          immediately above a table of thirty headlines and the reason there
          were feeds-configured-zero headlines at all never showed. Each of
          these is separately true or not. */}
      {!budget.enabled && (
        <p className="muted">
          LLM triage is off (<code>news.headlines.enabled</code>). Headlines
          below are still collected and listed — they simply carry no score,
          direction or sentiment.
        </p>
      )}
      {budget.enabled && !budget.has_api_key && (
        <p className="muted">
          Enabled, but no <code>ANTHROPIC_API_KEY</code> is configured — triage
          refuses rather than running. Headlines are still collected.
        </p>
      )}
      {news.feeds_configured === 0 && (
        <p className="muted">
          No RSS feeds are configured right now, so nothing new is being
          collected. <code>news.headlines.rss_feeds</code> ships empty: each
          feed is a decision about what this system reads, and a wider feed is
          mostly more noise.
        </p>
      )}
      {news.headlines.length === 0 && (
        <p className="muted">Nothing collected yet.</p>
      )}

      {news.headlines.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>when</th>
                <th>source</th>
                <th>headline</th>
                <th>possible markets</th>
              </tr>
            </thead>
            <tbody>
              {news.headlines.map((h) => (
                <tr key={`${h.source}-${h.title}-${h.published_at}`}>
                  <td className="muted" title={h.published_at}>
                    {new Date(h.published_at).toLocaleString()}
                  </td>
                  <td className="mono">{h.source}</td>
                  <td className="audit-detail">
                    {h.link ? (
                      <a href={h.link} target="_blank" rel="noreferrer">
                        {h.title}
                      </a>
                    ) : (
                      h.title
                    )}
                  </td>
                  <td className="mono muted">
                    {h.matched_tickers.length === 0
                      ? "—"
                      : h.matched_tickers.join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="stats" style={{ marginTop: 10 }}>
        <div className="stat">
          <span className="stat-label">llm spend today</span>
          {/* These arrive in dollars. Multiplying to cents in JS just to
              divide back by 100 inside the formatter was two float
              operations on a value that arrived correct. */}
          <span className="stat-value">
            {asUsd(budget.spent_usd)} / {asUsd(budget.budget_usd)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">triaged</span>
          <span className="stat-value">{budget.triaged}</span>
        </div>
        <div className="stat">
          <span className="stat-label">escalated</span>
          <span
            className={
              budget.escalation_rate > budget.escalation_rate_cap
                ? "stat-value warn"
                : "stat-value"
            }
          >
            {budget.escalated} ({(budget.escalation_rate * 100).toFixed(0)}% of{" "}
            {(budget.escalation_rate_cap * 100).toFixed(0)}% cap)
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">feeds</span>
          <span className="stat-value">{news.feeds_configured}</span>
        </div>
      </div>
    </section>
  );
}
