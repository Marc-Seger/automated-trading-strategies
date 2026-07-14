"""
strategies/bollinger_bounce.py
-------------------------------
Plain Bollinger Band bounce strategy (not the BB Channel Rider).

Long  signal: close crosses back above lower band after dipping below it
Short signal: close crosses back below upper band after spiking above it

This is the simple "bounce" variant. For the full Channel Rider strategy
with trend filter, SL snap, and flip logic, see strategies/bb_channel.py.

Public interface
----------------
  simulate(candles, params, ...) -> list[dict]
  indicators(candles, params)    -> dict of named series
"""

from __future__ import annotations

from typing import Optional

from strategies.indicators import compute_bollinger


DEFAULTS = {
    "period":    20,
    "std_dev":   2.0,
    "direction": "both",
}


def _signals(candles: list, params: dict) -> list[dict]:
    period    = int(params.get("period",    DEFAULTS["period"]))
    std_dev   = float(params.get("std_dev", DEFAULTS["std_dev"]))
    direction = params.get("direction",     DEFAULTS["direction"])

    bands   = compute_bollinger(candles, period, std_dev)
    closes  = [c[4] for c in candles]
    signals = []

    for i in range(1, len(candles)):
        if bands[i] is None or bands[i-1] is None:
            continue
        upper    = bands[i][0]      # type: ignore[index]
        lower    = bands[i][2]      # type: ignore[index]
        prev_upper = bands[i-1][0]  # type: ignore[index]
        prev_lower = bands[i-1][2]  # type: ignore[index]

        if direction in ("long", "both") \
                and closes[i-1] < prev_lower and closes[i] > lower:
            signals.append({"candle_idx": i, "direction": "long"})
        elif direction in ("short", "both") \
                and closes[i-1] > prev_upper and closes[i] < upper:
            signals.append({"candle_idx": i, "direction": "short"})

    return signals


def simulate(
    candles:          list,
    params:           dict,
    sl_pct:           float = 0.05,
    tp_pct:           float = 0.10,
    leverage:         int   = 1,
    fee_rate:         float = 0.0,
    starting_capital: float = 1000.0,
) -> list[dict]:
    raw_signals = _signals(candles, params)
    trades: list[dict] = []
    open_until_idx: Optional[int] = None

    for sig in raw_signals:
        idx = sig["candle_idx"]
        if open_until_idx is not None and idx <= open_until_idx:
            continue

        entry_candle = candles[idx]
        entry_price  = entry_candle[1]
        sig_dir      = sig["direction"]
        is_long      = sig_dir == "long"

        sl = entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
        tp = entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)

        outcome    = "open"
        exit_price: Optional[float] = None
        exit_idx:   Optional[int]   = None

        for j in range(idx + 1, len(candles)):
            _, _o, h, l, _c, _v = candles[j]
            if (is_long and l <= sl) or (not is_long and h >= sl):
                outcome = "stop_loss";   exit_price = sl; exit_idx = j; break
            if (is_long and h >= tp) or (not is_long and l <= tp):
                outcome = "take_profit"; exit_price = tp; exit_idx = j; break

        if outcome == "take_profit":
            raw_move = (tp - entry_price) / entry_price
        elif outcome == "stop_loss":
            raw_move = (sl - entry_price) / entry_price
        else:
            raw_move = (candles[-1][4] - entry_price) / entry_price

        pnl_move      = raw_move if is_long else -raw_move
        gross_pnl_pct = round(pnl_move * leverage * 100, 2)
        fee_pct       = round(2 * fee_rate * leverage * 100, 4) \
                        if outcome in ("take_profit", "stop_loss") else 0.0
        net_pnl_pct   = round(gross_pnl_pct - fee_pct, 2)

        entry_ts_ms = entry_candle[0]
        exit_ts_ms  = candles[exit_idx][0] if exit_idx is not None else candles[-1][0]
        dur_h       = round((exit_ts_ms - entry_ts_ms) / 3_600_000, 2)

        trades.append({
            "candle_idx":     idx,
            "direction":      sig_dir,
            "entry_ts_ms":    entry_ts_ms,
            "exit_ts_ms":     exit_ts_ms if outcome != "open" else None,
            "entry":          entry_price,
            "sl":             sl,
            "tp":             tp,
            "exit_price":     exit_price,
            "outcome":        outcome,
            "pnl_pct":        net_pnl_pct,
            "gross_pnl_pct":  gross_pnl_pct,
            "fee_pct":        fee_pct,
            "price_move_pct": round(raw_move * 100, 2),
            "leverage":       leverage,
            "duration_hours": dur_h,
        })

        open_until_idx = exit_idx if exit_idx is not None else len(candles) - 1

    return trades


def indicators(candles: list, params: dict) -> dict:
    period  = int(params.get("period",    DEFAULTS["period"]))
    std_dev = float(params.get("std_dev", DEFAULTS["std_dev"]))
    bands   = compute_bollinger(candles, period, std_dev)
    return {
        "bb_upper": [b[0] if b else None for b in bands],
        "bb_mid":   [b[1] if b else None for b in bands],
        "bb_lower": [b[2] if b else None for b in bands],
    }
