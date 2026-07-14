"""
gate2_grid_search.py
--------------------
Parameter sensitivity grid search for BB Channel Rider.
Groups by (std_dev, trend_period) to precompute indicators once per pair.
"""

import json, math, time, itertools, csv
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
CACHE_PATH  = "data/indicator_ohlcv/BTC_USDT_15m.json"
WINDOW_FROM = datetime(2025, 6, 1, tzinfo=timezone.utc)
WINDOW_TO   = datetime(2026, 5, 9, 23, 59, tzinfo=timezone.utc)
WARMUP      = 320   # enough for EMA 300
BB_PERIOD   = 20    # fixed
LEVERAGE    = 10
FEE_RATE    = 0.0004   # MEXC API maker 0.04%
START_CAP   = 1000.0

# Current (frozen) params — used for ★ highlighting
CURRENT = dict(std_dev=3.0, sl_pct=0.01, sl_thresh=0.85, cooldown=2,
               trend_period=200, direction="both")

# ── Grid ─────────────────────────────────────────────────────────────────────
STD_DEVS    = [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0]
SL_PCTS     = [0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02]
SL_THRESHS  = [0.55, 0.65, 0.75, 0.85, 0.95]
COOLDOWNS   = [1, 2, 3, 4]
TREND_PRDS  = [100, 150, 200, 250, 300]
DIRECTIONS  = ["both", "long", "short"]

total_combos = (len(STD_DEVS) * len(SL_PCTS) * len(SL_THRESHS) *
                len(COOLDOWNS) * len(TREND_PRDS) * len(DIRECTIONS))

# ── Indicators ───────────────────────────────────────────────────────────────
def _sma(v, p):
    o = [None] * len(v)
    for i in range(p - 1, len(v)):
        o[i] = sum(v[i - p + 1:i + 1]) / p
    return o

def _ema(v, p):
    o = [None] * len(v); k = 2 / (p + 1); s = p - 1
    if s >= len(v): return o
    o[s] = sum(v[:p]) / p
    for i in range(s + 1, len(v)):
        o[i] = v[i] * k + o[i - 1] * (1 - k)
    return o

def compute_bands(candles, std_dev):
    cl = [c[4] for c in candles]
    mids = _sma(cl, BB_PERIOD)
    out = [None] * len(candles)
    for i in range(BB_PERIOD - 1, len(candles)):
        w = cl[i - BB_PERIOD + 1:i + 1]
        mn = mids[i]
        sd = math.sqrt(sum((x - mn) ** 2 for x in w) / BB_PERIOD)
        out[i] = (mn + std_dev * sd, mn, mn - std_dev * sd)
    return out

# ── Simulation ───────────────────────────────────────────────────────────────
def simulate(candles, bands, tma, sl_pct, sl_thresh, cooldown, direction, audit_start):
    FEE_PCT = round(2 * FEE_RATE * LEVERAGE * 100, 4)  # 0.8% at 0.04% rate

    trades = []
    pos = None; lc = 0; sc = 0
    do_long  = direction in ("both", "long")
    do_short = direction in ("both", "short")

    for i in range(len(candles)):
        bd = bands[i]
        if bd is None:
            continue
        ts, o, h, l, c, v = candles[i]
        upper, mid, lower = bd
        pt = tma[i - 1] if i > 0 else tma[i]
        pc = candles[i - 1][4] if i > 0 else c
        ok_l = do_long  and (pt is not None and pc > pt)
        ok_s = do_short and (pt is not None and pc < pt)

        if pos is not None:
            is_long = pos[0] == 'L'
            entry = pos[1]; sl = pos[2]; snap_at = pos[3]; snapped = pos[4]

            if is_long:
                if not snapped and h >= snap_at:
                    snapped = True
                if snapped:
                    sl = max(sl, mid)
                if h >= upper:
                    raw = (upper - entry) / entry
                    pnl = round(raw * LEVERAGE * 100 - FEE_PCT, 2)
                    trades.append((i, pnl, 'W' if pnl >= 0 else 'L'))
                    pos = ('S', upper, upper*(1+sl_pct),
                           mid - sl_thresh*(mid-lower), False) if ok_s else None
                    continue
                if l <= sl:
                    # Use actual SL level — may be above entry if snapped
                    raw = (sl - entry) / entry
                    pnl = round(raw * LEVERAGE * 100 - FEE_PCT, 2)
                    trades.append((i, pnl, 'W' if pnl >= 0 else 'L'))
                    pos = None; lc = cooldown
                    continue
                pos = ('L', entry, sl, snap_at, snapped)
            else:  # short
                if not snapped and l <= snap_at:
                    snapped = True
                if snapped:
                    sl = min(sl, mid)
                if l <= lower:
                    raw = (pos[1] - lower) / pos[1]
                    pnl = round(raw * LEVERAGE * 100 - FEE_PCT, 2)
                    trades.append((i, pnl, 'W' if pnl >= 0 else 'L'))
                    pos = ('L', lower, lower*(1-sl_pct),
                           mid + sl_thresh*(upper-mid), False) if ok_l else None
                    continue
                if h >= sl:
                    # Use actual SL level — may be below entry if snapped
                    raw = (entry - sl) / entry
                    pnl = round(raw * LEVERAGE * 100 - FEE_PCT, 2)
                    trades.append((i, pnl, 'W' if pnl >= 0 else 'L'))
                    pos = None; sc = cooldown
                    continue
                pos = ('S', entry, sl, snap_at, snapped)
        else:
            lcs = lc; scs = sc
            if lc > 0 and c > lower: lc -= 1
            if sc > 0 and c < upper: sc -= 1
            bd_now = bands[i]
            if bd_now is None:
                continue
            upper_n, mid_n, lower_n = bd_now
            if lcs == 0 and l <= lower_n and ok_l:
                snap_at = mid_n + sl_thresh * (upper_n - mid_n)
                pos = ('L', lower_n, lower_n*(1-sl_pct), snap_at, False)
            elif scs == 0 and h >= upper_n and ok_s:
                snap_at = mid_n - sl_thresh * (mid_n - lower_n)
                pos = ('S', upper_n, upper_n*(1+sl_pct), snap_at, False)

    # Close open position at last candle close
    if pos is not None:
        last_c = candles[-1][4]
        is_long = pos[0] == 'L'
        raw = (last_c - pos[1]) / pos[1]
        pnl = round((raw if is_long else -raw) * LEVERAGE * 100, 2)
        trades.append((len(candles) - 1, pnl, 'O'))

    # Stats (window trades only)
    w_trades = [t for t in trades if t[0] >= audit_start]
    wins   = sum(1 for t in w_trades if t[2] == 'W')
    losses = sum(1 for t in w_trades if t[2] == 'L')
    total  = len(w_trades)
    wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    # P&L: flat $1k per trade (for consistent cross-param comparison)
    cap = START_CAP; peak = cap; max_dd = 0.0
    for t in w_trades:
        if t[2] == 'O': continue
        cap += t[1] / 100 * START_CAP
        peak = max(peak, cap)
        max_dd = max(max_dd, peak - cap)
    net_pnl = cap - START_CAP
    calmar  = net_pnl / max_dd if max_dd > 0 else 0.0

    return dict(total=total, wins=wins, losses=losses,
                wr=round(wr, 1), net_pnl=round(net_pnl, 2),
                max_dd=round(max_dd, 2), calmar=round(calmar, 2))

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"Loading candles...")
    with open(CACHE_PATH) as f:
        all_c = json.load(f)['candles']

    from_ms = int(WINDOW_FROM.timestamp() * 1000)
    to_ms   = int(WINDOW_TO.timestamp() * 1000)
    wsi = next(i for i, c in enumerate(all_c) if c[0] >= from_ms)
    ws  = max(0, wsi - WARMUP)
    ei  = next((i for i, c in enumerate(all_c) if c[0] > to_ms), len(all_c))
    candles = all_c[ws:ei]
    audit_start = wsi - ws
    print(f"Candles: {len(candles):,}  audit_start idx: {audit_start}")

    # Precompute all unique bands and EMAs
    print(f"Precomputing {len(STD_DEVS)} BB band sets and {len(TREND_PRDS)} EMA sets...")
    closes = [c[4] for c in candles]
    all_bands = {sd: compute_bands(candles, sd) for sd in STD_DEVS}
    all_tma   = {tp: _ema(closes, tp) for tp in TREND_PRDS}
    print(f"Done. Running {total_combos:,} simulations...")

    results = []
    t0 = time.time()
    done = 0

    for sd, tp in itertools.product(STD_DEVS, TREND_PRDS):
        bands = all_bands[sd]
        tma   = all_tma[tp]
        for sl, thresh, cd, dirn in itertools.product(SL_PCTS, SL_THRESHS, COOLDOWNS, DIRECTIONS):
            s = simulate(candles, bands, tma, sl, thresh, cd, dirn, audit_start)
            results.append(dict(
                std_dev=sd, sl_pct=round(sl*100,3), sl_thresh=round(thresh*100),
                cooldown=cd, trend_period=tp, direction=dirn,
                **s
            ))
            done += 1
            if done % 1000 == 0:
                elapsed = time.time() - t0
                eta = elapsed / done * (total_combos - done)
                print(f"  {done:,}/{total_combos:,}  elapsed {elapsed:.0f}s  ETA {eta:.0f}s")

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s  ({total_combos/elapsed:.0f} runs/s)")

    # ── Save CSV ──────────────────────────────────────────────────────────────
    csv_path = "data/gate2_grid_results.csv"
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader(); w.writerows(results)
    print(f"Saved {len(results):,} rows → {csv_path}")

    # ── Analysis ──────────────────────────────────────────────────────────────
    profitable = [r for r in results if r['net_pnl'] > 0]
    print(f"\n{'='*65}")
    print(f"GRID SEARCH RESULTS — {total_combos:,} combinations")
    print(f"{'='*65}")
    print(f"Profitable combinations:  {len(profitable):,} / {total_combos:,} "
          f"({len(profitable)/total_combos*100:.1f}%)")

    # ── Current params result ─────────────────────────────────────────────────
    cur = next((r for r in results
                if r['std_dev'] == CURRENT['std_dev']
                and abs(r['sl_pct'] - CURRENT['sl_pct']*100) < 0.001
                and r['sl_thresh'] == round(CURRENT['sl_thresh']*100)
                and r['cooldown'] == CURRENT['cooldown']
                and r['trend_period'] == CURRENT['trend_period']
                and r['direction'] == CURRENT['direction']), None)
    if cur:
        print(f"\n★ Current params (std={CURRENT['std_dev']}, sl={CURRENT['sl_pct']*100}%, "
              f"thresh={CURRENT['sl_thresh']*100:.0f}%, cd={CURRENT['cooldown']}, "
              f"ema={CURRENT['trend_period']}, {CURRENT['direction']}):")
        print(f"  Trades={cur['total']}  WR={cur['wr']}%  "
              f"P&L=${cur['net_pnl']:,.0f}  MaxDD=${cur['max_dd']:,.0f}  Calmar={cur['calmar']:.2f}")
        rank = sorted(results, key=lambda r: -r['net_pnl']).index(cur) + 1
        print(f"  Rank by P&L: #{rank} of {total_combos:,}")

    # ── Top 15 by P&L ─────────────────────────────────────────────────────────
    top = sorted(results, key=lambda r: -r['net_pnl'])[:15]
    print(f"\nTop 15 by net P&L (flat $1k/trade):")
    print(f"{'std':>5} {'sl%':>5} {'thr':>5} {'cd':>4} {'ema':>5} {'dir':>6} "
          f"{'trades':>7} {'WR':>6} {'P&L':>8} {'MaxDD':>8} {'Calmar':>7}")
    for r in top:
        star = "★" if (r['std_dev']==CURRENT['std_dev']
                       and abs(r['sl_pct']-CURRENT['sl_pct']*100)<0.001
                       and r['sl_thresh']==round(CURRENT['sl_thresh']*100)
                       and r['cooldown']==CURRENT['cooldown']
                       and r['trend_period']==CURRENT['trend_period']
                       and r['direction']==CURRENT['direction']) else " "
        print(f"{star}{r['std_dev']:>4} {r['sl_pct']:>5.2f}% {r['sl_thresh']:>4}% "
              f"{r['cooldown']:>4} {r['trend_period']:>5} {r['direction']:>6} "
              f"{r['total']:>7} {r['wr']:>5.1f}% ${r['net_pnl']:>7,.0f} "
              f"${r['max_dd']:>7,.0f} {r['calmar']:>7.2f}")

    # ── 2D Heatmap: std_dev vs sl_pct (direction=both, others=current) ────────
    print(f"\nHeatmap: std_dev (rows) × sl_pct (cols)")
    print(f"Fixed: thresh={CURRENT['sl_thresh']*100:.0f}%, cd={CURRENT['cooldown']}, "
          f"ema={CURRENT['trend_period']}, direction=both")
    print(f"Values = Net P&L ($)")
    hdr = "std\\sl"
    print(f"\n{hdr:>8}", end="")
    sl_labels = [f"{s*100:.2f}%" for s in SL_PCTS]
    for lbl in sl_labels:
        print(f"{lbl:>9}", end="")
    print()
    for sd in STD_DEVS:
        star = "★" if sd == CURRENT['std_dev'] else " "
        print(f"{star}{sd:>6.2f} ", end="")
        for sl in SL_PCTS:
            match = next((r for r in results
                          if r['std_dev']==sd
                          and abs(r['sl_pct']-sl*100)<0.001
                          and r['sl_thresh']==round(CURRENT['sl_thresh']*100)
                          and r['cooldown']==CURRENT['cooldown']
                          and r['trend_period']==CURRENT['trend_period']
                          and r['direction']=="both"), None)
            val = match['net_pnl'] if match else 0
            mark = "★" if (sd==CURRENT['std_dev'] and abs(sl-CURRENT['sl_pct'])<0.001) else " "
            print(f"{val:>+8,.0f}{mark}", end="")
        print()

    # ── Robustness by parameter ───────────────────────────────────────────────
    print(f"\nRobustness: avg P&L by parameter value (all other params free)")

    def avg_by(key, vals):
        print(f"\n  {key}:")
        for v in vals:
            subset = [r for r in results if r[key] == v]
            avg = sum(r['net_pnl'] for r in subset) / len(subset)
            pct_pos = sum(1 for r in subset if r['net_pnl'] > 0) / len(subset) * 100
            cur_mark = "★" if v == CURRENT.get(key.replace('sl_pct','sl_pct').replace('sl_thresh','sl_thresh')) else ""
            print(f"    {key}={v}: avg P&L=${avg:>+7,.0f}  profitable={pct_pos:.0f}%  {cur_mark}")

    avg_by('std_dev',     STD_DEVS)
    avg_by('sl_pct',      [round(s*100,3) for s in SL_PCTS])
    avg_by('sl_thresh',   [round(t*100) for t in SL_THRESHS])
    avg_by('cooldown',    COOLDOWNS)
    avg_by('trend_period',TREND_PRDS)
    avg_by('direction',   DIRECTIONS)

if __name__ == "__main__":
    main()
