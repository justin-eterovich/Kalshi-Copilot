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
  category: string | null;
  status: string | null;
  yes_bid: string | null;
  yes_ask: string | null;
  last_price: string | null;
  spread: string | null;
  volume: string | null;
  volume_24h: string | null;
  open_interest: string | null;
  close_time: string | null;
  hours_to_close: number | null;
  first_seen_at: string | null;
}

export interface MarketsPage {
  total: number;
  limit: number;
  offset: number;
  markets: MarketRow[];
}

export interface CatalogStats {
  markets: number;
  markets_active: number;
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

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  system: () => getJson<SystemStatus>("/api/system"),
  health: () => getJson<Health>("/api/health"),
  catalogStats: () => getJson<CatalogStats>("/api/catalog/stats"),
  markets: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return getJson<MarketsPage>(`/api/markets?${query}`);
  },
};

/** Render a dollar-string price as cents, e.g. "0.4200" -> "42.0¢". */
export function asCents(dollars: string | null): string {
  if (dollars === null || dollars === "") return "—";
  const value = Number(dollars) * 100;
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}¢`;
}

/** Render a contract count compactly: 15234 -> "15.2k". */
export function asCount(count: string | null): string {
  if (count === null || count === "") return "—";
  const value = Number(count);
  if (!Number.isFinite(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
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
