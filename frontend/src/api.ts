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
  limit_price: string;
  contracts: string;
  fair_price: string | null;
  est_fee_cents: string | null;
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

  settlements: () =>
    getJson<{ settlements: SettlementRow[] }>("/api/settlements?limit=50"),
};

// ---------------------------------------------------------------------------
// Formatting. All input is a decimal string; none of it becomes a number
// before display.
// ---------------------------------------------------------------------------

/** Render a dollar-string price as cents, e.g. "0.4200" -> "42.0¢". */
export function asCents(dollars: string | null | undefined, dp = 1): string {
  if (dollars === null || dollars === undefined || dollars === "") return "—";
  const value = Number(dollars) * 100;
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(dp)}¢`;
}

/** Bare cents number without the symbol, for dense table columns. */
export function centsNum(dollars: string | null | undefined, dp = 1): string {
  if (dollars === null || dollars === undefined || dollars === "") return "—";
  const value = Number(dollars) * 100;
  if (!Number.isFinite(value)) return "—";
  return value.toFixed(dp);
}

/** Render a contract count compactly: 15234 -> "15.2k".
 *
 * Kalshi supports fractional contracts down to 0.01, so small sizes keep
 * their decimals — rounding 0.66 to "1" or 0.4 to "0" would misreport a real
 * print as nothing.
 */
export function asCount(count: string | null | undefined): string {
  if (count === null || count === undefined || count === "") return "—";
  const value = Number(count);
  if (!Number.isFinite(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  if (value === 0) return "0";
  if (value < 10 && !Number.isInteger(value)) return value.toFixed(2);
  return value.toFixed(0);
}

/** Human-readable time until close. */
export function asTimeToClose(hours: number | null): string {
  if (hours === null) return "—";
  if (hours < 0) return "closed";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours / 24)}d`;
}

/** Render a cents-string as money: "5175.00" -> "$51.75". */
export function asDollars(cents: string | null | undefined): string {
  if (cents === null || cents === undefined || cents === "") return "—";
  const value = Number(cents) / 100;
  if (!Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

/** A cents figure with an explicit sign, for P&L and edges. */
export function asSignedCents(
  cents: string | null | undefined,
  dp = 2,
): string {
  if (cents === null || cents === undefined || cents === "") return "—";
  const value = Number(cents);
  if (!Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(dp)}¢`;
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

export function asClock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
