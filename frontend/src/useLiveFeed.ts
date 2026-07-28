import { useEffect, useRef, useState } from "react";

/** One message from the backend relay. */
export interface FeedMessage {
  channel: string;
  data: Record<string, unknown>;
}

export type FeedStatus = "connecting" | "live" | "offline";

/**
 * What "live" here does and does not mean.
 *
 * It is the state of the **relay socket**, not of the data. If ingest stops
 * publishing to Redis while this socket stays open, the pill keeps saying
 * live. Every surface that shows it should carry this as a tooltip so the
 * word is not read as "these numbers are current" — the honest freshness
 * signals are the per-panel ones (`live`/`cached`, the quote read time).
 */
export const FEED_STATUS_TITLE =
  "connection to the dashboard's relay socket — not a guarantee that data is flowing";

/**
 * Subscribe to the backend WebSocket relay.
 *
 * Reconnects with backoff. Pass `tickers` to filter the tick firehose down to
 * the markets on screen — the scanner streams every market, and a market page
 * wants one of them.
 *
 * The handler is held in a ref so a caller can pass an inline closure without
 * tearing down and re-establishing the socket on every render.
 */
export function useLiveFeed(
  tickers: string[],
  onMessage: (message: FeedMessage) => void,
): FeedStatus {
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const handlerRef = useRef(onMessage);
  const socketRef = useRef<WebSocket | null>(null);

  handlerRef.current = onMessage;

  // Stable identity so the effect below only re-runs on a real change.
  const watchKey = tickers.slice().sort().join(",");

  useEffect(() => {
    let closed = false;
    let attempt = 0;
    let retryTimer: number | undefined;

    const connect = () => {
      if (closed) return;

      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${window.location.host}/ws`);
      socketRef.current = socket;
      setStatus("connecting");

      socket.onopen = () => {
        if (closed) return;
        attempt = 0;
        setStatus("live");
        socket.send(
          JSON.stringify({
            action: "watch",
            tickers: watchKey ? watchKey.split(",") : [],
          }),
        );
      };

      socket.onmessage = (event) => {
        try {
          handlerRef.current(JSON.parse(event.data) as FeedMessage);
        } catch {
          /* ignore malformed frames */
        }
      };

      socket.onclose = () => {
        if (closed) return;
        setStatus("offline");
        const delay = Math.min(1000 * 2 ** attempt, 15000);
        attempt += 1;
        retryTimer = window.setTimeout(connect, delay);
      };

      socket.onerror = () => socket.close();
    };

    connect();

    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [watchKey]);

  return status;
}
