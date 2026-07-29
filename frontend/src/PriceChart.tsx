import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  type AutoscaleInfo,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle } from "./api";

/**
 * Candles rendered with TradingView Lightweight Charts.
 *
 * Prices are plotted in **cents** rather than dollars: a prediction market
 * lives between 0 and 100¢ and traders read it that way. The conversion
 * happens here at the display boundary, not in stored data.
 */
export default function PriceChart({
  candles,
  height = 320,
}: {
  candles: Candle[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  // Create once.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      height,
      // Pin the locale instead of letting the library detect one.
      //
      // Lightweight Charts defaults `localization.locale` to
      // `navigator.language` and hands it straight to `Date.toLocaleString()`
      // in its tick-mark formatter. A container with no `LANG` set resolves
      // that to "en-US@posix" — not a valid BCP-47 tag — so every axis tick
      // threw `RangeError: Incorrect locale information provided`, the whole
      // canvas rendered blank with valid candle data behind it, and nothing
      // in the DOM said the chart had failed. Only devtools showed it.
      //
      // The axis is UTC-keyed exchange time, not the operator's calendar, so
      // there is nothing here worth deferring to a machine setting for.
      localization: { locale: "en-US" },
      layout: {
        background: { type: ColorType.Solid, color: "#101010" },
        textColor: "#8a8a8a",
        fontFamily:
          'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1a1a1a" },
        horzLines: { color: "#1a1a1a" },
      },
      rightPriceScale: {
        borderColor: "#242424",
        // Keep the bottom margin small. Reserving a large band here makes
        // Lightweight Charts label the *extrapolated* space below the data,
        // which prints axis ticks like -20¢ on a 0-100¢ instrument. Volume
        // overlays on its own scale instead of taking space from this one.
        scaleMargins: { top: 0.08, bottom: 0.02 },
      },
      timeScale: { borderColor: "#242424", timeVisible: true, secondsVisible: false },
      crosshair: {
        vertLine: { color: "#333", labelBackgroundColor: "#00d68f" },
        horzLine: { color: "#333", labelBackgroundColor: "#00d68f" },
      },
    });

    const price = chart.addCandlestickSeries({
      upColor: "#00d68f",
      downColor: "#d64545",
      borderUpColor: "#00d68f",
      borderDownColor: "#d64545",
      wickUpColor: "#00d68f",
      wickDownColor: "#d64545",
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
      // A contract can only settle between 0¢ and 100¢. Autoscale padding
      // would otherwise render axis labels at -20¢, which is not a price.
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const info = original();
        if (!info?.priceRange) return info;
        return {
          ...info,
          priceRange: {
            minValue: Math.max(0, info.priceRange.minValue),
            maxValue: Math.min(100, info.priceRange.maxValue),
          },
        };
      },
    });

    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: "#242424",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    chartRef.current = chart;
    priceRef.current = price;
    volumeRef.current = volume;

    const resize = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth });
    });
    resize.observe(container);
    chart.applyOptions({ width: container.clientWidth });

    return () => {
      resize.disconnect();
      chart.remove();
      chartRef.current = null;
      priceRef.current = null;
      volumeRef.current = null;
    };
  }, [height]);

  // Feed data.
  useEffect(() => {
    const price = priceRef.current;
    const volume = volumeRef.current;
    if (!price || !volume) return;

    const bars: CandlestickData[] = [];
    const volumes: HistogramData[] = [];

    for (const c of candles) {
      if (c.close === null) continue;
      const toCents = (v: string | null) => Number(v ?? c.close) * 100;
      const open = toCents(c.open);
      const close = toCents(c.close);

      bars.push({
        time: c.ts as UTCTimestamp,
        open,
        high: toCents(c.high),
        low: toCents(c.low),
        close,
      });
      volumes.push({
        time: c.ts as UTCTimestamp,
        value: Number(c.volume ?? 0),
        color: close >= open ? "#00d68f33" : "#d6454533",
      });
    }

    price.setData(bars);
    volume.setData(volumes);
    if (bars.length) chartRef.current?.timeScale().fitContent();
  }, [candles]);

  return <div ref={containerRef} className="chart" style={{ height }} />;
}
