"""
strategies/bb_channel.py
------------------------
Pure strategy logic for the BB Channel Rider.

No exchange calls, no file I/O, no Telegram — only data in, decisions out.
Both bots/bb_bot.py (live/paper) and backtest/indicator_backtest.py import
from here. Parity between backtest and live bot is guaranteed by construction.

See STRATEGY_SPEC.md for the full locked specification (2026-05-10).
"""

from __future__ import annotations

import math
from typing import Optional

# ── Frozen strategy constants ─────────────────────────────────────────────────
# Change only after re-running Gate 2 grid search and full audit.
BB_PERIOD     = 20
BB_STD        = 3.0
TREND_PERIOD  = 150
SL_PCT        = 0.01     # 1.0% stop-limit SL from entry
BACKSTOP_GAP  = 0.005    # extra 0.5% below stop-limit → stop-market backstop at 1.5%
SNAP_THRESH   = 0.95     # 95% of mid→band distance triggers SL snap
COOLDOWN_N    = 2        # candles of cooldown after SL (per direction)
SIZING_PCT    = 0.25     # fraction of capital deployed per trade
CONTRACT_LOT  = 0.0001   # BTC per contract on MEXC perpetuals

# Fee rates (as decimals, not percent)
FEE_TAKER = 0.0006   # 0.06% — market orders (entry, TP exit, backstop SL)
FEE_MAKER = 0.0004   # 0.04% — limit orders (stop-limit SL)


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_bb(
    candles: list,
    period:  int   = BB_PERIOD,
    std_dev: float = BB_STD,
) -> list:
    """
    Bollinger Bands using population variance (not sample variance).
    Returns list of (upper, mid, lower) or None, same length as candles.
    """
    closes = [c[4] for c in candles]
    n      = len(closes)
    out    = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mid    = sum(window) / period
        sd     = math.sqrt(sum((x - mid) ** 2 for x in window) / period)
        out[i] = (mid + std_dev * sd, mid, mid - std_dev * sd)
    return out


def compute_ema(
    candles: list,
    period:  int = TREND_PERIOD,
) -> list:
    """
    EMA seeded with SMA of the first `period` closes.
    Returns list of float or None, same length as candles.
    """
    closes = [c[4] for c in candles]
    n      = len(closes)
    out    = [None] * n
    k      = 2.0 / (period + 1)
    prev   = None
    for i, v in enumerate(closes):
        if prev is None:
            if i >= period - 1:
                prev   = sum(closes[i - period + 1 : i + 1]) / period
                out[i] = prev
        else:
            prev   = v * k + prev * (1 - k)
            out[i] = prev
    return out


# ── Entry signal ──────────────────────────────────────────────────────────────

def evaluate_entry(
    last_candle: list,
    prev_candle: list,
    last_bb:     tuple,
    prev_ema:    Optional[float],
    long_cd:     int,
    short_cd:    int,
) -> Optional[str]:
    """
    Check entry conditions on the just-closed candle.
    Uses the wick (high/low) for band touch, prev candle for trend filter.

    Pass prev_ema=None to disable the trend filter entirely (useful in
    dashboard backtest experiments).

    Returns "long", "short", or None.
    """
    h      = last_candle[2]
    l      = last_candle[3]
    prev_c = prev_candle[4]
    upper, _mid, lower = last_bb

    ok_long  = (prev_ema is None) or (prev_c > prev_ema)
    ok_short = (prev_ema is None) or (prev_c < prev_ema)

    if long_cd == 0 and l <= lower and ok_long:
        return "long"
    if short_cd == 0 and h >= upper and ok_short:
        return "short"
    return None


# ── Order price helpers ───────────────────────────────────────────────────────

def sl_price(
    direction: str,
    entry:     float,
    sl_pct:    float = SL_PCT,
) -> float:
    """Stop-limit SL: 1% from entry (maker fee on fill)."""
    return entry * (1 - sl_pct) if direction == "long" else entry * (1 + sl_pct)


def backstop_price(
    direction:    str,
    entry:        float,
    sl_pct:       float = SL_PCT,
    backstop_gap: float = BACKSTOP_GAP,
) -> float:
    """
    Stop-market backstop: (sl_pct + backstop_gap) from entry.
    Only fires when price gaps through the stop-limit without filling.
    Default: 1.5% from entry, 0.5% below the stop-limit.
    """
    total = sl_pct + backstop_gap
    return entry * (1 - total) if direction == "long" else entry * (1 + total)


def tp_price(direction: str, bb: tuple) -> float:
    """TP = opposite band (upper for long, lower for short)."""
    upper, _mid, lower = bb
    return upper if direction == "long" else lower


def snap_trigger(
    direction: str,
    bb:        tuple,
    thresh:    float = SNAP_THRESH,
) -> float:
    """
    Price level at which SL snaps to mid-band.
    Recomputed from current closed-candle BB each candle (not fixed at entry).
    Long:  mid + 95% × (upper − mid)
    Short: mid − 95% × (mid − lower)
    """
    upper, mid, lower = bb
    if direction == "long":
        return mid + thresh * (upper - mid)
    return mid - thresh * (mid - lower)


# ── Position sizing ───────────────────────────────────────────────────────────

def contracts(
    capital:     float,
    entry_price: float,
    sizing_pct:  float = SIZING_PCT,
    lot:         float = CONTRACT_LOT,
    leverage:    int   = 10,
) -> int:
    """
    MEXC contract quantity for the trade.

    sizing_pct is the fraction of capital used as MARGIN (collateral).
    The notional position = margin × leverage, so:
        contracts = floor(capital × sizing_pct × leverage / (entry_price × lot))

    Example: capital=$1000, sizing_pct=0.25, leverage=10, price=$80000, lot=0.0001
        margin   = $250
        notional = $2500
        contracts = floor(2500 / (80000 × 0.0001)) = floor(2500 / 8) = 312

    Returns 0 when capital is insufficient — caller must handle the skip.
    """
    return math.floor(capital * sizing_pct * leverage / (entry_price * lot))


# ── In-position checks ────────────────────────────────────────────────────────

def check_snap_triggered(
    direction: str,
    h:         float,
    l:         float,
    snap_at:   float,
) -> bool:
    """Did this candle's wick cross the SL snap threshold?"""
    return h >= snap_at if direction == "long" else l <= snap_at


def trail_sl(
    direction:  str,
    current_sl: float,
    mid:        float,
) -> float:
    """
    Monotone SL trail after snap fires.
    Long: SL only ever moves up   → max(current_sl, mid)
    Short: SL only ever moves down → min(current_sl, mid)
    Returns the new SL (unchanged if mid hasn't moved favorably).
    """
    return max(current_sl, mid) if direction == "long" else min(current_sl, mid)


def check_tp_hit(direction: str, h: float, l: float, tp: float) -> bool:
    """Did the candle wick reach the TP level?"""
    return h >= tp if direction == "long" else l <= tp


def check_sl_hit(direction: str, h: float, l: float, sl: float) -> bool:
    """Did the candle wick reach the SL level?"""
    return l <= sl if direction == "long" else h >= sl


# ── Flip validity ─────────────────────────────────────────────────────────────

def flip_valid(
    closed_direction: str,
    current_price:    float,
    bb:               tuple,
    prev_ema:         float,
    prev_close:       float,
    long_cd:          int,
    short_cd:         int,
) -> bool:
    """
    After a TP hit, check whether an immediate flip into the opposite direction
    is valid. Three conditions must all pass:
      1. Current price is still at (or beyond) the band — not already pulled back
      2. Trend filter: prev candle close confirms the new direction
      3. No cooldown active for the new direction
    """
    upper, _mid, lower = bb
    flip_dir = "short" if closed_direction == "long" else "long"

    if flip_dir == "short":
        return current_price >= upper and prev_close < prev_ema and short_cd == 0
    return current_price <= lower and prev_close > prev_ema and long_cd == 0


# ── Cooldown ──────────────────────────────────────────────────────────────────

def advance_cooldown(
    long_cd:    int,
    short_cd:   int,
    last_close: float,
    bb:         Optional[tuple],
) -> tuple:
    """
    Advance per-direction cooldown counters by one candle.

    Each direction checks only its own relevant band (matches backtest exactly):
      long_cd  decrements when close > lower band (recovering from lower-band SL)
      short_cd decrements when close < upper band (recovering from upper-band SL)

    If BB is unavailable, both counters decrement unconditionally.
    """
    if bb is not None:
        upper, _mid, lower = bb
        new_long_cd  = max(0, long_cd  - 1) if long_cd  > 0 and last_close > lower  else long_cd
        new_short_cd = max(0, short_cd - 1) if short_cd > 0 and last_close < upper  else short_cd
    else:
        new_long_cd  = max(0, long_cd  - 1)
        new_short_cd = max(0, short_cd - 1)
    return new_long_cd, new_short_cd


# ── P&L ──────────────────────────────────────────────────────────────────────

def calc_pnl(
    direction:      str,
    entry:          float,
    exit_price:     float,
    leverage:       int,
    capital:        float,
    sizing_pct:     float = SIZING_PCT,
    fee_entry_rate: float = FEE_TAKER,  # market order entry
    fee_exit_rate:  float = FEE_TAKER,  # TP path default (market); pass FEE_MAKER for stop-limit SL
) -> tuple:
    """
    Net P&L after round-trip fees, with 100% compounding.

    Fee guide:
      TP exit (market)          → fee_exit_rate = FEE_TAKER (0.06%)  [default]
      SL exit (stop-limit)      → fee_exit_rate = FEE_MAKER (0.04%)
      SL exit (backstop market) → fee_exit_rate = FEE_TAKER (0.06%)  [default]

    Returns (pnl_pct, pnl_usdt) where:
      pnl_pct  = net return on collateral (leveraged), after fees, as a percentage
      pnl_usdt = absolute P&L in USDT on the deployed capital slice
    """
    raw_move  = (exit_price - entry) / entry if direction == "long" else (entry - exit_price) / entry
    gross_pct = raw_move * leverage * 100
    fee_pct   = (fee_entry_rate + fee_exit_rate) * leverage * 100
    net_pct   = gross_pct - fee_pct
    pnl_usdt  = (net_pct / 100) * (capital * sizing_pct)
    return round(net_pct, 4), round(pnl_usdt, 4)


# ── Backtest entry point ──────────────────────────────────────────────────────

def simulate(
    candles:          list,
    params:           dict,
    sl_pct:           float = SL_PCT,   # overridable via params["sl_pct"] or this arg
    tp_pct:           float = 0.10,     # ignored — TP is always the opposite band
    leverage:         int   = 10,
    fee_rate:         Optional[float] = None,  # None = live-accurate per-leg model; a float = both legs at that rate
    starting_capital: float = 1000.0,
) -> list[dict]:
    """
    BB Channel Rider backtest simulation.

    All parameters are overridable via the params dict so the dashboard can
    let users experiment freely. The frozen live-bot defaults are used when
    a key is absent. The live bot (bb_bot.py) imports the module-level
    constants directly and never calls this function.

    Fee model:
      - fee_rate=None (default) — model what the live bot actually pays, per leg:
            entry            → FEE_TAKER (0.06%), bb_bot.py enters with a market order
            exit via TP band → FEE_TAKER (0.06%), bb_bot.py closes TP with a market order
            exit via SL      → FEE_MAKER (0.04%), the stop-limit fills as a maker
        The stop-market backstop (also taker) only fires when price gaps through the
        stop-limit; it is rare and not modelled, so SL exits are costed at the
        optimistic maker rate.
      - fee_rate=<float> — override, applied to BOTH legs. Used by the dashboard's
        fee dropdown (none / spot / api_maker / api_taker) for what-if comparisons.

      This previously defaulted to FEE_MAKER on both legs, with a docstring claiming
      "all orders are limit/stop-limit so maker fee applies everywhere". That was
      wrong: entry and TP exit are both market orders. At 10x the understatement is
      material — a real trade on 2026-08-09 closed at -$1.66 despite exiting above
      entry, purely because taker fees on both legs exceeded a +0.058% raw move.

    Key mechanics:
      - SL  = sl_pct from entry (default 1%); params["sl_pct"] overrides
      - TP  = opposite BB band at each candle close — dynamic, not percentage-based
      - SL snap: when wick crosses snap_thresh of mid→band distance, SL → mid
      - snap_at fixed at entry time; never recomputed mid-trade
      - Cooldown: cooldown_n candles (idle only) after SL, per direction
      - TP wins when both TP and SL are touched in the same candle
      - outcome derived from P&L sign (handles band-drift edge cases)
      - exit_trigger records mechanical reason (band hit vs sl)
    """
    period       = int(params.get("period",       BB_PERIOD))
    std_dev      = float(params.get("std_dev",     BB_STD))
    trend_period = int(params.get("trend_period",  TREND_PERIOD))
    sizing_pct   = float(params.get("sizing_pct",  SIZING_PCT))
    dir_filter   = params.get("direction", "both")
    # These can all be tuned in the dashboard; live bot uses module constants directly
    sl_pct       = float(params.get("sl_pct",       sl_pct))        # default: SL_PCT=0.01
    snap_thresh  = float(params.get("sl_threshold_pct", SNAP_THRESH))  # default: 0.95
    cooldown_n   = int(params.get("cooldown_n",     COOLDOWN_N))    # default: 2
    trend_on     = bool(params.get("trend_filter",  True))          # default: on
    # Experimental (2026-08-09): floor on how close TP can compress back
    # toward entry as the BB channel narrows during an open trade. Default
    # 0.0 = disabled, matches current frozen live behavior exactly. Motivated
    # by a TP that closed at a net loss once fees were charged; backtested at
    # 2x/3x/5x the fee cost and worse at every threshold, so it stays off.
    min_tp_pct   = float(params.get("min_tp_pct",   0.0))

    bb_series  = compute_bb(candles, period, std_dev)
    ema_series = compute_ema(candles, trend_period)

    trades:      list[dict]    = []
    capital:     float         = starting_capital
    long_cd:     int           = 0
    short_cd:    int           = 0
    phase:       str           = "idle"

    # Active-position state
    direction:   Optional[str]   = None
    entry_price: Optional[float] = None
    entry_ts_ms: Optional[int]   = None
    entry_idx:   Optional[int]   = None
    current_sl:  Optional[float] = None
    snap_at:     Optional[float] = None   # fixed at entry time (not recomputed)
    snap_fired:  bool            = False

    warmup = max(period, trend_period)

    for i in range(warmup, len(candles)):
        ts, _o, h, l, c, _v = candles[i]
        bb  = bb_series[i]

        if bb is None:
            continue

        prev_ema   = ema_series[i - 1]
        prev_close = candles[i - 1][4]

        if prev_ema is None:
            continue

        # ── Idle: advance cooldowns first, then check entry ───────────────
        if phase == "idle":
            # Advance cooldown at start of idle candle (matches original: only
            # decrements when not in position, and SL candle skips via continue).
            lcd_start = long_cd
            scd_start = short_cd
            if long_cd  > 0 and c > bb[2]:   # close > lower band
                long_cd  -= 1
            if short_cd > 0 and c < bb[0]:   # close < upper band
                short_cd -= 1

            # Pass None when trend filter is disabled so evaluate_entry skips
            # the trend check (None is the agreed sentinel — see docstring)
            sig = evaluate_entry(
                candles[i], candles[i - 1], bb,
                prev_ema if trend_on else None,
                lcd_start, scd_start,   # check against pre-advance counts
            )
            if sig is not None and (dir_filter == "both" or sig == dir_filter):
                direction    = sig
                upper_b, _mid_b, lower_b = bb
                # Entry at band level (limit order fill) — matches validated backtest
                entry_price  = lower_b if direction == "long" else upper_b
                entry_ts_ms  = ts
                entry_idx    = i
                current_sl   = sl_price(direction, entry_price, sl_pct)
                # Snap threshold fixed at entry from entry-candle BB
                snap_at      = snap_trigger(direction, bb, snap_thresh)
                snap_fired   = False
                phase        = "in_position"
                # SL/TP NOT checked on entry candle (position opens at close;
                # first candle management starts next iteration)
                continue

        # ── In position: manage SL snap, check exit ───────────────────────
        elif phase == "in_position":
            current_tp = tp_price(direction, bb)
            if min_tp_pct > 0:
                # Floor TP relative to THIS position's own entry price so it
                # can't compress back through a worthwhile distance as the
                # channel narrows — still tracks the band normally otherwise,
                # can still move further away, just can't collapse past this.
                if direction == "long":
                    current_tp = max(current_tp, entry_price * (1 + min_tp_pct))
                else:
                    current_tp = min(current_tp, entry_price * (1 - min_tp_pct))

            # SL snap: threshold fixed at entry (not recomputed each candle)
            if not snap_fired:
                if check_snap_triggered(direction, h, l, snap_at):
                    snap_fired = True
                    current_sl = trail_sl(direction, current_sl, bb[1])
            else:
                current_sl = trail_sl(direction, current_sl, bb[1])

            tp_hit = check_tp_hit(direction, h, l, current_tp)
            sl_hit = check_sl_hit(direction, h, l, current_sl)

            # Both on same candle → TP wins (matches original validated backtest)
            if tp_hit and sl_hit:
                sl_hit = False

            if tp_hit or sl_hit:
                exit_price   = current_tp if tp_hit else current_sl
                exit_trigger = (
                    ("upper_band" if direction == "long" else "lower_band")
                    if tp_hit else "sl"
                )

                raw_move = (
                    (exit_price - entry_price) / entry_price
                    if direction == "long"
                    else (entry_price - exit_price) / entry_price
                )
                # outcome from P&L sign (handles band-drift edge case)
                outcome  = "take_profit" if raw_move > 0 else "stop_loss"

                # fee_rate=None → mirror the live bot leg by leg (market entry and
                # market TP close are taker; the stop-limit SL fills as maker).
                # An explicit fee_rate overrides both legs, for dashboard what-ifs.
                if fee_rate is None:
                    _fee_entry = FEE_TAKER
                    _fee_exit  = FEE_MAKER if exit_trigger == "sl" else FEE_TAKER
                else:
                    _fee_entry = _fee_exit = fee_rate

                pnl_pct, pnl_usdt = calc_pnl(
                    direction, entry_price, exit_price,
                    leverage, capital, sizing_pct,
                    fee_entry_rate=_fee_entry,
                    fee_exit_rate=_fee_exit,
                )
                dur_h = round((ts - entry_ts_ms) / 3_600_000, 2)

                # TP at entry time for display
                entry_bb = bb_series[entry_idx]
                tp_at_entry = tp_price(direction, entry_bb) if entry_bb else None

                trades.append({
                    "candle_idx":     entry_idx,
                    "direction":      direction,
                    "entry_ts_ms":    entry_ts_ms,
                    "exit_ts_ms":     ts,
                    "entry":          entry_price,
                    "sl":             sl_price(direction, entry_price, sl_pct),
                    "tp":             tp_at_entry,
                    "exit_price":     exit_price,
                    "exit_trigger":   exit_trigger,
                    "outcome":        outcome,
                    "pnl_pct":        pnl_pct,
                    "pnl_usdt":       pnl_usdt,
                    "gross_pnl_pct":  round(raw_move * leverage * 100, 4),
                    "fee_pct":        round((_fee_entry + _fee_exit) * leverage * 100, 4),
                    "price_move_pct": round(raw_move * 100, 2),
                    "leverage":       leverage,
                    "duration_hours": dur_h,
                    "capital_before": round(capital, 4),
                })

                capital += pnl_usdt

                # Cooldown on any real loss (user-configurable in dashboard)
                if outcome == "stop_loss":
                    if direction == "long":
                        long_cd = cooldown_n
                    else:
                        short_cd = cooldown_n

                # Flip: immediate re-entry in opposite direction after TP
                flipped = False
                if tp_hit and outcome == "take_profit":
                    if flip_valid(
                        direction, exit_price, bb,
                        prev_ema, prev_close,
                        long_cd, short_cd,
                    ):
                        flip_dir = "short" if direction == "long" else "long"
                        if dir_filter == "both" or flip_dir == dir_filter:
                            direction    = flip_dir
                            entry_price  = exit_price
                            entry_ts_ms  = ts
                            entry_idx    = i
                            current_sl   = sl_price(direction, entry_price, sl_pct)
                            snap_at      = snap_trigger(direction, bb, snap_thresh)   # fixed at flip entry
                            snap_fired   = False
                            phase        = "in_position"
                            flipped      = True

                if not flipped:
                    phase     = "idle"
                    direction = None
                # Cooldown advances only when idle (handled at top of idle block).
                # In-position candles do NOT advance cooldown — matches original.

    # ── Open trade at end of data ─────────────────────────────────────────
    if phase == "in_position" and entry_price is not None:
        last      = candles[-1]
        raw_move  = (
            (last[4] - entry_price) / entry_price
            if direction == "long"
            else (entry_price - last[4]) / entry_price
        )
        _fee_entry_open = FEE_TAKER if fee_rate is None else fee_rate
        pnl_pct, pnl_usdt = calc_pnl(
            direction, entry_price, last[4],
            leverage, capital, sizing_pct,
            fee_entry_rate=_fee_entry_open,
            fee_exit_rate=0.0,   # exit not yet realised
        )
        entry_bb    = bb_series[entry_idx]
        tp_at_entry = tp_price(direction, entry_bb) if entry_bb else None
        trades.append({
            "candle_idx":     entry_idx,
            "direction":      direction,
            "entry_ts_ms":    entry_ts_ms,
            "exit_ts_ms":     None,
            "entry":          entry_price,
            "sl":             sl_price(direction, entry_price, sl_pct),
            "tp":             tp_at_entry,
            "exit_price":     None,
            "exit_trigger":   "open",
            "outcome":        "open",
            "pnl_pct":        pnl_pct,
            "pnl_usdt":       pnl_usdt,
            "gross_pnl_pct":  round(raw_move * leverage * 100, 4),
            "fee_pct":        round(_fee_entry_open * leverage * 100, 4),   # entry leg only
            "price_move_pct": round(raw_move * 100, 2),
            "leverage":       leverage,
            "duration_hours": round((last[0] - entry_ts_ms) / 3_600_000, 2),
            "capital_before": round(capital, 4),
        })

    return trades


def indicators(candles: list, params: dict) -> dict:
    """Return BB and EMA series for chart overlay."""
    period       = int(params.get("period",      BB_PERIOD))
    std_dev      = float(params.get("std_dev",    BB_STD))
    trend_period = int(params.get("trend_period", TREND_PERIOD))
    bb  = compute_bb(candles, period, std_dev)
    ema = compute_ema(candles, trend_period)
    return {
        "bb_upper": [b[0] if b else None for b in bb],
        "bb_mid":   [b[1] if b else None for b in bb],
        "bb_lower": [b[2] if b else None for b in bb],
        "ema":      ema,
    }
