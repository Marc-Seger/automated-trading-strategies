"""
gate1_audit.py
--------------
Gate 1 for BB Channel Rider: validate the strategy as it is actually deployed.

Why this replaces audit_bb_channel.py
-------------------------------------
The old audit was a standalone re-implementation of the strategy. It drifted from
the thing it was supposed to be auditing, and nobody noticed because it kept
passing against its own copy of the logic. By the time it was checked (2026-08-12)
it differed from the deployed bot on five counts, every one of them flattering:

    old audit                          frozen spec / live bot
    ---------                          ----------------------
    SL snap threshold 85%              95%
    trend filter EMA-200               EMA-150
    100% of capital per trade          25%  (SIZING_PCT)
    0.04% maker on both legs           taker on market entry AND market TP exit
    win = (outcome == "take_profit")   win = net P&L > 0

Its headline — 290 trades, 63.8% WR, +$10,348 — therefore described a strategy
that was never deployed. The trade count is the tell: 290 is the pre-grid-search
configuration; the frozen one produces a different number.

This script imports `strategies.bb_channel.simulate` directly, with no parameter
overrides, so it can only ever measure the frozen constants the live bot imports
from that same module. Parity is guaranteed by construction rather than by hand.

Usage:
    python3 research/gate1_audit.py path/to/candles.json

No default candle file. There used to be one (data/research/btc_15m_1y.json),
and it silently produced a different, worse result (438 trades, 53.4%,
-$709.48) than the fresh fetch this audit was actually re-derived against
(437 trades, 53.8%, -$683.90) — a caught-too-late trap for anyone re-running
this with no argument. data/research/ is gitignored (raw market data, not
source), so there is no committed file to default to; fetch a fresh year of
15m BTC/USDT:USDT swap candles yourself (this repo's own machine may need to
do it from a host that can reach MEXC — see automated-trading-strategies
CLAUDE.md for the fetch-from-VPS workaround) and pass the path explicitly.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.bb_channel import (  # noqa: E402
    simulate, FEE_MAKER, FEE_TAKER,
    BB_PERIOD, BB_STD, TREND_PERIOD, SL_PCT, SNAP_THRESH, COOLDOWN_N, SIZING_PCT,
)

START_CAPITAL = 1000.0
LEVERAGE = 10


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load_candles(path):
    with open(path) as f:
        raw = json.load(f)
    return raw if isinstance(raw, list) else raw.get("candles", raw)


def stats(trades, start_capital=START_CAPITAL):
    """
    Win rate is net-P&L based, deliberately.

    The old audit counted a win as outcome == "take_profit", i.e. the raw price
    move went the right way. At 10x, a round trip costs ~1.2% of margin in taker
    fees, so a TP exit on a small move is a net loss that still counted as a win.
    This is the same defect found and fixed in the dashboard on 2026-08-09.
    """
    closed = [t for t in trades if t.get("outcome") != "open"]
    if not closed:
        return None

    wins = [t for t in closed if t["pnl_usdt"] > 0]

    # Would-be win count under the old definition, to quantify the difference.
    tp_outcome_wins = [t for t in closed if t["outcome"] == "take_profit"]

    capital = start_capital
    peak = capital
    max_dd_pct = 0.0
    for t in closed:
        capital += t["pnl_usdt"]
        peak = max(peak, capital)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - capital) / peak * 100)

    return {
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": len(wins) / len(closed) * 100,
        "win_rate_old_defn": len(tp_outcome_wins) / len(closed) * 100,
        "net_pnl": capital - start_capital,
        "final_capital": capital,
        "return_pct": (capital - start_capital) / start_capital * 100,
        "max_dd_pct": max_dd_pct,
        "from": fmt_ts(closed[0]["entry_ts_ms"]),
        "to": fmt_ts(closed[-1]["exit_ts_ms"] or closed[-1]["entry_ts_ms"]),
    }


def run(candles, fee_rate, label):
    trades = simulate(
        candles,
        {},                      # no overrides — frozen constants only
        leverage=LEVERAGE,
        fee_rate=fee_rate,
        starting_capital=START_CAPITAL,
    )
    s = stats(trades)
    if s is None:
        print(f"{label}: no closed trades")
        return None
    print(f"\n{label}")
    print(f"  trades        {s['trades']}")
    print(f"  win rate      {s['win_rate']:.1f}%   "
          f"(old 'TP outcome' definition would say {s['win_rate_old_defn']:.1f}%)")
    print(f"  net P&L       ${s['net_pnl']:+,.2f}   on ${START_CAPITAL:,.0f} start "
          f"({s['return_pct']:+.1f}%)")
    print(f"  max drawdown  {s['max_dd_pct']:.2f}%")
    print(f"  window        {s['from']} -> {s['to']}")
    return s


def main():
    if len(sys.argv) < 2:
        sys.exit(
            "Usage: python3 research/gate1_audit.py path/to/candles.json\n"
            "No default — see the module docstring for why."
        )
    candles = load_candles(sys.argv[1])

    print("=" * 74)
    print("GATE 1 — BB Channel Rider, as deployed")
    print("=" * 74)
    print(f"candles       {len(candles):,}  ({fmt_ts(candles[0][0])} -> {fmt_ts(candles[-1][0])})")
    print(f"frozen params BB({BB_PERIOD}, {BB_STD})  EMA-{TREND_PERIOD}  "
          f"SL {SL_PCT:.1%}  snap {SNAP_THRESH:.0%}  cooldown {COOLDOWN_N}")
    print(f"sizing        {SIZING_PCT:.0%} of capital per trade, {LEVERAGE}x leverage, compounding")

    headline = run(candles, None, "LIVE-ACCURATE FEES  (taker entry + taker TP exit, maker stop-limit SL)")
    run(candles, FEE_MAKER, f"maker-only {FEE_MAKER:.2%} both legs  (what the old audit assumed)")
    run(candles, FEE_TAKER, f"taker {FEE_TAKER:.2%} both legs  (worst case, backstop on every exit)")
    run(candles, 0.0, "zero fees  (upper bound, not achievable)")

    print("\n" + "=" * 74)
    if headline:
        print(f"HEADLINE (live-accurate): {headline['trades']} trades, "
              f"{headline['win_rate']:.1f}% win rate, "
              f"${headline['net_pnl']:+,.2f} on ${START_CAPITAL:,.0f}, "
              f"{headline['max_dd_pct']:.1f}% max DD")
    print("=" * 74)


if __name__ == "__main__":
    main()
