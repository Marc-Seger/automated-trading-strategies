"""
strategies/indicators.py
------------------------
Shared indicator computations used by all strategy modules and the dashboard.

Functions here operate on raw OHLCV candle lists:
  candle = [timestamp_ms, open, high, low, close, volume]

All functions return lists of the same length as the input, with None
for candles where there is not yet enough history to compute the value.
"""

from __future__ import annotations

import math
from typing import Optional


# ── Moving averages ───────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple moving average."""
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """
    Exponential moving average, seeded with SMA of the first `period` values.
    k = 2 / (period + 1)
    """
    out: list[Optional[float]] = [None] * len(values)
    k        = 2.0 / (period + 1)
    seed_idx = period - 1
    if seed_idx >= len(values):
        return out
    out[seed_idx] = sum(values[:period]) / period
    for i in range(seed_idx + 1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)  # type: ignore[operator]
    return out


def compute_ma(
    candles: list,
    period:  int,
    ma_type: str = "ema",
) -> list[Optional[float]]:
    """Compute SMA or EMA of close prices."""
    closes = [c[4] for c in candles]
    return sma(closes, period) if ma_type == "sma" else ema(closes, period)


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def compute_bollinger(
    candles: list,
    period:  int   = 20,
    std_dev: float = 2.0,
) -> list[Optional[tuple[float, float, float]]]:
    """
    Bollinger Bands using population variance (same formula as bb_channel core).
    Returns list of (upper, mid, lower) or None per candle.
    """
    closes = [c[4] for c in candles]
    mids   = sma(closes, period)
    out: list[Optional[tuple[float, float, float]]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window   = closes[i - period + 1 : i + 1]
        mean     = mids[i]
        variance = sum((x - mean) ** 2 for x in window) / period  # type: ignore[operator]
        sd       = math.sqrt(variance)
        out[i]   = (mean + std_dev * sd, mean, mean - std_dev * sd)  # type: ignore[arg-type]
    return out


# ── RSI ───────────────────────────────────────────────────────────────────────

def compute_rsi(candles: list, period: int = 14) -> list[Optional[float]]:
    """Wilder RSI using exponential smoothing of gains/losses."""
    closes = [c[4] for c in candles]
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains    = [max(d, 0.0) for d in deltas]
    losses   = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period])  / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        di       = i - 1
        avg_gain = (avg_gain * (period - 1) + gains[di])  / period
        avg_loss = (avg_loss * (period - 1) + losses[di]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs     = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1 + rs)
    return out
