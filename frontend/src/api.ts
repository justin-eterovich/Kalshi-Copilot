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
    maker_rate_fraction: number;
    unverified_categories: string[];
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

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new ApiError(`${path} -> HTTP ${response.status}`, response.status);
  }
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

export function asClock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
