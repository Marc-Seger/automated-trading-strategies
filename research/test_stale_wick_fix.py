"""
research/test_stale_wick_fix.py
-------------------------------
Regression test for the 2026-08-24 fix: the live bot could be stopped out by a
price that printed BEFORE the stop existed.

_check_position() tests stops against the forming candle's cumulative H/L —
the extremes since the candle opened. A stop set mid-candle (at entry, or when
the SL snaps to mid) therefore inherited a range that partly predates it, and
fired on a move that had already happened.

The fix stamps the H/L already printed when the levels were set, and falls back
to the live price for the part of the wick that is stale. Once the candle rolls
over the stamp lapses and normal wick checks resume.

Cases below are taken from real trades in data/trades/bb_bot_trades_BTC.json.
Run:  .venv/bin/python research/test_stale_wick_fix.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bots.bb_bot import BBBot
from strategies.bb_channel import check_sl_hit


def _bot(forming_ts, h_at, l_at, current_ts=None):
    """A BBBot with only the attributes _live_basis reads."""
    b = object.__new__(BBBot)
    b.state = {
        "pos_lvl_candle_ts": forming_ts,
        "pos_lvl_h_at":      h_at,
        "pos_lvl_l_at":      l_at,
    }
    b._forming_ts = forming_ts if current_ts is None else current_ts
    return b


def case(name, direction, sl, forming_ts, h_at, l_at, h_now, l_now, price,
         expect, candle_rolled=False):
    b = _bot(forming_ts, h_at, l_at,
             current_ts=forming_ts + 900_000 if candle_rolled else None)
    eff_h, eff_l = b._live_basis(h_now, l_now, price)
    fired = check_sl_hit(direction, eff_h, eff_l, sl)
    ok = fired == expect
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    print("        wick %.2f/%.2f -> effective %.2f/%.2f (price %.2f)"
          % (h_now, l_now, eff_h, eff_l, price))
    print("        stop %.2f fires=%s expected=%s\n" % (sl, fired, expect))
    return ok


def main():
    results = []

    print("STALE WICK MUST NOT TRIGGER A STOP\n" + "=" * 70)
    # #14, 08-19 08:38. Snap moved SL to 64285.39; the candle's low of 64261.10
    # was printed at 08:30, ~8 min earlier. Price was ~64430 at the time.
    results.append(case("#14 phantom snap-stop", "long", 64285.39,
                        1787128200000, 64431.00, 64261.10,
                        64431.00, 64261.10, 64430.00, expect=False))

    # #17, 08-20 15:04. Same phantom; the candle ran on past the real TP.
    results.append(case("#17 phantom snap-stop", "long", 71835.27,
                        1787238000000, 72368.00, 71760.10,
                        72368.00, 71760.10, 72367.00, expect=False))

    # #18, 08-22 05:10. Entered on a candle that had already fallen 2,000 pts;
    # the stop was below the candle's low at the moment of entry.
    results.append(case("#18 stop already breached at entry", "long", 76543.50,
                        1787374800000, 78591.30, 76506.90,
                        78591.30, 76506.90, 77360.90, expect=False))

    print("GENUINE STOPS MUST STILL FIRE\n" + "=" * 70)
    # #21, real stop-out three hours after entry — candle long since rolled.
    results.append(case("#21 real stop-out, later candle", "short", 77677.11,
                        1787480700000, 76920.00, 76640.40,
                        77771.60, 77325.40, 77419.20,
                        expect=True, candle_rolled=True))

    # A new low is made AFTER the stop was set: the wick advanced, so it counts.
    results.append(case("new wick low after stop set", "long", 64285.39,
                        1787128200000, 64431.00, 64261.10,
                        64431.00, 64200.00, 64210.00, expect=True))

    # Price drifts through the stop without making a new candle low: the live
    # price catches it even though the wick is unchanged.
    results.append(case("live price crosses, no new wick", "long", 64285.39,
                        1787128200000, 64431.00, 64261.10,
                        64431.00, 64261.10, 64270.00, expect=True))

    # Short side: a genuine new high through the stop in the same candle.
    results.append(case("short, new wick high after stop set", "short", 65082.88,
                        1787146200000, 65000.00, 64779.60,
                        65190.00, 64779.60, 65100.00, expect=True))

    print("=" * 70)
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
