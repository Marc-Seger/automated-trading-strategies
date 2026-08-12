"""
dashboard/server.py
-------------------
Flask-based backtest dashboard for systematic BTC trading strategies.

Run:
    python3 dashboard/server.py

Then open http://localhost:5050 in your browser.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
import time
from datetime import datetime, timezone, timedelta

import ccxt
import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from backtest.indicator_backtest import simulate_indicator_trades, normalise_mexc_symbol

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str = "config.yaml") -> dict:
    load_dotenv()
    full_path = os.path.join(BASE_DIR, path)
    with open(full_path, "r") as f:
        cfg = yaml.safe_load(f)
    mx = cfg.setdefault("mexc", {})
    mx["api_key"]    = os.environ.get("MEXC_API_KEY",    mx.get("api_key", ""))
    mx["api_secret"] = os.environ.get("MEXC_API_SECRET", mx.get("api_secret", ""))
    return cfg


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=DASHBOARD_DIR)

# Global state (populated on startup)
CONFIG:      dict = {}
INDICATOR_OHLCV_CACHE: dict = {}  # symbol -> timeframe -> list of candles
_MEXC_EXCHANGE = None  # shared ccxt.mexc() client, see _get_mexc_exchange()


def _get_mexc_exchange():
    """
    Shared, long-lived ccxt.mexc() client, reused across every request.

    Previously each gap-fill created a brand new ccxt.mexc() instance, which
    meant every call re-triggered ccxt's lazy load_markets() — that can hit
    MEXC's rate-limited spot /capital/config/getall endpoint instead of the
    swap one (the same gotcha bots/bb_bot.py already works around). Under the
    Live Bot chart's 15s auto-refresh that meant a fresh client — and a fresh
    failing/slow lazy-load — on every single poll, which piled up unclosed
    connections and took the VPS down on 2026-08-08. Pre-loading swap markets
    once, like bb_bot.py already does, avoids the lazy-load path entirely.
    """
    global _MEXC_EXCHANGE
    if _MEXC_EXCHANGE is None:
        mx_cfg = CONFIG.get("mexc", {})
        exchange = ccxt.mexc({
            "apiKey": mx_cfg.get("api_key", ""),
            "secret": mx_cfg.get("api_secret", ""),
            "options": {"defaultType": "swap"},
        })
        exchange.fetch_markets({"type": "swap"})
        _MEXC_EXCHANGE = exchange
    return _MEXC_EXCHANGE


# ---------------------------------------------------------------------------
# Indicator OHLCV gap-filling cache
# ---------------------------------------------------------------------------

_IND_CACHE_DIR = os.path.join(BASE_DIR, "data", "ohlcv", "indicator")
os.makedirs(_IND_CACHE_DIR, exist_ok=True)

# Milliseconds per candle per timeframe
_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000, "1d": 86_400_000,
    "1w": 604_800_000,
}
# Default lookback (days) when no date range specified — stays within MEXC's per-timeframe cap
_TF_DEFAULT_LOOKBACK = {
    "1m": 7, "5m": 120, "15m": 355, "30m": 355,
    "1h": 400, "4h": 1600, "8h": 3200, "1d": 3650, "1w": 3650,
}
_WARMUP_CANDLES = 300   # extra candles fetched before display window for indicator warmup


def _ind_cache_path(symbol: str, timeframe: str) -> str:
    safe = symbol.replace("/", "_")
    return os.path.join(_IND_CACHE_DIR, f"{safe}_{timeframe}.json")


def _load_disk_ind_cache(symbol: str, timeframe: str) -> list:
    path = _ind_cache_path(symbol, timeframe)
    if os.path.exists(path):
        try:
            return json.loads(open(path).read()).get("candles", [])
        except Exception:
            return []
    return []


def _save_disk_ind_cache(symbol: str, timeframe: str, candles: list) -> None:
    path = _ind_cache_path(symbol, timeframe)
    with open(path, "w") as f:
        json.dump({"candles": candles}, f)


# --- Funding rates -----------------------------------------------------------
# Perpetual swaps charge funding at fixed settlement times (every 8h on MEXC).
# bb_bot.py does not model this at all: its P&L covers the price move and the
# taker/maker fees only. Funding is small here (measured 2026-08-10: ~0.005% per
# 8h on average, ~$0.37 over the longest 22h trade against $34 of P&L) but it is
# a real, systematic cost — it scales with notional x holding time rather than
# with profit, and it is always a debit for a long while the rate is positive,
# which is the normal state in a rising market.
#
# Computed here rather than stored by the bot, matching how fee_usdt /
# gross_pnl_usdt are reconstructed below: the trade log keeps only what actually
# happened, and derived figures are recomputed on read so they apply
# retroactively to every trade already on record.
_FUNDING_CACHE  = {}                 # symbol -> {"fetched_ms": int, "rates": [...]}
_FUNDING_TTL_MS = 30 * 60_000        # settles every 8h, so half-hourly is ample


def _funding_cache_path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace(":", "_")
    return os.path.join(_IND_CACHE_DIR, f"funding_{safe}.json")


def _get_funding_rates(symbol: str) -> list:
    """
    Funding settlements as [{"ts": ms, "rate": float}], oldest first.

    Cached hard on purpose. /api/bb_bot_status is polled every 15s and Flask
    here is single-threaded, so an uncached exchange call on this path would
    recreate the conditions that took the VPS down on 2026-08-08. At most one
    refresh per _FUNDING_TTL_MS, and any failure falls back to the last good
    data (memory, then disk, then nothing) — funding detail is worth degrading,
    never worth failing the whole dashboard for.
    """
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    entry  = _FUNDING_CACHE.get(symbol)

    if entry is None:                                  # cold process: try disk
        try:
            path = _funding_cache_path(symbol)
            if os.path.exists(path):
                with open(path) as f:
                    entry = json.load(f)
                _FUNDING_CACHE[symbol] = entry
        except Exception:
            entry = None

    if entry and now_ms - entry.get("fetched_ms", 0) < _FUNDING_TTL_MS:
        return entry.get("rates", [])

    try:
        ex  = _get_mexc_exchange()
        sym = symbol if ":" in symbol else symbol + ":USDT"
        raw = ex.fetch_funding_rate_history(sym, limit=200)
        rates = sorted(
            ({"ts": int(r["timestamp"]), "rate": float(r["fundingRate"])}
             for r in raw
             if r.get("timestamp") is not None and r.get("fundingRate") is not None),
            key=lambda r: r["ts"],
        )
        if rates:
            entry = {"fetched_ms": now_ms, "rates": rates}
            _FUNDING_CACHE[symbol] = entry
            try:
                with open(_funding_cache_path(symbol), "w") as f:
                    json.dump(entry, f)
            except Exception:
                pass
            return rates
    except Exception as e:
        logger.warning(f"Funding rate fetch failed for {symbol}: {e}")

    return (entry or {}).get("rates", [])


def _trade_funding_usdt(trade: dict, rates: list, price_at, contract_lot: float):
    """
    Funding COST for one trade in USDT — positive means paid out, negative
    means received — plus the number of settlements it was charged over.

    A position is charged if it is open when a settlement lands, so the window
    is (entry_ts, exit_ts]: a trade opened exactly at a settlement has not held
    through it, one closed exactly at a settlement has. When the rate is
    positive longs pay shorts, hence the sign flip for shorts. Notional is
    marked at the price when each settlement actually occurred rather than at
    entry, since that is what the exchange charges against; entry price is the
    fallback when no candle covers that moment.

    Returns (None, 0) when the rate history does not cover the trade, so the UI
    can distinguish "no funding" from "not known".
    """
    entry_ts, exit_ts = trade.get("entry_ts"), trade.get("exit_ts")
    entry, qty = trade.get("entry"), trade.get("quantity")
    if None in (entry_ts, exit_ts, entry, qty) or not rates:
        return None, 0
    if entry_ts < rates[0]["ts"] or exit_ts > rates[-1]["ts"] + 8 * 3600_000:
        return None, 0                                  # outside known history

    is_long = trade.get("direction") == "long"
    total, n = 0.0, 0
    for r in rates:
        if not (entry_ts < r["ts"] <= exit_ts):
            continue
        px  = price_at(r["ts"]) or entry
        pay = qty * px * contract_lot * r["rate"]        # long pays when rate > 0
        total += pay if is_long else -pay
        n += 1
    return total, n


def _merge_ind_candles(existing: list, new: list) -> list:
    by_ts = {c[0]: c for c in existing}
    for c in new:
        by_ts[c[0]] = c
    return sorted(by_ts.values(), key=lambda x: x[0])


def _compute_gaps(cached: list, from_ms: int, to_ms: int, tf_ms: int = 3_600_000) -> list:
    """Return [(gap_from, gap_to)] ranges not covered by cache for [from_ms, to_ms].

    Also detects internal gaps larger than 2× the candle interval.
    """
    if not cached:
        return [(from_ms, to_ms)]
    first_ts, last_ts = cached[0][0], cached[-1][0]
    gaps = []
    if from_ms < first_ts:
        gaps.append((from_ms, first_ts - 1))
    # Internal gaps: consecutive candles more than 2 candle-widths apart
    threshold = tf_ms * 2
    for i in range(1, len(cached)):
        if cached[i][0] - cached[i - 1][0] > threshold:
            gap_start = cached[i - 1][0] + tf_ms
            gap_end   = cached[i][0] - 1
            if gap_end >= from_ms and gap_start <= to_ms:
                gaps.append((max(gap_start, from_ms), min(gap_end, to_ms)))
    if to_ms > last_ts:
        gaps.append((last_ts + 1, to_ms))
    return gaps


def _fetch_ohlcv_range(exchange, symbol: str, timeframe: str, from_ms: int, to_ms: int) -> list:
    """Paginate MEXC to collect all candles in [from_ms, to_ms].

    Sleeps 200ms between batches to stay under the 20 req/2s rate limit.
    Retries once on empty batch in case of transient throttle.
    """
    candles, since, page = [], from_ms, 0
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
        if not batch:
            # One retry after a short pause in case of transient rate-limit empty
            time.sleep(0.5)
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
            if not batch:
                break
        in_range = [c for c in batch if c[0] <= to_ms]
        candles.extend(in_range)
        page += 1
        if page % 10 == 0:
            dt = datetime.fromtimestamp(batch[-1][0] / 1000, tz=timezone.utc).date()
            logger.info(f"  ... {symbol} {timeframe} page {page}, up to {dt} ({len(candles)} candles so far)")
        since = batch[-1][0] + 1
        if len(batch) < 500 or batch[-1][0] >= to_ms:
            break
        time.sleep(0.2)  # 200ms between batches → max 5 req/s, well under 20/2s limit
    return candles


def ensure_indicator_candles(
    symbol: str, timeframe: str, from_ms, to_ms, force_tail_ms: int = 0
) -> list:
    """
    Return all cached candles for symbol+timeframe that cover [from_ms, to_ms]
    (plus warmup buffer). Fetches only the gaps not already on disk, merges,
    and writes back to disk so coverage grows incrementally across sessions.

    force_tail_ms: when >0, always re-fetch the trailing window of this width
    ending at to_ms, even if the cache already has it. The cache treats any
    timestamp it already holds as permanently settled, but a candle fetched
    while still forming stays frozen at that partial snapshot forever unless
    something forces a re-check — this is how a live-tracking caller (the
    Live Bot chart) keeps its current and most-recently-closed candle honest.
    """
    # Extend fetch start by warmup buffer so indicators are seeded before display window
    tf_ms      = _TF_MS.get(timeframe, 3_600_000)
    warmup_ms  = _WARMUP_CANDLES * tf_ms
    fetch_from = from_ms - warmup_ms

    # Load in-memory cache, falling back to disk
    cached = INDICATOR_OHLCV_CACHE.get(symbol, {}).get(timeframe)
    if cached is None:
        cached = _load_disk_ind_cache(symbol, timeframe)
        INDICATOR_OHLCV_CACHE.setdefault(symbol, {})[timeframe] = cached

    gaps = _compute_gaps(cached, fetch_from, to_ms, tf_ms=tf_ms)
    if force_tail_ms > 0:
        gaps.append((max(fetch_from, to_ms - force_tail_ms), to_ms))
    if gaps:
        exchange = _get_mexc_exchange()
        # MEXC futures symbol format: "BTC/USDT" → "BTC/USDT:USDT"
        fetch_symbol = symbol if ":" in symbol else symbol + ":USDT"
        new_candles = []
        for g_from, g_to in gaps:
            d0 = datetime.fromtimestamp(g_from / 1000, tz=timezone.utc).date()
            d1 = datetime.fromtimestamp(g_to   / 1000, tz=timezone.utc).date()
            logger.info(f"Fetching gap {fetch_symbol} {timeframe}: {d0} → {d1}")
            try:
                new_candles.extend(_fetch_ohlcv_range(exchange, fetch_symbol, timeframe, g_from, g_to))
            except Exception as e:
                logger.error(f"Gap fetch failed {fetch_symbol} {timeframe}: {e}")
                raise

        if new_candles:
            # NEVER persist a candle that hasn't closed yet.
            #
            # A candle fetched mid-formation holds only the trading so far, so
            # its high/low/close are provisional. The cache treats any timestamp
            # it already holds as settled and never rechecks it, so a provisional
            # candle written once stays wrong permanently. force_tail_ms above
            # re-fetches the trailing couple of candles, but that is a sliding
            # window: as soon as polling stops (browser closed, session over),
            # whatever partial was last written falls outside it and is stranded.
            # A 7-day audit on 2026-08-10 found 2 genuinely corrupted candles
            # this way — one stuck at O=H=L=C, another with its high understated
            # by $231 — which silently misdrew the chart and fed the backtest.
            #
            # Excluding unclosed candles from the *write* removes the cause
            # rather than chasing it: a partial can never enter the cache. It is
            # still returned to the caller, so the live forming candle keeps
            # showing on the chart; it just gets re-fetched each time until it
            # closes, at which point it is stored complete.
            now_ms  = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            closed  = [c for c in new_candles if c[0] + tf_ms <= now_ms]
            forming = [c for c in new_candles if c[0] + tf_ms >  now_ms]

            if closed:
                # Filter the MERGED result, not just the incoming candles.
                # Excluding only `new_candles` is not enough: a provisional
                # candle already sitting in the cache (written by an older
                # build, or by this process before it closed) rides along
                # inside `merged` and gets written straight back out. Filtering
                # here means the persisted file can never hold an unclosed
                # candle whatever its origin, and legacy ones are evicted the
                # first time this path runs.
                merged = [c for c in _merge_ind_candles(cached, closed)
                          if c[0] + tf_ms <= now_ms]
                INDICATOR_OHLCV_CACHE.setdefault(symbol, {})[timeframe] = merged
                _save_disk_ind_cache(symbol, timeframe, merged)
                logger.info(
                    f"Gap fill complete: {symbol} {timeframe} +{len(closed)} candles "
                    f"(total: {len(merged)}"
                    + (f", {len(forming)} still forming, not cached)" if forming else ")")
                )
                cached = merged
            if forming:
                # Returned but not stored — _merge_ind_candles builds a new list,
                # so the cached/in-memory copy above stays closed-candles-only.
                cached = _merge_ind_candles(cached, forming)

    return cached


@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.route("/api/indicator_simulate", methods=["POST"])
def api_indicator_simulate():
    from backtest.indicator_backtest import compute_ma, compute_bollinger, compute_rsi
    params = request.get_json(force=True) or {}

    symbol    = normalise_mexc_symbol(params.get("symbol", "BTC/USDT"))
    timeframe = params.get("timeframe", "1h")
    from_date = params.get("from_date", "")   # "YYYY-MM-DD" or ""
    to_date   = params.get("to_date",   "")

    sl_pct    = float(params.get("sl_pct", 5.0)) / 100.0
    tp_pct    = float(params.get("tp_pct", 10.0)) / 100.0
    leverage  = int(params.get("leverage", 1))
    _pt_raw   = params.get("per_trade")
    per_trade = float(_pt_raw) if _pt_raw is not None else 100.0
    starting_capital = float(params.get("starting_capital", 1000.0))

    _fee_rates = {"none": 0.0, "spot": 0.001, "api_maker": 0.0004, "api_taker": 0.0006}
    fee_rate   = _fee_rates.get(params.get("fee_mode", "none"), 0.0)

    strategy = {
        "strategy_type":    params.get("strategy_type", "ma_cross"),
        "direction":        params.get("direction", "both"),
        "fast":             params.get("fast", 20),
        "slow":             params.get("slow", 50),
        "period":           params.get("period", 50),
        "ma_type":          params.get("ma_type", "ema"),
        "std_dev":          params.get("std_dev", 3.0),   # frozen BB spec uses 3.0
        "oversold":         params.get("oversold", 30),
        "overbought":       params.get("overbought", 70),
        # BB Channel params
        "sl_threshold_pct": float(params.get("sl_threshold_pct", 0.95)),  # locked spec
        "cooldown_n":       int(params.get("cooldown_n", 2)),
        "trend_filter":     bool(params.get("trend_filter", True)),       # locked spec
        "trend_period":     int(params.get("trend_period", 150)),         # locked spec
    }

    # --- Parse date window (always set; drives both fetch and display) ---
    now_ms  = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    from_ms = None
    to_ms   = None
    if from_date:
        try:
            from_ms = int(datetime.fromisoformat(from_date + "T00:00:00+00:00").timestamp() * 1000)
        except ValueError:
            pass
    if to_date:
        try:
            to_ms = int(datetime.fromisoformat(to_date + "T23:59:59+00:00").timestamp() * 1000)
        except ValueError:
            pass
    # Default window when no dates given — same as the per-timeframe lookback so
    # the display stays on recent data even if historical cache is larger.
    if from_ms is None:
        days = _TF_DEFAULT_LOOKBACK.get(timeframe, 400)
        from_ms = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    if to_ms is None:
        to_ms = now_ms

    # --- OHLCV: gap-filling cache (fetches only missing segments, persists to disk) ---
    try:
        candles = ensure_indicator_candles(symbol, timeframe, from_ms, to_ms)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch {symbol} at {timeframe}: {e}"}), 500

    if not candles:
        return jsonify({"error": f"No OHLCV data for {symbol} at {timeframe}"}), 404

    # --- Simulate on FULL candle set (indicator warmup needs history) ---
    raw_trades = simulate_indicator_trades(candles, strategy, sl_pct, tp_pct, leverage, fee_rate,
                                           starting_capital=starting_capital)

    # Filter trades to date window
    if from_ms is not None or to_ms is not None:
        raw_trades = [t for t in raw_trades
                      if (from_ms is None or t["entry_ts_ms"] >= from_ms) and
                         (to_ms   is None or t["entry_ts_ms"] <= to_ms)]

    # --- Compute indicator values on full candles, then slice to display window ---
    stype   = strategy["strategy_type"]
    ma_type = strategy["ma_type"]
    fast    = int(strategy["fast"])
    slow    = int(strategy["slow"])
    period  = int(strategy["period"])
    std_dev = float(strategy["std_dev"])
    oversold    = float(strategy["oversold"])
    overbought  = float(strategy["overbought"])

    fast_ma_full = slow_ma_full = bb_full = rsi_full = trend_ma_full = None
    if stype == "ma_cross":
        fast_ma_full = compute_ma(candles, fast, ma_type)
        slow_ma_full = compute_ma(candles, slow, ma_type)
    elif stype == "price_vs_ma":
        fast_ma_full = compute_ma(candles, period, ma_type)
    elif stype in ("bollinger", "bb_channel"):
        bb_full = compute_bollinger(candles, period, std_dev)
        if stype == "bb_channel" and strategy.get("trend_filter"):
            trend_ma_full = compute_ma(candles, strategy["trend_period"], "ema")
    elif stype == "rsi":
        rsi_full = compute_rsi(candles, period)

    # Build chart_data (candles in display window with indicator values)
    chart_data = []
    for i, c in enumerate(candles):
        ts = c[0]
        if from_ms is not None and ts < from_ms:
            continue
        if to_ms is not None and ts > to_ms:
            continue
        entry = {"t": ts, "o": c[1], "h": c[2], "l": c[3], "c": c[4]}
        if fast_ma_full is not None:
            entry["fast_ma"] = fast_ma_full[i]
        if slow_ma_full is not None:
            entry["slow_ma"] = slow_ma_full[i]
        if bb_full is not None:
            entry["bb_upper"] = bb_full[i][0] if bb_full[i] else None
            entry["bb_mid"]   = bb_full[i][1] if bb_full[i] else None
            entry["bb_lower"] = bb_full[i][2] if bb_full[i] else None
        if rsi_full is not None:
            entry["rsi"] = rsi_full[i]
        if trend_ma_full is not None:
            entry["trend_ma"] = trend_ma_full[i]
        chart_data.append(entry)

    # --- Capital tracking setup ---
    per_trade_pct_raw = params.get("per_trade_pct")
    per_trade_pct_val = float(per_trade_pct_raw) / 100.0 if per_trade_pct_raw is not None else None
    track_capital     = bool(params.get("track_capital", False))

    shadow_capital = starting_capital

    # --- Build trade dicts with per-trade capital tracking ---
    trades = []
    for t in raw_trades:
        capital_at_time = round(shadow_capital, 2)

        # Per-trade size: flat $ or % of current running capital
        if per_trade_pct_val is not None:
            trade_size = round(shadow_capital * per_trade_pct_val, 4)
        else:
            trade_size = per_trade
        trade_size = max(trade_size, 0.01)

        pnl_dollar = round((t["pnl_pct"] / 100.0) * trade_size, 4)
        fee_dollar = round((t["fee_pct"] / 100.0) * trade_size, 4)
        entry_iso  = _ms_to_iso(t["entry_ts_ms"])
        exit_iso   = _ms_to_iso(t["exit_ts_ms"]) if t["exit_ts_ms"] else None

        # Update shadow capital when trade resolves (sequential — no overlapping)
        if t["outcome"] != "open":
            shadow_capital = max(0.0, shadow_capital + pnl_dollar)

        trades.append({
            "time":           entry_iso,
            "exit_time":      exit_iso,
            "entry_ts_ms":    t["entry_ts_ms"],
            "exit_ts_ms":     t["exit_ts_ms"],
            "direction":      t["direction"],
            "entry":          t["entry"],
            "sl":             t["sl"],
            "tp":             t["tp"],
            "exit_price":     t["exit_price"],
            "outcome":        t["outcome"],
            "pnl_pct":        t["pnl_pct"],
            "pnl_dollar":     pnl_dollar,
            "gross_pnl_pct":  t["gross_pnl_pct"],
            "fee_dollar":     fee_dollar,
            "price_move_pct": t["price_move_pct"],
            "leverage":       t["leverage"],
            "duration_hours": t["duration_hours"],
            "symbol":         symbol,
            "trade_size":     round(trade_size, 4),
            "capital_at_time": capital_at_time,
        })

    # Win/loss by actual net P&L (after fees), not by outcome/exit_trigger —
    # a trade that hits TP (price moved favorably) can still be a net loss
    # once fees are subtracted, especially on a narrow BB channel where the
    # raw move barely covers the round-trip fee cost. Matches how the live
    # bot's own stats are computed (see /api/bb_bot_status below).
    closed_trades = [t for t in trades if t["outcome"] != "open"]
    wins      = [t for t in closed_trades if t["pnl_dollar"] > 0]
    losses    = [t for t in closed_trades if t["pnl_dollar"] <= 0]
    evaluated = len(wins) + len(losses)
    win_rate  = len(wins) / evaluated if evaluated else 0.0

    total_pnl_dollar  = sum(t["pnl_dollar"] for t in trades)
    total_fees_dollar = sum(t["fee_dollar"] for t in trades)
    avg_win_dollar    = sum(t["pnl_dollar"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss_dollar   = sum(t["pnl_dollar"] for t in losses) / len(losses) if losses else 0.0

    peak = running = max_dd = 0.0
    for t in trades:
        running += t["pnl_dollar"]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd

    cum = 0.0
    equity_curve = []
    for t in trades:
        cum += t["pnl_dollar"]
        equity_curve.append({"t": t["time"][:10], "v": round(cum, 4),
                              "ts": t["exit_ts_ms"] or t["entry_ts_ms"]})

    # Coverage = full cached range for this symbol+timeframe (may be wider than display window)
    all_cached = INDICATOR_OHLCV_CACHE.get(symbol, {}).get(timeframe, [])
    cache_coverage = {
        "from": _ms_to_iso(all_cached[0][0])[:10]  if all_cached else None,
        "to":   _ms_to_iso(all_cached[-1][0])[:10] if all_cached else None,
        "candles": len(all_cached),
    }

    return jsonify({
        "summary": {
            "total":             len(trades),
            "evaluated":         evaluated,
            "wins":              len(wins),
            "losses":            len(losses),
            "open":              len([t for t in trades if t["outcome"] == "open"]),
            "win_rate":          round(win_rate * 100, 1),
            "total_pnl_dollar":  round(total_pnl_dollar, 4),
            "total_fees_dollar": round(total_fees_dollar, 4),
            "avg_win_dollar":    round(avg_win_dollar, 4),
            "avg_loss_dollar":   round(avg_loss_dollar, 4),
            "max_dd_dollar":     round(max_dd, 4),
            "starting_capital":  starting_capital,
            "equity_curve":      equity_curve,
            "symbol":            symbol,
            "timeframe":         timeframe,
            "strategy_type":     stype,
            "oversold":          oversold,
            "overbought":        overbought,
            "sl_threshold_pct":  strategy.get("sl_threshold_pct", 0.75),
            "cooldown_n":        strategy.get("cooldown_n", 2),
            "cache_coverage":    cache_coverage,
        },
        "trades":     trades,
        "chart_data": chart_data,
    })



def _ms_to_iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()



@app.route("/api/ind_ohlcv")
def api_ind_ohlcv():
    """Return OHLCV slice from indicator cache for a symbol + timeframe.

    Used by the indicator trade detail modal chart.
    Query params: symbol, timeframe, from (ISO), to (ISO)
    """
    symbol    = normalise_mexc_symbol(request.args.get("symbol", "BTC/USDT"))
    timeframe = request.args.get("timeframe", "1h")
    from_iso  = request.args.get("from", "")
    to_iso    = request.args.get("to",   "")

    candles = INDICATOR_OHLCV_CACHE.get(symbol, {}).get(timeframe, [])
    if not candles:
        return jsonify([])

    from_ms = int(datetime.fromisoformat(from_iso.replace("Z", "+00:00")).timestamp() * 1000) if from_iso else None
    to_ms   = int(datetime.fromisoformat(to_iso.replace("Z",   "+00:00")).timestamp() * 1000) if to_iso   else None

    result = []
    for c in candles:
        ts = c[0]
        if from_ms is not None and ts < from_ms:
            continue
        if to_ms is not None and ts > to_ms:
            continue
        result.append({"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return jsonify(result)



_EARLIEST_CACHE: dict = {}  # (symbol, tf) -> earliest_ms or None


@app.route("/api/indicator_earliest")
def api_indicator_earliest():
    """Return the earliest candle timestamp available on MEXC for a symbol+timeframe.

    MEXC rejects since=epoch_0. We probe year-by-year from 2017 forward until
    we get a response, then binary-search within that year for the exact first candle.
    Result is cached in memory for the server lifetime.
    """
    symbol = normalise_mexc_symbol(request.args.get("symbol", "BTC/USDT"))
    tf = request.args.get("timeframe", "1h")
    key = (symbol, tf)
    if key not in _EARLIEST_CACHE:
        try:
            ex = _get_mexc_exchange()
            fetch_sym = symbol if ":" in symbol else symbol + ":USDT"
            earliest_ms = None
            found_year = None
            # Probe year by year until we find where MEXC has data
            for year in range(2017, datetime.now(tz=timezone.utc).year + 1):
                since = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
                rows = ex.fetch_ohlcv(fetch_sym, tf, since=since, limit=1)
                if rows:
                    earliest_ms = rows[0][0]
                    found_year = year
                    break
            # Refine: check previous year month-by-month (Dec → Jan) for exact first month
            if found_year and found_year > 2017:
                for month in range(12, 0, -1):
                    since = int(datetime(found_year - 1, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
                    rows = ex.fetch_ohlcv(fetch_sym, tf, since=since, limit=1)
                    if rows:
                        earliest_ms = rows[0][0]
                    else:
                        break  # no data for this month, stop going further back
            _EARLIEST_CACHE[key] = earliest_ms
        except Exception as e:
            logger.error(f"indicator_earliest failed {symbol} {tf}: {e}")
            return jsonify({"error": str(e)}), 500
    earliest_ms = _EARLIEST_CACHE.get(key)
    if earliest_ms is None:
        return jsonify({"earliest_ms": None, "earliest_date": None})
    earliest_date = datetime.fromtimestamp(earliest_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return jsonify({"earliest_ms": earliest_ms, "earliest_date": earliest_date})



@app.route("/api/bb_bot_status")
def api_bb_bot_status():
    """
    Return current state + closed trade history for the live/paper BB bot.
    Query params:
      symbol  — symbol_key from config (default "BTC")
    """
    symbol = request.args.get("symbol", "BTC").upper()

    state_path  = os.path.join(BASE_DIR, "data", "state",  f"bb_bot_state_{symbol}.json")
    trades_path = os.path.join(BASE_DIR, "data", "trades", f"bb_bot_trades_{symbol}.json")

    # --- State ---
    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except Exception:
            pass

    # --- Trades ---
    trades = []
    if os.path.exists(trades_path):
        try:
            with open(trades_path) as f:
                trades = json.load(f)
        except Exception:
            pass

    # Add fee_usdt / gross_pnl_usdt to every trade — reconstructed from
    # fields already stored (direction, entry, exit, quantity), not stored
    # directly by bb_bot.py, so this works retroactively for every past
    # trade too. notional = quantity * entry * CONTRACT_LOT is the dollar
    # exposure at entry; gross P&L is the raw price move over that notional
    # (the "sell higher than bought" part, before fees); fee is whatever's
    # left once the already-stored net pnl_usdt is subtracted from that.
    from strategies.bb_channel import CONTRACT_LOT
    for t in trades:
        entry = t.get("entry")
        exit_p = t.get("exit")
        qty = t.get("quantity")
        pnl_usdt = t.get("pnl_usdt")
        if None in (entry, exit_p, qty, pnl_usdt) or entry == 0:
            continue
        notional = qty * entry * CONTRACT_LOT
        raw_move = (exit_p - entry) / entry if t.get("direction") == "long" else (entry - exit_p) / entry
        gross_pnl_usdt = raw_move * notional
        t["gross_pnl_usdt"] = round(gross_pnl_usdt, 4)
        t["fee_usdt"] = round(gross_pnl_usdt - pnl_usdt, 4)

    # --- Funding (perp cost the bot does not book) ---
    # Marked against the 15m close at each settlement, read straight from the
    # existing OHLCV cache — no extra fetch, and no candle is ever added by
    # this path. Silently degrades to no funding fields if either the rate
    # history or the candle cache is unavailable.
    try:
        f_rates = _get_funding_rates(f"{symbol}/USDT")
        f_candles = _load_disk_ind_cache(f"{symbol}/USDT", _SIGNAL_TIMEFRAME)
        tf_ms = _TF_MS[_SIGNAL_TIMEFRAME]
        by_bucket = {c[0]: c[4] for c in f_candles}

        def _price_at(ms):
            return by_bucket.get((ms // tf_ms) * tf_ms)

        for t in trades:
            # Trades closed by a bot build that charges funding store it (and
            # already took it out of capital) — that is authoritative. Older
            # trades predate it, so funding is reconstructed for them here.
            if t.get("funding_usdt") is None:
                fund, n = _trade_funding_usdt(t, f_rates, _price_at, CONTRACT_LOT)
                if fund is None:
                    continue
                t["funding_usdt"] = round(fund, 4)
                t["funding_windows"] = n
                t["funding_charged"] = False    # not deducted from capital at the time
            else:
                t["funding_charged"] = True
            if t.get("pnl_usdt") is not None:
                t["pnl_after_funding_usdt"] = round(t["pnl_usdt"] - t["funding_usdt"], 4)
    except Exception as e:
        logger.warning(f"Funding enrichment skipped: {e}")

    # --- Stats ---
    # Funding is a cost of the trade exactly like the taker/maker fees already
    # inside pnl_usdt, so every headline figure below is net of it: P&L, win
    # rate, avg win/loss. A trade whose funding eats its profit is a loss.
    def _net(t):
        return (t.get("pnl_usdt") or 0) - (t.get("funding_usdt") or 0)

    closed = [t for t in trades if t.get("reason") != "open"]
    wins   = [t for t in closed if _net(t) > 0]
    losses = [t for t in closed if _net(t) <= 0]

    total_gross_pnl = round(sum(t.get("pnl_usdt", 0) for t in closed), 2)
    total_funding   = round(sum(t.get("funding_usdt", 0) or 0 for t in closed), 4)
    total_pnl       = round(sum(_net(t) for t in closed), 2)
    # Every cost the strategy paid: taker/maker on both legs (already inside
    # pnl_usdt, reconstructed per trade above) plus perp funding.
    total_exchange_fees = round(sum(t.get("fee_usdt", 0) or 0 for t in closed), 4)
    total_costs         = round(total_exchange_fees + total_funding, 4)
    funding_known   = sum(1 for t in closed if t.get("funding_usdt") is not None)
    # Trades closed before the bot charged funding had it reconstructed after
    # the fact, so their `capital` figures — and the bot's running capital —
    # never had it deducted. Surfaced so the UI can explain why Total P&L and
    # Capital differ by exactly this much until those trades age out.
    uncharged_funding = round(
        sum(t.get("funding_usdt", 0) or 0 for t in closed if not t.get("funding_charged")), 4)
    win_rate    = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
    avg_win     = round(sum(_net(t) for t in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss    = round(sum(_net(t) for t in losses) / len(losses), 2) if losses else 0.0

    # --- Equity curve (compounded, timestamps from trade exit_ts or entry_ts) ---
    starting_capital = float(state.get("capital", 0) or 0)
    # Accumulated from each trade's NET result rather than read off its stored
    # `capital` field. Those stored values are the bot's running capital at the
    # time, which for trades closed before it charged funding excludes it — so
    # replaying them would end at a different number than the bot's (trued-up)
    # capital, and would give a max drawdown that disagrees with the
    # funding-aware avg win/loss beside it. Accumulating _net keeps the equity
    # curve, Total P&L and Capital all consistent.
    equity = []
    cap = None
    for t in trades:
        if cap is None:
            # Infer starting capital from the first trade
            cap = (t.get("capital", 0) or 0) - (t.get("pnl_usdt", 0) or 0)
            equity.append({"ts": t.get("entry_ts", 0), "capital": round(cap, 2)})
        ts = t.get("exit_ts") or t.get("entry_ts", 0)
        cap += _net(t)
        equity.append({"ts": ts, "capital": round(cap, 2)})

    # --- Max drawdown on equity series ---
    max_dd = 0.0
    peak   = None
    for pt in equity:
        v = pt["capital"]
        if peak is None or v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # --- Mode and symbol from state ---
    mode        = state.get("mode", "paper")
    current_cap = round(float(state.get("capital", 0) or 0), 2)

    return jsonify({
        "symbol":          symbol,
        "mode":            mode,
        "state":           state,
        "trades":          trades,
        "stats": {
            "total_trades":  len(closed),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      win_rate,
            "total_pnl":       total_pnl,           # net of fees AND funding
            "total_gross_pnl": total_gross_pnl,     # as the bot booked it
            "total_funding":   total_funding,
            "total_exchange_fees": total_exchange_fees,
            "total_costs":         total_costs,       # exchange fees + funding
            "funding_known_trades": funding_known,
            "uncharged_funding":    uncharged_funding,
            "avg_win":       avg_win,
            "avg_loss":      avg_loss,
            "max_drawdown":  max_dd,
            "current_capital": current_cap,
        },
        "equity": equity,
    })


# ---------------------------------------------------------------------------
# Live Bot price chart
# ---------------------------------------------------------------------------


# Candle resolution auto-selected from the visible span: pick the FINEST
# resolution (most precision) whose candle count over the span still fits
# within a single MEXC OHLCV request (limit=500, same cap _fetch_ohlcv_range
# already pages against) — 500 15m candles covers ~5.2 days before the count
# forces a move to 1h, etc. Falls back to the coarsest resolution (1w) once
# even that would exceed 500 candles (spans over ~9.6 years).
_LIVE_CHART_RESOLUTIONS = ["15m", "1h", "4h", "1d", "1w"]
_LIVE_CHART_MAX_CANDLES = 500

# The timeframe bb_bot.py actually trades on. The Live Bot chart is an audit
# view of the bot's decisions, so its BB/EMA should be the bands the bot used
# — not bands recomputed at whatever candle resolution happens to be on screen.
# Above ~5.2 days the display drops to 1h+ candles (the 500-candle cap), and
# resolution-matched bands there are a different indicator entirely: markers
# stop landing on the band and correct entries look wrong.
#
# Capped by span because the override means fetching 15m data for the whole
# window regardless of display resolution: 14 days is ~1,344 candles (plus
# warmup), a few paged fetches against an on-disk cache. Beyond that the cost
# climbs fast (a year would be ~35k candles) while the value falls away — you
# are no longer auditing an individual signal. The cap also keeps this visually
# sane: within 14 days the display is at most 1h, so a 15m band is 4 points per
# candle, never the hairball it would be under 1d candles.
_SIGNAL_TIMEFRAME             = "15m"
_SIGNAL_INDICATOR_MAX_SPAN_MS = 14 * 24 * 3600 * 1000


def _timeframe_for_span(span_ms: int) -> str:
    for tf in _LIVE_CHART_RESOLUTIONS:
        if span_ms / _TF_MS[tf] <= _LIVE_CHART_MAX_CANDLES:
            return tf
    return _LIVE_CHART_RESOLUTIONS[-1]


@app.route("/api/live_chart")
def api_live_chart():
    """
    Return OHLCV + BB(20,3.0) + EMA(150) for the live bot price chart.

    Candle resolution is auto-selected from the requested span: the finest of
    15m/1h/4h/1d/1w whose candle count stays within _LIVE_CHART_MAX_CANDLES.

    Indicators are computed on _SIGNAL_TIMEFRAME (15m — what the bot actually
    trades) whenever the span is within _SIGNAL_INDICATOR_MAX_SPAN_MS, even if
    the displayed candles are coarser, so the bands on screen are the bands the
    bot signalled on. Beyond that cap they fall back to the display resolution
    and become a chart-only overlay. `indicator_timeframe` in the response says
    which happened, so the frontend can label it honestly.

    Because the indicator series can be finer than the candles, it ships with
    its own `indicator_ts` timestamps rather than being aligned index-by-index
    to `candles` — downsampling it onto candle timestamps would drop 3 of every
    4 points at 1h and break the property that a marker sits on the band.

    Uses the same gap-filling indicator cache as the indicator backtest tab.

    Query params:
      symbol — symbol_key from config (default "BTC")
      from, to — ISO date/datetime strings for the display window. If either
        is omitted, defaults to a 7-day window ending now.
    """
    from strategies.bb_channel import (
        compute_bb, compute_ema, BB_PERIOD, BB_STD, TREND_PERIOD,
    )

    symbol_key = request.args.get("symbol", "BTC").upper()
    ccxt_sym   = f"{symbol_key}/USDT"          # cache key format (spot-style)

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    from_param = request.args.get("from")
    to_param   = request.args.get("to")

    resolution_span_ms = None
    if from_param and to_param:
        try:
            from_ms = int(datetime.fromisoformat(from_param.replace("Z", "+00:00")).timestamp() * 1000)
            to_ms   = int(datetime.fromisoformat(to_param.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return jsonify({"error": "from/to must be ISO date or datetime strings"}), 400

        # The frontend sends whole calendar days: from = start of the From day,
        # to = end of the To day (23:59:59.999) so that day's candles are fully
        # covered. Using that raw span to pick a resolution over-counts by up to
        # a full day (picking, say, the same single day for both From and To
        # already spans nearly 24h once "to" is end-of-day, and adjacent days
        # span nearly 48h) — floor both to their own day start first so "From
        # Monday to Tuesday" reads as the 1-day gap a user actually picked, not
        # ~2 days.
        def _floor_to_day(ms):
            d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return int(d.timestamp() * 1000)
        resolution_span_ms = _floor_to_day(to_ms) - _floor_to_day(from_ms)
    else:
        # Default window: 7 days ending now.
        to_ms   = now_ms
        from_ms = now_ms - 7 * 24 * 3600 * 1000

    if to_ms <= from_ms:
        return jsonify({"error": "to must be after from"}), 400

    span_ms   = resolution_span_ms if resolution_span_ms is not None else to_ms - from_ms
    timeframe = _timeframe_for_span(span_ms)
    candle_ms = _TF_MS[timeframe]

    # A request tracking "now" (to_ms within one candle of the real current
    # time) is a live-tracking view — force a re-fetch of the last 2 candles
    # on every call so the forming candle and the most-recently-closed one
    # never sit frozen on a stale first-fetch snapshot (see docstring above).
    is_live_request = to_ms >= now_ms - candle_ms

    def _load(tf: str) -> list:
        tf_ms  = _TF_MS[tf]
        warmup = max(BB_PERIOD, TREND_PERIOD) * tf_ms * 3   # generous seed buffer
        return ensure_indicator_candles(
            ccxt_sym, tf, from_ms - warmup, to_ms,
            force_tail_ms=tf_ms * 2 if is_live_request else 0,
        )

    try:
        candles = _load(timeframe)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not candles:
        return jsonify({"error": f"No OHLCV data for {ccxt_sym}"}), 404

    # Indicators on the bot's own signal timeframe when the span allows it, so
    # the bands drawn are the bands the bot signalled on. Degrades to the
    # display resolution if that extra fetch fails — a chart labelled
    # display-only beats no chart at all.
    indicator_tf = timeframe
    ind_candles  = candles
    if timeframe != _SIGNAL_TIMEFRAME and span_ms <= _SIGNAL_INDICATOR_MAX_SPAN_MS:
        try:
            signal_candles = _load(_SIGNAL_TIMEFRAME)
            if signal_candles:
                ind_candles, indicator_tf = signal_candles, _SIGNAL_TIMEFRAME
        except Exception as e:
            logger.warning(
                f"Signal-timeframe indicators unavailable, falling back to {timeframe}: {e}"
            )

    bb_series  = compute_bb(ind_candles, BB_PERIOD, BB_STD)
    ema_series = compute_ema(ind_candles, TREND_PERIOD)

    # Only ship the display window to the client (warmup stays server-side)
    display_from_ms = from_ms - candle_ms * 10   # 10 candle grace on left edge
    out_candles = [c for c in candles if display_from_ms <= c[0] <= to_ms]

    # Indicator series carries its own timestamps — it can be finer than the
    # candles (15m bands under 1h candles), so it is not index-parallel to them.
    out_ind_ts   = []
    out_bb_upper = []
    out_bb_mid   = []
    out_bb_lower = []
    out_ema      = []
    for i, c in enumerate(ind_candles):
        if c[0] < display_from_ms or c[0] > to_ms:
            continue
        bb = bb_series[i]
        out_ind_ts  .append(c[0])
        out_bb_upper.append(bb[0] if bb else None)
        out_bb_mid  .append(bb[1] if bb else None)
        out_bb_lower.append(bb[2] if bb else None)
        out_ema     .append(ema_series[i])

    return jsonify({
        "candles":             out_candles,
        "indicator_ts":        out_ind_ts,
        "bb_upper":            out_bb_upper,
        "bb_mid":              out_bb_mid,
        "bb_lower":            out_bb_lower,
        "ema":                 out_ema,
        "timeframe":           timeframe,
        "indicator_timeframe": indicator_tf,
        "signal_timeframe":    _SIGNAL_TIMEFRAME,
        "from_ms":             from_ms,
        "to_ms":               to_ms,
        "now_ms":              now_ms,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import signal as _signal
    import subprocess as _sp
    from werkzeug.serving import is_running_from_reloader

    # Free port 5050 if something is already bound to it (e.g. a previous server run).
    # Only do this in the outer process, not in the reloader child.
    if not is_running_from_reloader():
        try:
            _pids = _sp.check_output(
                ["lsof", "-ti", "tcp:5050"], text=True
            ).split()
            for _pid in _pids:
                if int(_pid) != os.getpid():
                    os.kill(int(_pid), _signal.SIGTERM)
                    logger.info(f"Killed stale server on port 5050 (pid {_pid})")
        except _sp.CalledProcessError:
            pass  # nothing on that port

    CONFIG = load_config()

    if is_running_from_reloader():
        logger.info("Dashboard ready.")
    else:
        logger.info("Open http://localhost:5050 in your browser.")

    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=True)