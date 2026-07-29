/** Typed access to the backend API.
 *
 * Money arrives as strings and stays that way. Parsing a price into a JS
 * number loses precision the backend deliberately preserved, so formatting
 * happens on the string and arithmetic does not happen here at all.
 */

export interface SystemStatus {
  environment: string;
  trading_mode: string;
  live_trading_armed: boolean;
  kill_switch: boolean;
  credentials_present: boolean;
  enabled_detectors: string[];
  heartbeats: Record<string, boolean>;
  fees: {
    verified_on: string | null;
    schedule_revision: string | null;
    base_taker_rate: number;
    base_maker_rate: number;
    /** Series the schedule lists as non-standard. */
    series_listed: number;
    /** Series that charge no trading fees at all. */
    fee_free_series: string[];
    /** False if a listed multiplier exceeds the default, making the
     *  "unlisted means standard" assumption unsafe. */
    default_is_safe: boolean;
  };
  endpoints: { rest: string; ws: string };
}

export interface Health {
  status: string;
  checks: Record<string, boolean>;
}

export interface MarketRow {
  ticker: string;
  event_ticker: string | null;
  series_ticker: string | null;
  title: string | null;
  yes_sub_title: string | null;
  no_sub_title: string | null;
  category: string | null;
  status: string | null;
  yes_bid: string | null;
  yes_ask: string | null;
  no_bid: string | null;
  no_ask: string | null;
  last_price: string | null;
  previous_price: string | null;
  spread: string | null;
  /**
   * The summary quote is crossed — `yes_bid` is above `yes_ask`.
   *
   * Decided **once, on the server, in `Decimal`** (`_market_row` in
   * `api/routes/markets.py`). Never re-derive it here: `Number(bid) >
   * Number(ask)` is exactly the float parse this whole money path exists to
   * avoid, and comparing the two strings only works for as long as every
   * price happens to arrive at the same width.
   *
   * `null` is **not** `false`. It means one side of the quote is missing, so
   * the question was never asked — render it as unknown. Inventing a `false`
   * for a `null` is the same bug as copy that asserts a state nobody checked.
   *
   * True means *both* numbers are untrustworthy, not just the one that looks
   * wrong: the summary payload that produces a crossed quote on this exchange
   * also reports negative bid sizes.
   */
  quote_crossed: boolean | null;
  volume: string | null;
  volume_24h: string | null;
  open_interest: string | null;
  liquidity_dollars: string | null;
  liquidity_score: number | null;
  close_time: string | null;
  hours_to_close: number | null;
  first_seen_at: string | null;
}

export interface MarketDetail extends MarketRow {
  rules_primary: string | null;
  rules_secondary: string | null;
  price_level_structure: string | null;
  market_type: string | null;
  open_time: string | null;
  result: string | null;
  strike_type: string | null;
  floor_strike: string | null;
  cap_strike: string | null;
  event?: {
    ticker: string;
    title: string | null;
    sub_title: string | null;
    mutually_exclusive: boolean | null;
  };
}

export interface MarketsPage {
  total: number;
  limit: number;
  offset: number;
  markets: MarketRow[];
}

export interface Candle {
  ts: number;
  open: string | null;
  high: string | null;
  low: string | null;
  close: string | null;
  volume: string | null;
}

export interface CandlesResponse {
  ticker: string;
  period_sec: number;
  source: string;
  candles: Candle[];
}

export interface Trade {
  ts: string;
  yes_price: string | null;
  no_price: string | null;
  count: string | null;
  taker_side: string | null;
}

export interface OrderbookResponse {
  ticker: string;
  source: string;
  ts: string;
  seq: number | null;
  yes: [string, string][];
  no: [string, string][];
}

export interface CatalogStats {
  markets: number;
  markets_active: number;
  markets_uncategorised: number;
  events: number;
  candles: number;
  tape: number;
  orderbook_snaps: number;
  newest_trade: string | null;
  newest_candle: string | null;
  trade_lag_sec: number | null;
  by_category: { category: string; count: number }[];
  recent_listings: {
    ticker: string;
    title: string | null;
    category: string | null;
    first_seen_at: string | null;
  }[];
}

// ---------------------------------------------------------------------------
// Trading (M3)
//
// Every price here is a dollar string. The backend refuses a cents-style "56"
// rather than reading it as $56, so the forms send exactly what was typed and
// let the engine validate it.
// ---------------------------------------------------------------------------

export interface TradingState {
  environment: string;
  trading_mode: string;
  live_trading_armed: boolean;
  kill_switch: boolean;
  /** `config.yaml` pins it on, so it cannot be released from the UI. */
  kill_switch_config_floor?: boolean;
  credentials_present: boolean;
  /** simulated | demo_exchange | live_exchange, or null when refused. */
  execution_route: string | null;
  route_blocked_by: string | null;
  real_money: boolean;
  requires_typed_confirmation: boolean;
  proposal_ttl_sec: number;
  time_in_force: string;
  auto_cancel_after_sec: number;
  pending_proposals: number;
  working_orders: number;
  balance?: {
    cents: number | null;
    dollars: string | null;
    portfolio_value_cents: number | null;
  };
  balance_error?: string;
}

/** What the emergency stop actually did. */
export interface KillSwitchResult {
  /** The state the server settled on — not the state that was requested. */
  kill_switch: boolean;
  /** True when `config.yaml` pins it on; a runtime release cannot clear it. */
  config_floor: boolean;
  canceled_orders: number;
  canceled_order_ids: number[];
  /** Orders the cancel could not reach. When this is non-empty it is the most
   *  urgent thing on the screen: those orders are still live. */
  failed_cancels: { order_id: number; error: string }[];
}

export interface TicketQuote {
  ticker: string;
  side: string;
  action: string;
  limit_price: string;
  contracts: string;
  category: string | null;
  /** Series ticker — what the fee schedule is actually keyed by. */
  series: string | null;
  is_taker: boolean;
  /** Exactly what will be sent to Kalshi, so it can be shown before approval. */
  wire: { book_side: string; yes_price: string; count: string };
  notional_cents: string;
  /** Fractional cents as a decimal string — fees round to a centicent. */
  est_fee_cents: string;
  total_cost_cents: string;
  breakeven_cents: string;
  max_loss_cents: string;
  max_win_cents: string;
  /** Present only when a fair value was supplied. Always net of costs. */
  net_edge_cents: string | null;
  fair_price: string | null;
}

export interface ProposalLeg {
  seq: number;
  ticker: string;
  side: string;
  action: string;
  /** The price on the **traded** side — 0.30 for "buy NO at 30¢". */
  limit_price: string;
  contracts: string;
  fair_price: string | null;
  est_fee_cents: string | null;
  /**
   * The literal wire form, which is what actually reaches the exchange.
   *
   * Kalshi quotes one book from the YES side, so "buy NO at 30¢" goes out as
   * an **ask at 0.70**. Nothing downstream catches an inversion — the fee
   * formula P(1-P) is symmetric, so a flipped direction produces the same
   * fee, the same notional and a plausible confirmation — which makes the
   * human reading this card the only guard there is.
   *
   * Optional because the API did not always send it; the card says so
   * plainly rather than quietly showing one less check.
   */
  book_side?: string | null;
  wire_price?: string | null;
}

export interface Proposal {
  id: number;
  signal_id: number | null;
  source: string;
  /** First leg's market, denormalised for listing. Legs decide what trades. */
  ticker: string;
  event_ticker: string | null;
  leg_count: number;
  legs: ProposalLeg[];
  net_edge_cents: string | null;
  est_fee_cents: string | null;
  /** Worst case across every leg, in cents. What the risk limits measure. */
  max_loss_cents: string | null;
  pct_of_bankroll: number | null;
  rationale: string | null;
  status: string;
  expires_at: string | null;
  expires_in_sec: number | null;
  decided_at: string | null;
  decision_reason: string | null;
  created_at: string | null;
}

export interface OrderRow {
  id: number;
  proposal_id: number | null;
  client_order_id: string;
  exchange_order_id: string | null;
  ticker: string;
  side: string;
  action: string;
  limit_price: string;
  contracts: string;
  filled_contracts: string;
  time_in_force: string;
  status: string;
  is_paper: boolean;
  route: string;
  error: string | null;
  created_at: string | null;
}

export interface FillRow {
  id: number;
  order_id: number | null;
  exchange_fill_id: string | null;
  ticker: string;
  side: string;
  action: string;
  price: string;
  contracts: string;
  /** Exact cents as a decimal string — the exchange bills fractional cents. */
  fee_cents: string;
  is_taker: boolean;
  ts: string | null;
}

/**
 * Portfolio risk limits and how close each one is.
 *
 * Every money field is a decimal string of **cents**, like the rest of the
 * API — formatted for display, never computed with.
 */
export interface RiskState {
  route: string;
  day: string;
  bankroll_cents: string;
  exposure_cents: string;
  pending_cents: string;
  committed_cents: string;
  exposure_limit_cents: string;
  headroom_cents: string;
  daily_realized_cents: string;
  daily_fees_cents: string;
  /** Realised minus fees. The only honest version of the day's P&L. */
  daily_net_cents: string;
  daily_loss_limit_cents: string;
  daily_loss_breached: boolean;
  consecutive_losses: number;
  cooldown_until: string | null;
  in_cooldown: boolean;
  halted: boolean;
}

export interface RiskLimits {
  bankroll_usd: number;
  max_pct_per_market: number;
  max_total_exposure_pct: number;
  daily_loss_limit_pct: number;
  cooldown_after_consecutive_losses: number;
  cooldown_minutes: number;
  kelly_fraction: number;
  max_pending_proposals: number;
}

export interface RiskResponse {
  /** Null only when no execution route is usable at all. */
  state: RiskState | null;
  limits: RiskLimits;
}

/**
 * A spot feed the BTC-style detectors price against.
 *
 * `fresh` already accounts for staleness — a stale reference counts as absent,
 * because comparing one to a live market invents an edge in whichever
 * direction the market has already moved.
 */
export interface ReferenceFeed {
  symbol: string;
  price: string | null;
  source: string | null;
  age_sec: number | null;
  fresh: boolean;
}

export interface CalibrationBucket {
  cents: number;
  settled: number;
  ready: boolean;
}

export interface EngineState {
  reference_feeds: ReferenceFeed[];
  bitcoin_enabled: boolean;
  calibration: {
    observations: number;
    settled: number;
    min_samples: number;
    buckets: CalibrationBucket[];
  };
}

/**
 * A scheduled catalyst and, more importantly, when its trading window shuts.
 *
 * On Kalshi's economic releases the market closes *before* the data lands —
 * CPI's market stops trading five minutes ahead of the print. So `close_time`
 * is a deadline, not a resolution, and `expected_release` is inferred from it
 * rather than published by the API.
 */
export interface CatalystRow {
  ticker: string;
  series_ticker: string;
  label: string;
  title: string | null;
  /** Tradeable markets sharing this deadline — one FOMC meeting is many. */
  market_count: number;
  close_time: string;
  expected_release: string | null;
  /** open | closing_soon | closed_pending_settlement | settled */
  state: string;
  minutes_to_close: number;
  /** False once the window has shut. Not an opportunity — a missed one. */
  actionable: boolean;
}

/** A headline. Deliberately carries no score, direction or sentiment. */
export interface HeadlineRow {
  title: string;
  source: string;
  link: string | null;
  published_at: string;
  matched_tickers: string[];
}

/** LLM spend against the two independent caps. */
export interface NewsBudget {
  enabled: boolean;
  has_api_key: boolean;
  day: string;
  spent_usd: string;
  budget_usd: string;
  triaged: number;
  escalated: number;
  escalation_rate: number;
  escalation_rate_cap: number;
}

export interface NewsState {
  catalysts: CatalystRow[];
  headlines: HeadlineRow[];
  budget: NewsBudget;
  feeds_configured: number;
}

/** Where a detector's signals went. Available long before any P&L is. */
export interface ReportFunnel {
  signals: number;
  observations: number;
  proposals: number;
  approved: number;
  executed: number;
  partial: number;
  rejected: number;
  expired: number;
  pending: number;
  failed: number;
  orders: number;
  fills: number;
  decided: number;
}

/**
 * One detector's evidence, on one route.
 *
 * Never merged across routes: a simulated fill and a demo-exchange fill are
 * different kinds of evidence and only one of them happened at an exchange.
 */
export interface DetectorReport {
  detector: string;
  route: string;
  funnel: ReportFunnel;
  /** The edge it claimed, averaged over its signals. */
  avg_claimed_edge_cents: string | null;
  /** Decisions closed — not fills, and not legs. */
  trades: number;
  total_pnl_cents: string;
  mean_pnl_cents: string;
  ci_low_cents: string | null;
  ci_high_cents: string | null;
  ci_method: string;
  fees_paid_cents: string;
  max_drawdown_cents: string;
  verdict:
    | "insufficient_evidence"
    | "no_edge_shown"
    | "edge_shown"
    | "losing";
  headline: string;
  unattributed: number;
}

/**
 * One (detector, route) pair as the gate measured it.
 *
 * `ci_low_cents` is the field that decides anything — the bootstrap interval's
 * lower bound. `mean_cents` is shown beside it and is never the basis for a
 * decision: on a 20-trade binary sample the point estimate is the number that
 * lies, which is the whole reason the verdict exists.
 */
export interface AutonomyPair {
  detector: string;
  route: string;
  verdict: DetectorReport["verdict"];
  trades: number;
  ci_low_cents: string | null;
  mean_cents: string;
}

export interface AutonomyEvidence {
  computed_at: string;
  min_trades: number;
  coverage_usable: boolean;
  coverage_refusals: string[];
  pairs: AutonomyPair[];
}

/**
 * Whether the machine may trade, and when it may not, why.
 *
 * `kill_switch` and `disarmed_reason` are **two different stops** and the panel
 * must never blur them: the kill switch halts everything including manual
 * approvals and cancels resting orders; the latch stops only the machine.
 */
export interface AutonomyState {
  enabled: boolean;
  env_armed: boolean;
  live_armed: boolean;
  routes: { simulated: boolean; demo_exchange: boolean; live_exchange: boolean };
  kill_switch: boolean;
  /** Non-null means latched off, and the string says what tripped it. */
  disarmed_reason: string | null;
  requires_manual_rearm: boolean;
  min_proposal_age_sec: number;
  decision_interval_sec: number;
  /** Null until the worker's refresh loop has published one. */
  evidence: AutonomyEvidence | null;
  budget: {
    max_trades_per_hour: number;
    max_trades_per_detector_per_hour: number;
    max_daily_risk_cents: number;
    max_open_positions: number;
    max_working_orders: number;
    repeat_cooldown_sec: number;
  };
}

export interface ReportCardResponse {
  min_trades: number;
  window_days: number;
  generated_at: string;
  detectors: DetectorReport[];
}

/** A market that resolved while we held a position in it. */
export interface SettlementRow {
  ticker: string;
  event_ticker: string | null;
  route: string;
  result: string | null;
  settled_yes_value: string;
  net_contracts: string;
  avg_price: string;
  realized_pnl_cents: string;
  fee_cents: string;
  /** exchange | market — the latter is the simulated book settling locally. */
  source: string;
  settled_at: string | null;
}

export interface PositionRow {
  ticker: string;
  /** simulated | demo_exchange | live_exchange — positions are per book. */
  route: string;
  is_paper: boolean;
  net_contracts: string;
  side: string;
  contracts: string;
  avg_price: string;
  avg_yes_price: string;
  realized_pnl_cents: string;
  unrealized_pnl_cents: string | null;
  fees_paid_cents: string;
  updated_at: string | null;
}

export interface SignalRow {
  /** Scans that have seen this same observation. 1 means it is new. */
  seen_count: number;
  /** When it was last still true — a persisting edge differs from a flicker. */
  last_seen_at: string | null;
  id: number;
  detector: string;
  ticker: string;
  side: string;
  fair_price: string;
  /** Always net of fees. Zero means the detector declined to claim an edge. */
  net_edge_cents: string;
  confidence: number;
  size_hint: string | null;
  rationale: string | null;
  evidence: Record<string, unknown> | null;
  created_at: string | null;
}

export interface AuditEntry {
  id: number;
  ts: string | null;
  kind: string;
  ticker: string | null;
  actor: string;
  payload: Record<string, unknown> | null;
}

export interface TicketInput {
  ticker: string;
  side: "yes" | "no";
  action: "buy" | "sell";
  limit_price: string;
  contracts: string;
  fair_price?: string | null;
  rationale?: string | null;
  ttl_sec?: number | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    /** Stable machine-readable reason from the backend, when it sent one. */
    public code?: string,
    public detail?: string,
  ) {
    super(message);
  }
}

async function parseError(response: Response, path: string): Promise<ApiError> {
  let code: string | undefined;
  let detail: string | undefined;
  try {
    const body = await response.json();
    const d = body?.detail;
    if (typeof d === "string") {
      detail = d;
    } else if (Array.isArray(d)) {
      // FastAPI's request-validation errors (422) arrive as a *list* of
      // {loc, msg, ...} objects, not the {error, message} shape our own
      // handlers use. Falling through to the object branch below read
      // `.error`/`.message` off an array, got undefined for both, and every
      // 422 in the app collapsed to the generic "path -> HTTP 422" — which
      // is how a chart tab asking for an out-of-range window ended up
      // indistinguishable from missing data. Name the field and the reason.
      detail = d
        .map((item) => {
          const where = Array.isArray(item?.loc)
            ? item.loc.filter((p: unknown) => p !== "query" && p !== "body").join(".")
            : "";
          const why = typeof item?.msg === "string" ? item.msg : "invalid";
          return where ? `${where}: ${why}` : why;
        })
        .join("; ");
    } else if (d && typeof d === "object") {
      code = d.error;
      detail = d.message;
    }
  } catch {
    /* a non-JSON error body is still an error */
  }
  return new ApiError(
    detail || `${path} -> HTTP ${response.status}`,
    response.status,
    code,
    detail,
  );
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw await parseError(response, path);
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw await parseError(response, path);
  return (await response.json()) as T;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  return query.toString();
}

export const api = {
  system: () => getJson<SystemStatus>("/api/system"),
  health: () => getJson<Health>("/api/health"),
  catalogStats: () => getJson<CatalogStats>("/api/catalog/stats"),
  categories: () =>
    getJson<{ categories: { category: string; count: number }[] }>("/api/categories"),
  markets: (params: Record<string, string | number | undefined>) =>
    getJson<MarketsPage>(`/api/markets?${qs(params)}`),
  market: (ticker: string) =>
    getJson<MarketDetail>(`/api/markets/${encodeURIComponent(ticker)}`),
  siblings: (ticker: string) =>
    getJson<{ event_ticker: string; markets: MarketRow[] }>(
      `/api/markets/${encodeURIComponent(ticker)}/siblings`,
    ),
  candles: (ticker: string, periodSec: number, lookbackHours: number) =>
    getJson<CandlesResponse>(
      `/api/markets/${encodeURIComponent(ticker)}/candles?${qs({
        period_sec: periodSec,
        lookback_hours: lookbackHours,
      })}`,
    ),
  tape: (ticker: string, limit = 60) =>
    getJson<{ ticker: string; trades: Trade[] }>(
      `/api/markets/${encodeURIComponent(ticker)}/tape?${qs({ limit })}`,
    ),
  orderbook: (ticker: string) =>
    getJson<OrderbookResponse>(
      `/api/markets/${encodeURIComponent(ticker)}/orderbook`,
    ),

  // -- trading ------------------------------------------------------------

  tradingState: () => getJson<TradingState>("/api/trading/state"),

  /** Price a ticket without creating anything. */
  quoteTicket: (ticket: TicketInput) =>
    postJson<{ quote: TicketQuote; market: Record<string, string | null> }>(
      "/api/proposals/quote",
      ticket,
    ),

  /** Create a pending proposal. Sends nothing to any exchange. */
  propose: (ticket: TicketInput) =>
    postJson<{ proposal: Proposal; quote: TicketQuote }>(
      "/api/proposals",
      ticket,
    ),

  proposals: (status?: string) =>
    getJson<{ proposals: Proposal[] }>(
      `/api/proposals?${qs({ status, limit: 100 })}`,
    ),

  /**
   * Approve one proposal and place its order. `confirm` is always explicit,
   * and the live route additionally requires the ticker typed back.
   */
  approve: (id: number, confirmationPhrase?: string) =>
    postJson<{ proposal: Proposal; order: OrderRow; fills: FillRow[] }>(
      `/api/proposals/${id}/approve`,
      { confirm: true, confirmation_phrase: confirmationPhrase ?? null },
    ),

  reject: (id: number, reason?: string) =>
    postJson<{ proposal: Proposal }>(`/api/proposals/${id}/reject`, {
      reason: reason ?? null,
    }),

  /**
   * Engage or release the kill switch at runtime.
   *
   * Returns the state the server actually settled on plus how many resting
   * orders it cancelled, so the UI reports what happened rather than what it
   * asked for. Never assume the request succeeded — an emergency stop that
   * *looks* engaged and is not is worse than no control at all.
   */
  killSwitch: (engaged: boolean) =>
    postJson<KillSwitchResult>("/api/kill-switch", { engaged, confirm: true }),

  orders: (ticker?: string) =>
    getJson<{ orders: OrderRow[] }>(`/api/orders?${qs({ ticker, limit: 50 })}`),

  cancelOrder: (id: number) =>
    postJson<{ order: OrderRow }>(`/api/orders/${id}/cancel`),

  fills: (ticker?: string) =>
    getJson<{ fills: FillRow[] }>(`/api/fills?${qs({ ticker, limit: 100 })}`),

  positions: () => getJson<{ positions: PositionRow[] }>("/api/positions"),

  signals: (detector?: string) =>
    getJson<{ signals: SignalRow[] }>(`/api/signals?${qs({ detector, limit: 50 })}`),

  audit: (kind?: string) =>
    getJson<{ entries: AuditEntry[] }>(`/api/audit?${qs({ kind, limit: 100 })}`),

  risk: () => getJson<RiskResponse>("/api/risk"),

  engine: () => getJson<EngineState>("/api/engine"),

  news: () => getJson<NewsState>("/api/news"),

  reportCard: () => getJson<ReportCardResponse>("/api/report-card"),

  autonomy: () => getJson<AutonomyState>("/api/autonomy"),

  /** Stop the machine. Confirmed but not typed — a stop must be fast. */
  disarmAutonomy: () =>
    postJson<{ disarmed: boolean; reason: string | null }>(
      "/api/autonomy/disarm",
      { confirm: true },
    ),

  /** Clear the latch. Typed, because resuming is never urgent. */
  rearmAutonomy: (phrase: string) =>
    postJson<{ disarmed: boolean; reason: string | null }>(
      "/api/autonomy/rearm",
      { confirm: true, phrase },
    ),

  settlements: () =>
    getJson<{ settlements: SettlementRow[] }>("/api/settlements?limit=50"),
};

// ---------------------------------------------------------------------------
// Formatting.
//
// Money arrives as a decimal string and is formatted by moving the decimal
// point along the digits, not by parsing it into a JS number. The comment that
// used to sit here claimed exactly that while the code beneath it did the
// opposite, and that is how the canonical 1.75¢ fee — one contract at 50¢, the
// number CLAUDE.md's units section exists to protect — reached the screen as
// "$0.02", and a real billed fee of 0.04¢ as "$0.00".
//
// Two rules follow, and neither is cosmetic:
//
//   1. Rounding happens on the digit string, half-up. `Number()` survives only
//      as a fallback for a payload this parser does not recognise (exponent
//      notation, say), so an odd value still renders instead of vanishing.
//   2. **A non-zero amount never renders as zero.** Precision is extended
//      until a significant digit survives, down to the centicent ($0.0001)
//      that the fee schedule rounds to. Money that rounds away to nothing is
//      the one display error that only ever flatters.
// ---------------------------------------------------------------------------

interface Dec {
  neg: boolean;
  /** Integer digits, no leading zeros, never empty. */
  int: string;
  /** Fraction digits, possibly empty. */
  frac: string;
}

const PLAIN_DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

/** Cents are never shown finer than a centicent — nothing is billed finer. */
const MAX_CENT_DP = 4;

function parseDec(raw: string): Dec | null {
  const s = raw.trim();
  if (!PLAIN_DECIMAL.test(s)) return null;
  const signed = s.charAt(0) === "+" || s.charAt(0) === "-";
  const body = signed ? s.slice(1) : s;
  const dot = body.indexOf(".");
  const int = dot === -1 ? body : body.slice(0, dot);
  const frac = dot === -1 ? "" : body.slice(dot + 1);
  return {
    neg: s.charAt(0) === "-",
    int: int.replace(/^0+(?=\d)/, "") || "0",
    frac,
  };
}

/**
 * Parse, falling back to `Number` for anything the strict grammar rejects.
 *
 * Postgres NUMERIC columns serialise as plain fixed-point, so the fallback
 * should never fire on our own money. It exists so that a computed Decimal
 * that came out as "1E-8" degrades to an approximate figure rather than a
 * dash.
 */
function toDec(raw: string): Dec | null {
  const strict = parseDec(raw);
  if (strict) return strict;
  const value = Number(raw);
  if (!Number.isFinite(value)) return null;
  return parseDec(value.toFixed(8));
}

function isZero(d: Dec): boolean {
  return !/[1-9]/.test(d.int + d.frac);
}

/** Move the decimal point `places` to the left. Negative shifts right. */
function shift(d: Dec, places: number): Dec {
  let digits = d.int + d.frac;
  const point = d.int.length - places;
  if (point > digits.length) digits += "0".repeat(point - digits.length);
  const int = point > 0 ? digits.slice(0, point) : "0";
  const frac = point > 0 ? digits.slice(point) : "0".repeat(-point) + digits;
  return {
    neg: d.neg,
    int: int.replace(/^0+(?=\d)/, "") || "0",
    frac,
  };
}

/** Add one to a run of digits, carrying left. "099" -> "100". */
function bumpDigits(digits: string): string {
  const out = digits.split("");
  let i = out.length - 1;
  for (; i >= 0; i--) {
    if (out[i] === "9") {
      out[i] = "0";
    } else {
      out[i] = String(Number(out[i]) + 1);
      break;
    }
  }
  return i < 0 ? `1${out.join("")}` : out.join("");
}

/** Half-up rounding to `dp` fraction digits, on the digits themselves. */
function round(d: Dec, dp: number): Dec {
  if (d.frac.length <= dp) {
    return { neg: d.neg, int: d.int, frac: d.frac.padEnd(dp, "0") };
  }
  const kept = d.int + d.frac.slice(0, dp);
  const digits = d.frac.charAt(dp) >= "5" ? bumpDigits(kept) : kept;
  const cut = digits.length - dp;
  return { neg: d.neg, int: digits.slice(0, cut), frac: digits.slice(cut) };
}

/** Group the integer part in threes: "65264" -> "65,264". */
function group(int: string): string {
  return int.replace(/\B(?=(\d{3})+$)/g, ",");
}

function render(d: Dec, dp: number): string {
  const r = round(d, dp);
  const body = dp > 0 ? `${r.int}.${r.frac}` : r.int;
  // No "-0.00": a signed zero is a nonsense figure and reads as a real one.
  return r.neg && !isZero(r) ? `-${body}` : body;
}

/** Round to `dp`, going finer rather than letting a real amount read as zero. */
function renderSignificant(d: Dec, dp: number, maxDp: number): string {
  if (isZero(d)) return render(d, dp);
  for (let p = dp; p < maxDp; p++) {
    if (!isZero(round(d, p))) return render(d, p);
  }
  return render(d, maxDp);
}

/** True when the amount is real but finer than `maxDp` can show at all. */
function tooSmall(d: Dec, maxDp: number): boolean {
  return !isZero(d) && isZero(round(d, maxDp));
}

function trimZeros(text: string): string {
  if (!text.includes(".")) return text;
  return text.replace(/0+$/, "").replace(/\.$/, "");
}

/** Render a dollar-string price as cents, e.g. "0.4200" -> "42.0¢". */
export function asCents(dollars: string | null | undefined, dp = 1): string {
  const value = centsNum(dollars, dp);
  return value === "—" ? value : `${value}¢`;
}

/** Bare cents number without the symbol, for dense table columns. */
export function centsNum(dollars: string | null | undefined, dp = 1): string {
  if (dollars === null || dollars === undefined || dollars === "") return "—";
  const d = toDec(dollars);
  if (d === null) return "—";
  return render(shift(d, -2), dp);
}

/**
 * An amount that already arrived in cents, rendered in cents.
 *
 * This is the honest formatter for a fee: the schedule rounds to a centicent
 * and one contract at 50¢ costs 1.75¢, which no dollars-with-two-decimals
 * rendering can express.
 */
export function asCentsAmount(
  cents: string | null | undefined,
  dp = 2,
): string {
  if (cents === null || cents === undefined || cents === "") return "—";
  const d = toDec(cents);
  if (d === null) return "—";
  if (tooSmall(d, MAX_CENT_DP)) return `${d.neg ? "-" : ""}<0.0001¢`;
  return `${renderSignificant(d, dp, MAX_CENT_DP)}¢`;
}

/** Render a contract count compactly: 15234 -> "15.2k".
 *
 * Kalshi supports fractional contracts down to 0.01, so a fractional size
 * keeps its decimals at every magnitude — rounding 0.66 to "1", 0.4 to "0" or
 * a real 484.69-lot print to "485" all misreport what actually traded.
 */
export function asCount(count: string | null | undefined): string {
  if (count === null || count === undefined || count === "") return "—";
  const d = toDec(count);
  if (d === null) return "—";
  if (isZero(d)) return "0";
  // `int` carries no leading zeros, so its length is the magnitude.
  const magnitude = d.int === "0" ? 0 : d.int.length;
  if (magnitude >= 7) return `${render(shift(d, 6), 1)}M`;
  if (magnitude >= 4) return `${render(shift(d, 3), 1)}k`;
  return trimZeros(render(d, 2));
}

/** Human-readable time until close. */
export function asTimeToClose(hours: number | null): string {
  if (hours === null) return "—";
  if (hours < 0) return "closed";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours / 24)}d`;
}

function usd(d: Dec): string {
  const r = round(d, 2);
  const body = `$${group(r.int)}.${r.frac}`;
  return r.neg && !isZero(r) ? `-${body}` : body;
}

/**
 * Render a cents-string as money: "5175.00" -> "$51.75".
 *
 * **Under a dollar the figure is rendered in cents instead**, and that is the
 * whole point of this function. Dollars-at-two-decimals cannot express what
 * the exchange actually bills: a 1.75¢ fee came out as "$0.02" — the exact
 * number CLAUDE.md's units section says is wrong — a 0.54¢ worst case as
 * "$0.01", and a real 0.04¢ fee as "$0.00". The rule is one line: use the
 * unit that can carry the value. Above a dollar the sub-cent tail is noise
 * and dollars read better; below one, it is the value.
 */
export function asDollars(cents: string | null | undefined): string {
  if (cents === null || cents === undefined || cents === "") return "—";
  const d = toDec(cents);
  if (d === null) return "—";
  // `int` carries no leading zeros, so a length of 2 or less is under 100¢.
  if (!isZero(d) && d.int.length <= 2) return asCentsAmount(cents);
  return usd(shift(d, 2));
}

/** Money that arrived already in dollars (the LLM budget, account balance). */
export function asUsd(dollars: string | null | undefined): string {
  if (dollars === null || dollars === undefined || dollars === "") return "—";
  const d = toDec(dollars);
  if (d === null) return "—";
  return usd(d);
}

/** A cents figure with an explicit sign, for P&L and edges. */
export function asSignedCents(
  cents: string | null | undefined,
  dp = 2,
): string {
  if (cents === null || cents === undefined || cents === "") return "—";
  const d = toDec(cents);
  if (d === null) return "—";
  if (tooSmall(d, MAX_CENT_DP)) return `${d.neg ? "-" : "+"}<0.0001¢`;
  const text = renderSignificant(d, dp, MAX_CENT_DP);
  return `${text.startsWith("-") ? "" : "+"}${text}¢`;
}

/**
 * Magnitude of a money string, still a string.
 *
 * A meter that fills with a loss wants the loss as a positive quantity;
 * negating it through `Number` put a float round trip in front of a figure
 * the operator reads as today's P&L.
 */
export function absCents(cents: string | null | undefined): string {
  if (cents === null || cents === undefined || cents === "") return "0";
  const d = toDec(cents);
  if (d === null) return "0";
  return d.frac ? `${d.int}.${d.frac}` : d.int;
}

/**
 * Sign of a money string: -1, 0 or 1.
 *
 * For picking a CSS class, never for display. Zero is its own answer on
 * purpose — a realised P&L of exactly zero rendered in the green reserved for
 * gains, which it is not.
 */
export function moneySign(cents: string | null | undefined): -1 | 0 | 1 {
  if (cents === null || cents === undefined || cents === "") return 0;
  const d = toDec(cents);
  if (d === null || isZero(d)) return 0;
  return d.neg ? -1 : 1;
}

/** Human label for an execution route. */
export function routeLabel(route: string | null): string {
  switch (route) {
    case "simulated":
      return "simulated";
    case "demo_exchange":
      return "demo exchange";
    case "live_exchange":
      return "LIVE — real money";
    default:
      return "unavailable";
  }
}

/**
 * Locale formatting that cannot take a panel down with it.
 *
 * `Date#toLocaleTimeString` throws `RangeError: Incorrect locale information
 * provided` when the environment's default locale is not a valid BCP-47 tag —
 * a container with no `LANG` set resolves `navigator.language` to
 * `"en-US@posix"`, which Intl rejects. A formatter is called from inside
 * render, so one throw blanks the whole component that called it and leaves
 * nothing on the page saying why. Every locale-dependent format in this app
 * goes through the two functions below, which fall back to a plain
 * ISO-derived rendering rather than throwing.
 *
 * The fallback is deliberately the browser's *local* clock, same as the happy
 * path — a timestamp that silently switches to UTC on some machines is worse
 * than an ugly one.
 */
export function asClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    // "14:03:22 GMT+0000 (…)" -> "14:03:22", already in local time.
    return d.toTimeString().slice(0, 8);
  }
}

/** Date + time, same defensive contract as `asClock`. */
export function asDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return d.toLocaleString();
  } catch {
    return `${d.toDateString()} ${d.toTimeString().slice(0, 8)}`;
  }
}
