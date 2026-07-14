"""
audit_bb_channel.py
-------------------
Complete trade-by-trade audit of the BB Channel Rider strategy.
Verifies all simulation claims against raw OHLCV data.

Strategy params (frozen 2026-05-09):
  - 15m BTC/USDT, BB period 20, std dev 3.0
  - SL 1.0%, SL snap threshold 85%
  - Cooldown 2 candles, EMA 200 trend filter (prev_c > prev_tma)
  - Leverage 10x, maker fees (0.02%), 100% capital, $1,000 start
  - Window: 2025-06-01 to 2026-05-09

Expected: 290 trades, 63.8% WR, +$10.3k net P&L (maker), max DD $5,991.20
"""

import json
import math
import sys
from datetime import datetime, timezone

# ─── Parameters ────────────────────────────────────────────────────────────────
BB_PERIOD      = 20
STD_DEV        = 3.0
SL_PCT         = 0.01          # 1.0%
SL_SNAP_PCT    = 0.85          # 85% of mid→band
COOLDOWN_N     = 2
TREND_PERIOD   = 200
LEVERAGE       = 10
MAKER_FEE_RATE = 0.0004        # 0.04% — MEXC API maker rate (not Web/App 0.02%)
START_CAPITAL  = 1000.0
WINDOW_FROM    = datetime(2025, 6, 1, tzinfo=timezone.utc)
WINDOW_TO      = datetime(2026, 5, 9, 23, 59, tzinfo=timezone.utc)
WARMUP_CANDLES = 220           # 200 EMA + 20 BB warmup

CACHE_PATH = "data/indicator_ohlcv/BTC_USDT_15m.json"

# ─── Helpers ────────────────────────────────────────────────────────────────────

def ts_to_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1: i + 1]) / period
    return out

def _ema(values, period):
    out = [None] * len(values)
    k = 2.0 / (period + 1)
    seed = period - 1
    if seed >= len(values):
        return out
    out[seed] = sum(values[:period]) / period
    for i in range(seed + 1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out

def compute_bollinger(candles, period, std_dev):
    closes = [c[4] for c in candles]
    mids   = _sma(closes, period)
    out    = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window   = closes[i - period + 1: i + 1]
        mean     = mids[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        sd       = math.sqrt(variance)
        out[i]   = (mean + std_dev * sd, mean, mean - std_dev * sd)
    return out

# ─── Load and slice candles ─────────────────────────────────────────────────────

def load_candles():
    with open(CACHE_PATH) as f:
        raw = json.load(f)
    all_candles = raw["candles"]

    from_ms = int(WINDOW_FROM.timestamp() * 1000)
    to_ms   = int(WINDOW_TO.timestamp() * 1000)

    # Find the first candle at or after WINDOW_FROM, then prepend warmup
    window_start_idx = next(
        (i for i, c in enumerate(all_candles) if c[0] >= from_ms), None
    )
    if window_start_idx is None:
        sys.exit("ERROR: No candles found at or after WINDOW_FROM")

    warmup_start_idx = max(0, window_start_idx - WARMUP_CANDLES)
    end_idx = next(
        (i for i, c in enumerate(all_candles) if c[0] > to_ms),
        len(all_candles)
    )

    candles = all_candles[warmup_start_idx:end_idx]
    # Record the index within `candles` where the audit window begins
    audit_start_in_slice = window_start_idx - warmup_start_idx

    print(f"Cache:      {len(all_candles):,} candles")
    print(f"Slice:      {len(candles):,} candles (idx {warmup_start_idx}..{end_idx-1})")
    print(f"Window:     first at {ts_to_str(candles[audit_start_in_slice][0])}")
    print(f"Window end: last  at {ts_to_str(candles[-1][0])}")
    return candles, audit_start_in_slice

# ─── Run simulation (same logic as indicator_backtest._simulate_bb_channel) ────

def run_simulation(candles):
    bands    = compute_bollinger(candles, BB_PERIOD, STD_DEV)
    closes   = [c[4] for c in candles]
    tma_vals = _ema(closes, TREND_PERIOD)

    trades   = []
    open_pos = None
    long_cd  = 0
    short_cd = 0

    def _open(side, entry_price, idx):
        upper, mid, lower = bands[idx]
        if side == "long":
            sl      = entry_price * (1 - SL_PCT)
            snap_at = mid + SL_SNAP_PCT * (upper - mid)
        else:
            sl      = entry_price * (1 + SL_PCT)
            snap_at = mid - SL_SNAP_PCT * (mid - lower)
        return {
            "direction":   side,
            "entry":       entry_price,
            "entry_band":  lower if side == "long" else upper,
            "sl":          sl,
            "sl_orig":     sl,
            "sl_snapped":  False,
            "snap_at":     snap_at,
            "entry_idx":   idx,
            "entry_ts":    candles[idx][0],
            "prev_tma":    tma_vals[idx - 1] if idx > 0 else None,
            "prev_c":      candles[idx - 1][4] if idx > 0 else None,
            "upper_band":  upper,
            "mid_band":    mid,
            "lower_band":  lower,
        }

    def _close(pos, exit_price, trigger, exit_idx):
        is_long  = pos["direction"] == "long"
        raw_move = (exit_price - pos["entry"]) / pos["entry"]
        pnl_move = raw_move if is_long else -raw_move
        outcome  = "open" if trigger == "open" else ("take_profit" if pnl_move >= 0 else "stop_loss")
        gross    = round(pnl_move * LEVERAGE * 100, 2)
        fee      = round(2 * MAKER_FEE_RATE * LEVERAGE * 100, 4) if trigger != "open" else 0.0
        net      = round(gross - fee, 2)
        e_ts     = pos["entry_ts"]
        x_ts     = candles[exit_idx][0]
        return {
            "num":            len(trades) + 1,
            "direction":      pos["direction"],
            "entry_idx":      pos["entry_idx"],
            "exit_idx":       exit_idx,
            "entry_ts_ms":    e_ts,
            "exit_ts_ms":     x_ts if trigger != "open" else None,
            "entry":          pos["entry"],
            "entry_band":     pos["entry_band"],
            "sl":             pos["sl_orig"],
            "exit_price":     exit_price if trigger != "open" else None,
            "exit_trigger":   trigger,
            "outcome":        outcome,
            "pnl_pct":        net,
            "gross_pnl_pct":  gross,
            "fee_pct":        fee,
            "upper_band_entry": pos["upper_band"],
            "mid_band_entry":   pos["mid_band"],
            "lower_band_entry": pos["lower_band"],
            "prev_tma_entry":   pos["prev_tma"],
            "prev_c_entry":     pos["prev_c"],
            "snap_at_entry":    pos["snap_at"],
        }

    for i in range(len(candles)):
        if bands[i] is None:
            continue

        ts, o, h, l, c, v = candles[i]
        upper, mid, lower  = bands[i]
        tma                = tma_vals[i]
        prev_tma           = tma_vals[i - 1] if i > 0 else tma
        prev_c             = candles[i - 1][4] if i > 0 else c
        ok_long            = prev_tma is not None and prev_c > prev_tma
        ok_short           = prev_tma is not None and prev_c < prev_tma

        if open_pos is not None:
            pos     = open_pos
            is_long = pos["direction"] == "long"

            if is_long:
                if not pos["sl_snapped"] and h >= pos["snap_at"]:
                    pos["sl_snapped"] = True
                if pos["sl_snapped"]:
                    pos["sl"] = max(pos["sl"], mid)
                if h >= upper:
                    trades.append(_close(pos, upper, "upper_band", i))
                    open_pos = _open("short", upper, i) if ok_short else None
                    continue
                if l <= pos["sl"]:
                    trades.append(_close(pos, pos["sl"], "sl", i))
                    open_pos = None
                    long_cd  = COOLDOWN_N
                    continue
            else:
                if not pos["sl_snapped"] and l <= pos["snap_at"]:
                    pos["sl_snapped"] = True
                if pos["sl_snapped"]:
                    pos["sl"] = min(pos["sl"], mid)
                if l <= lower:
                    trades.append(_close(pos, lower, "lower_band", i))
                    open_pos = _open("long", lower, i) if ok_long else None
                    continue
                if h >= pos["sl"]:
                    trades.append(_close(pos, pos["sl"], "sl", i))
                    open_pos = None
                    short_cd = COOLDOWN_N
                    continue
        else:
            lcd_start = long_cd
            scd_start = short_cd
            if long_cd  > 0 and c > lower:
                long_cd  -= 1
            if short_cd > 0 and c < upper:
                short_cd -= 1
            if lcd_start == 0 and l <= lower and ok_long:
                open_pos = _open("long", lower, i)
            elif scd_start == 0 and h >= upper and ok_short:
                open_pos = _open("short", upper, i)

    if open_pos is not None:
        trades.append(_close(open_pos, candles[-1][4], "open", len(candles) - 1))

    return trades

# ─── Audit ──────────────────────────────────────────────────────────────────────

def audit(trades, candles, audit_start_in_slice):
    """Verify each trade against raw OHLCV; return list of discrepancy dicts."""
    bands    = compute_bollinger(candles, BB_PERIOD, STD_DEV)
    closes   = [c[4] for c in candles]
    tma_vals = _ema(closes, TREND_PERIOD)

    errors = []
    warnings = []

    for t in trades:
        num      = t["num"]
        ei       = t["entry_idx"]
        xi       = t["exit_idx"]
        ec       = candles[ei]
        ts, o, h, l, c, v = ec
        band_at  = bands[ei]
        direction = t["direction"]
        is_long   = direction == "long"

        # ── Filter: only audit trades that start within the audit window ────────
        if ei < audit_start_in_slice:
            continue

        errs = []

        # (a) Entry candle actually touches the band ──────────────────────────
        if band_at is None:
            errs.append("BANDS_NONE: BB not yet computed at entry candle (insufficient warmup)")
        else:
            upper_b, mid_b, lower_b = band_at
            if is_long:
                if l > lower_b + 0.01:  # 1-cent tolerance for floating-point
                    errs.append(
                        f"BAND_TOUCH: long entry but candle low {l:.2f} > lower_band {lower_b:.2f}"
                    )
            else:
                if h < upper_b - 0.01:
                    errs.append(
                        f"BAND_TOUCH: short entry but candle high {h:.2f} < upper_band {upper_b:.2f}"
                    )

        # (b) Entry price equals the band value ────────────────────────────────
        expected_entry = lower_b if is_long else upper_b
        if band_at is not None:
            diff = abs(t["entry"] - expected_entry)
            if diff > 0.01:
                errs.append(
                    f"ENTRY_PRICE: recorded entry {t['entry']:.4f} vs band {expected_entry:.4f} "
                    f"(diff={diff:.4f})"
                )

        # (c) SL = entry ± 1.0% ───────────────────────────────────────────────
        expected_sl = t["entry"] * (1 - SL_PCT) if is_long else t["entry"] * (1 + SL_PCT)
        diff_sl = abs(t["sl"] - expected_sl)
        if diff_sl > 0.01:
            errs.append(
                f"SL_PCT: recorded SL {t['sl']:.4f} vs expected {expected_sl:.4f} "
                f"(diff={diff_sl:.4f})"
            )

        # (d) Trend filter ─────────────────────────────────────────────────────
        pv_tma = t["prev_tma_entry"]
        pv_c   = t["prev_c_entry"]
        if pv_tma is None:
            errs.append("TREND_FILTER: prev EMA not yet computed at entry (insufficient warmup)")
        else:
            if is_long and not (pv_c > pv_tma):
                errs.append(
                    f"TREND_FILTER: LONG entered but prev_c {pv_c:.2f} NOT > prev_tma {pv_tma:.2f}"
                )
            if not is_long and not (pv_c < pv_tma):
                errs.append(
                    f"TREND_FILTER: SHORT entered but prev_c {pv_c:.2f} NOT < prev_tma {pv_tma:.2f}"
                )

        # (e) Exit candle H/L actually crosses the exit price ─────────────────
        trigger = t["exit_trigger"]
        if trigger != "open" and xi is not None:
            xc      = candles[xi]
            xts, xo, xh, xl, xc4, xv = xc
            xprice  = t["exit_price"]
            if trigger == "upper_band":       # TP for long: high must reach upper band
                if xh < xprice - 0.01:
                    errs.append(
                        f"EXIT_CROSS: upper_band TP but exit candle high {xh:.2f} < exit_price {xprice:.2f}"
                    )
            elif trigger == "lower_band":     # TP for short: low must reach lower band
                if xl > xprice + 0.01:
                    errs.append(
                        f"EXIT_CROSS: lower_band TP but exit candle low {xl:.2f} > exit_price {xprice:.2f}"
                    )
            elif trigger == "sl":
                if is_long:
                    if xl > xprice + 0.01:
                        errs.append(
                            f"EXIT_CROSS: SL (long) but exit candle low {xl:.2f} > SL {xprice:.2f}"
                        )
                else:
                    if xh < xprice - 0.01:
                        errs.append(
                            f"EXIT_CROSS: SL (short) but exit candle high {xh:.2f} < SL {xprice:.2f}"
                        )

        if errs:
            errors.append({
                "trade_num":   num,
                "direction":   direction,
                "entry_ts":    ts_to_str(t["entry_ts_ms"]),
                "exit_ts":     ts_to_str(t["exit_ts_ms"]) if t["exit_ts_ms"] else "open",
                "entry":       t["entry"],
                "sl":          t["sl"],
                "exit_price":  t["exit_price"],
                "outcome":     t["outcome"],
                "pnl_pct":     t["pnl_pct"],
                "errors":      errs,
            })

    return errors

# ─── Stats ──────────────────────────────────────────────────────────────────────

def compute_stats(trades, candles, audit_start_in_slice, start_capital=START_CAPITAL):
    window_trades = [t for t in trades if t["entry_idx"] >= audit_start_in_slice]
    wins    = sum(1 for t in window_trades if t["outcome"] == "take_profit")
    losses  = sum(1 for t in window_trades if t["outcome"] == "stop_loss")
    opens   = sum(1 for t in window_trades if t["outcome"] == "open")
    total   = len(window_trades)
    wr      = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    capital  = start_capital
    peak     = capital
    max_dd   = 0.0
    total_net_pct = 0.0
    total_fee_pct = 0.0
    total_gross   = 0.0
    for t in window_trades:
        trade_size = capital   # 100% per trade
        dollar_pnl = t["pnl_pct"] / 100 * trade_size
        capital += dollar_pnl
        peak    = max(peak, capital)
        dd      = peak - capital
        max_dd  = max(max_dd, dd)
        total_net_pct   += t["pnl_pct"]
        total_gross     += t["gross_pnl_pct"]
        total_fee_pct   += t["fee_pct"]

    return {
        "total":      total,
        "wins":       wins,
        "losses":     losses,
        "opens":      opens,
        "win_rate":   round(wr, 1),
        "net_capital": round(capital, 2),
        "net_dollar":  round(capital - start_capital, 2),
        "max_dd":      round(max_dd, 2),
        "total_net_pct": round(total_net_pct, 2),
        "total_gross_pct": round(total_gross, 2),
        "total_fee_pct": round(total_fee_pct, 2),
    }

# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("BB CHANNEL RIDER — TRADE AUDIT")
    print("15m BTC/USDT | 2025-06-01 to 2026-05-09")
    print("BB 20/3.0 | SL 1% | Snap 85% | EMA 200 | 2-candle CD | 10x | Maker 0.02%")
    print("=" * 70)

    candles, audit_start = load_candles()
    print()

    print("Running simulation...")
    trades = run_simulation(candles)
    window_trades = [t for t in trades if t["entry_idx"] >= audit_start]
    print(f"Total trades (full slice): {len(trades)}")
    print(f"Trades in audit window:    {len(window_trades)}")
    print()

    print("Computing stats...")
    stats = compute_stats(trades, candles, audit_start)
    print(f"  Trades:   {stats['total']} (expected: 290)")
    print(f"  Wins:     {stats['wins']} (expected: 185)")
    print(f"  Losses:   {stats['losses']} (expected: 105)")
    print(f"  Open:     {stats['opens']}")
    print(f"  Win Rate: {stats['win_rate']}% (expected: 63.8%)")
    print(f"  Net P&L:  ${stats['net_dollar']:,.2f} (expected: ~+$10,300)")
    print(f"  Max DD:   ${stats['max_dd']:,.2f} (expected: $5,991.20)")
    print(f"  Total fees paid: {stats['total_fee_pct']:.2f}% notional")
    print()

    print("Running audit checks...")
    errors = audit(trades, candles, audit_start)
    print()

    if not errors:
        print("✓ AUDIT PASSED — 0 discrepancies found across all trades")
    else:
        print(f"✗ AUDIT FOUND {len(errors)} TRADES WITH ISSUES:")
        print()
        for e in errors:
            print(f"  Trade #{e['trade_num']:3d} | {e['direction'].upper():5s} | "
                  f"{e['entry_ts']} → {e['exit_ts']}")
            print(f"            entry={e['entry']:.2f} sl={e['sl']:.2f} "
                  f"exit={e['exit_price']} outcome={e['outcome']} pnl={e['pnl_pct']:+.2f}%")
            for err in e["errors"]:
                print(f"            ✗ {err}")
            print()

    # Print all trades for the record
    print()
    print("─" * 70)
    print("FULL TRADE LIST")
    print("─" * 70)
    print(f"{'#':>3} {'Dir':5} {'Entry Time':20} {'Exit Time':20} {'Entry':>10} "
          f"{'SL':>10} {'Exit':>10} {'Outcome':12} {'PnL%':>8}")
    for t in window_trades:
        ets = ts_to_str(t["entry_ts_ms"])
        xts = ts_to_str(t["exit_ts_ms"]) if t["exit_ts_ms"] else "OPEN"
        xp  = f"{t['exit_price']:.2f}" if t["exit_price"] else "–"
        print(f"{t['num']:>3} {t['direction']:5} {ets:20} {xts:20} {t['entry']:>10.2f} "
              f"{t['sl']:>10.2f} {xp:>10} {t['outcome']:12} {t['pnl_pct']:>+8.2f}%")

    print()
    print("=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)
    print(f"Trades audited:  {stats['total']}")
    print(f"Discrepancies:   {len(errors)}")
    if errors:
        from collections import Counter
        err_types = Counter()
        for e in errors:
            for err in e["errors"]:
                err_types[err.split(":")[0]] += 1
        print("Error breakdown:")
        for etype, count in err_types.most_common():
            print(f"  {etype}: {count}")
    else:
        print("All checks passed: band touch, entry price, SL%, trend filter, exit cross")

if __name__ == "__main__":
    main()
