"""
bots/bb_bot.py
--------------
BB Channel Rider — live / paper trading bot.
See STRATEGY_SPEC.md for the full locked specification (2026-05-10).

Architecture (Option A):
  - ccxt.pro WebSocket (watch_ohlcv) for real-time forming candle H/L
  - Candle close detected when forming candle timestamp changes
  - Entry: market order the instant price touches a BB band
  - SL: dual stop-limit + stop-market backstop (both reduceOnly)
  - TP: market close when price touches opposite band
  - All strategy math delegated to strategies/bb_channel.py
  - Exchange I/O delegated to exchange/mexc_futures.py

Run:
  python3 bots/bb_bot.py --symbol BTC

Switch paper → live:
  Set  mexc.mode: live  in config.yaml
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import json
import logging
import time
from typing import Optional

import ccxt
import ccxt.pro as ccxtpro
import requests
import yaml
from dotenv import load_dotenv

from strategies.bb_channel import (
    BB_PERIOD, TREND_PERIOD, COOLDOWN_N,
    FEE_TAKER, FEE_MAKER,
    compute_bb, compute_ema,
    sl_price       as core_sl_price,
    backstop_price as core_backstop_price,
    tp_price       as core_tp_price,
    snap_trigger   as core_snap_trigger,
    contracts      as core_contracts,
    check_snap_triggered,
    trail_sl,
    check_tp_hit,
    check_sl_hit,
    flip_valid,
    advance_cooldown,
    calc_pnl,
)
from exchange.mexc_futures import BBExecutor

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bb_bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_PATH    = "config.yaml"
CANDLE_TF      = "15m"
CANDLE_MS      = 15 * 60_000
CANDLES_NEEDED = 300     # 150 EMA warmup + 20 BB + 130 convergence buffer

# Funding settles every 8h, so the rate history barely moves; refreshed lazily
# on candle closes so a trade never waits on a network call in its exit path.
FUNDING_REFRESH_MS = 30 * 60_000
WS_LIMIT       = 302     # candles requested from WebSocket (300 closed + 1 forming + buffer)
MIN_HISTORY    = BB_PERIOD + TREND_PERIOD   # minimum closed candles before trading


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def find_symbol_cfg(config: dict, key: str) -> dict:
    for s in config.get("bb_bot", {}).get("symbols", []):
        if s["symbol_key"].upper() == key.upper():
            return s
    raise ValueError(
        f"Symbol '{key}' not found in config.yaml bb_bot.symbols. "
        f"Add it or check the symbol_key spelling."
    )


# ── State management ──────────────────────────────────────────────────────────

def fresh_bot_state() -> dict:
    return {
        "phase":          "idle",   # idle | in_position | cooldown
        "long_cd":        0,        # candles before long entries re-allowed
        "short_cd":       0,        # candles before short entries re-allowed
        "last_candle_ts": 0,
        "capital":        0.0,
        # in-position fields
        "pos_direction":         None,
        "pos_entry":             None,
        "pos_quantity":          0,
        "pos_sl":                None,   # stop-limit SL price
        "pos_backstop":          None,   # stop-market backstop price
        "pos_tp":                None,   # monitored TP target (not an exchange order)
        "pos_sl_order_id":       None,
        "pos_backstop_order_id": None,
        "pos_sl_snapped":        False,
        "pos_snap_at":           None,   # dynamic threshold, updated each candle close
        "pos_entry_ts":          None,
        # Wick already printed when the current SL/TP levels were set mid-candle.
        # Used to ignore the part of the forming candle that predates them —
        # a stop cannot be hit by a price that happened before it existed.
        "pos_lvl_candle_ts":     None,   # forming-candle ts when levels were set
        "pos_lvl_h_at":          None,   # forming high at that moment
        "pos_lvl_l_at":          None,   # forming low at that moment
    }


def _state_path(symbol_key: str) -> str:
    return f"data/state/bb_bot_state_{symbol_key.upper()}.json"


def _trades_path(symbol_key: str) -> str:
    return f"data/trades/bb_bot_trades_{symbol_key.upper()}.json"


def save_state(state: dict, symbol_key: str):
    os.makedirs("data/state", exist_ok=True)
    with open(_state_path(symbol_key), "w") as f:
        json.dump(state, f, indent=2)


def load_state(symbol_key: str) -> dict:
    state = fresh_bot_state()
    path  = _state_path(symbol_key)
    if not os.path.exists(path):
        return state
    try:
        with open(path) as f:
            state.update(json.load(f))
        logger.info(f"State restored: phase={state['phase']}  long_cd={state['long_cd']}  short_cd={state['short_cd']}")
    except Exception as e:
        logger.warning(f"Could not load state: {e} — starting fresh")
    return state


def append_trade(trade: dict, symbol_key: str):
    os.makedirs("data/trades", exist_ok=True)
    path   = _trades_path(symbol_key)
    trades = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                trades = json.load(f)
        except Exception:
            pass
    trades.append(trade)
    # Write to a temp file then rename — atomic on POSIX, prevents corruption
    # from concurrent reads (e.g. dashboard) and avoids partial-write data loss.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, path)


# ── Telegram notifier ─────────────────────────────────────────────────────────

class BBNotifier:

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, text: str):
        """
        Send a message, stamped with the event time in UTC.

        The stamp matters: Telegram renders *delivery* time in the reader's local
        timezone, so a CEST phone shows 08:07 for an event the dashboard, the
        trade log and the bot's own logs all call 06:07 — every alert needed a
        mental +2 to reconcile. Worse, delivery time is not event time; a delayed
        message would misreport when the trade actually happened.
        Stamping here rather than per-message means every alert type gets it,
        including any added later.
        """
        if not self.enabled:
            return
        stamped = f"{text}\n_{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC_"
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": stamped, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    def started(self, symbol: str, mode: str, capital: float):
        badge = "_(PAPER)_" if mode == "paper" else "⚡ *LIVE*"
        self.send(
            f"🤖 *BB Channel Rider Started* {badge}\n"
            f"Symbol:  `{symbol}`\n"
            f"Capital: `{capital:,.2f} USDT`"
        )

    def entered(self, direction: str, price: float, sl: float,
                backstop: float, tp: float, qty: int, mode: str,
                margin: float = 0.0):
        emoji = "🟢" if direction == "long" else "🔴"
        tag   = " _(PAPER)_" if mode == "paper" else ""
        self.send(
            f"{emoji} *Entry*{tag}\n"
            f"Dir:      {direction.upper()}  @  `{price:,.2f}`\n"
            f"SL:       `{sl:,.2f}`  Backstop: `{backstop:,.2f}`\n"
            f"TP:       `{tp:,.2f}`  Margin: `${margin:,.2f}`"
        )

    def sl_snapped(self, new_sl: float, backstop: float):
        self.send(
            f"📌 *SL Snapped to Mid-Band*\n"
            f"SL: `{new_sl:,.2f}`  Backstop: `{backstop:,.2f}`"
        )

    def sl_trailed(self, new_sl: float, backstop: float):
        self.send(f"📈 *SL Trailed*  SL: `{new_sl:,.2f}`  Backstop: `{backstop:,.2f}`")

    def trade_closed(self, direction: str, entry: float, exit_p: float,
                     pnl_pct: float, pnl_usdt: float, reason: str,
                     capital: float, mode: str,
                     funding_usdt: float = 0.0, funding_windows: int = 0,
                     net_usdt: float = None):
        # Win/loss reads off the NET figure — the number that actually moved
        # capital — so a trade whose funding eats its profit is not badged green.
        net   = pnl_usdt if net_usdt is None else net_usdt
        emoji = "✅" if net >= 0 else "❌"
        tag   = " _(PAPER)_" if mode == "paper" else ""
        body = (
            f"{emoji} *Trade Closed*{tag}\n"
            f"Dir:     {direction.upper()}\n"
            f"Entry:   `{entry:,.2f}` → `{exit_p:,.2f}`\n"
            f"Reason:  {reason}\n"
            f"P&L:     `{pnl_pct:+.2f}%`  (`{pnl_usdt:+.2f} USDT` after fees)\n"
        )
        if funding_windows:
            body += (f"Funding: `{-funding_usdt:+.4f} USDT` "
                     f"({funding_windows} settlement{'' if funding_windows == 1 else 's'})\n"
                     f"Net:     `{net:+.2f} USDT`\n")
        body += f"Capital: `{capital:,.2f} USDT`"
        self.send(body)

    def position_abandoned(self, direction: str, entry: float, qty: int,
                           mode: str, mark: Optional[float] = None,
                           pnl_pct: Optional[float] = None,
                           pnl_usdt: Optional[float] = None,
                           held_h: Optional[float] = None):
        tag  = " _(PAPER)_" if mode == "paper" else ""
        body = (
            f"⚠️ *Position Dropped on Restart*{tag}\n"
            f"Dir:     {direction.upper()}  @  `{entry:,.2f}`  qty `{qty}`\n"
        )
        if mark is not None:
            held = f"  (held {held_h:.1f}h)" if held_h is not None else ""
            body += f"Mark:    `{mark:,.2f}`{held}\n"
        if pnl_usdt is not None:
            body += f"Unreal.: `{pnl_pct:+.2f}%`  (`{pnl_usdt:+.2f} USDT`)\n"
        body += "_Not closed — no P&L booked, not logged as a trade._"
        self.send(body)

    def flip_skipped(self, flip_dir: str, price: float, band: float):
        self.send(
            f"↩️ *Flip Skipped — price pulled back*\n"
            f"Wanted: {flip_dir.upper()}  Price: `{price:,.2f}`  Band: `{band:,.2f}`"
        )

    def min_contracts_skip(self, symbol: str, capital: float, price: float):
        self.send(
            f"⚠️ *Trade Skipped — Insufficient Capital*\n"
            f"Symbol:  `{symbol}`\n"
            f"Capital: `{capital:,.2f} USDT` cannot buy 1 contract @ `{price:,.2f}`\n"
            f"Bot stays active — will retry on next signal."
        )

    def capital_warning(self, pct_lost: float, capital: float):
        self.send(
            f"⚠️ *Capital Warning — Down {pct_lost:.0f}%*\n"
            f"Current capital: `{capital:,.2f} USDT`"
        )

    def stopped(self, reason: str = "manual"):
        tag = "🛑 *BB Bot Stopped*" if reason == "manual" else "💀 *BB Bot Crashed*"
        self.send(f"{tag}\nReason: `{reason}`")

    def error(self, msg: str):
        self.send(f"🚨 *BB Bot Error*\n{msg}")


# ── Main bot ──────────────────────────────────────────────────────────────────

class BBBot:

    def __init__(self, config: dict, symbol_cfg: dict):
        mexc_cfg = config.get("mexc", {})
        tg_cfg   = config.get("telegram_bot", {})

        self.mode         = mexc_cfg.get("mode", "paper")
        self.symbol_key   = symbol_cfg["symbol_key"]
        self.ccxt_symbol  = symbol_cfg["ccxt_symbol"]
        self.leverage     = int(symbol_cfg.get("leverage", 10))
        self.sizing_pct   = float(symbol_cfg.get("sizing_pct", 0.25))
        self.starting_cap = float(symbol_cfg.get("starting_capital", 1000.0))
        self.contract_lot = float(symbol_cfg.get("contract_lot", 0.0001))  # overwritten on startup

        self.executor = BBExecutor(config, symbol_cfg["ccxt_symbol"])
        # Credentials come from the environment ONLY. config.yaml is tracked in
        # git; .env is not. This previously fell back to tg_cfg["token"], which
        # meant the tracked config was a working place to put a secret — and a
        # config file that accepts secrets eventually receives one. Refusing the
        # key outright is the difference between a leak and a startup error.
        if tg_cfg.get("token") or tg_cfg.get("chat_id"):
            raise SystemExit(
                "Refusing to start: Telegram credentials found in config.yaml.\n"
                "That file is tracked in git and would be published. Remove the "
                "token/chat_id keys and set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_NOTIFY_USER_ID in .env instead (see .env.example).\n"
                "If the secret was already committed, rotate it via @BotFather — "
                "deleting it from the file does not remove it from git history."
            )

        self.notifier = BBNotifier(
            token   = os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id = os.getenv("TELEGRAM_NOTIFY_USER_ID", ""),
        ) if tg_cfg.get("enabled", True) else BBNotifier(token="", chat_id="")

        # ccxt.pro exchange — WebSocket + REST (superset of ccxt)
        # newUpdates=False: watch_ohlcv() must return the full rolling window
        # (up to `limit`) on every call, not just the delta since the last
        # call. MEXC's swap kline stream pushes one tick at a time, so with
        # the ccxt.pro default (newUpdates=True) the delta is almost always
        # exactly 1 candle — meaning the close-detection branch below never
        # sees candles[-2] (the newly-closed candle) and silently discards
        # every candle close, forever. Confirmed live 2026-08-08.
        self.ws_exchange = ccxtpro.mexc({
            "apiKey": os.getenv("MEXC_API_KEY",    ""),
            "secret": os.getenv("MEXC_API_SECRET", ""),
            "options": {"defaultType": "swap"},
            "newUpdates": False,
        })

        self.state   = load_state(self.symbol_key)
        self.capital = float(self.state.get("capital") or self.starting_cap)

        # Indicator state — updated on each candle close
        self._closed_candles: list         = []
        self._funding_rates: list          = []   # [{"ts", "rate"}], oldest first
        self._funding_fetched_ms: float    = 0.0
        self._last_bb:    Optional[tuple]  = None   # BB of last closed candle
        self._prev_ema:   Optional[float]  = None   # EMA of second-to-last closed candle
        self._prev_close: Optional[float]  = None   # close of second-to-last closed candle

        # Latest forming candle H/L — updated on every WebSocket tick
        self._forming_h: float = 0.0
        self._forming_l: float = float("inf")
        # Forming candle id + last traded price. The price is what a level set
        # mid-candle must be judged against; the candle's own H/L may predate it.
        self._forming_ts: Optional[int]   = None
        self._price:      Optional[float] = None

        # Capital warning thresholds fired so far (avoid repeating)
        self._warned:     set   = set()
        self._start_cap:  float = self.capital

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            save_state(self.state, self.symbol_key)
            self.notifier.stopped("manual")
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            save_state(self.state, self.symbol_key)
            self.notifier.stopped(str(e))

    # ── Startup ───────────────────────────────────────────────────────────────

    async def _main(self):
        logger.info(
            f"BB Channel Rider starting — symbol={self.symbol_key}  "
            f"mode={self.mode.upper()}  capital={self.capital:.2f} USDT"
        )

        # Pre-load swap markets so watch_ohlcv never triggers lazy market
        # loading mid-loop (which would call the spot capital/config/getall
        # endpoint and throw a NetworkError on the first WS message).
        try:
            await self.ws_exchange.fetch_markets({"type": "swap"})
            logger.debug("Swap markets pre-loaded")
        except Exception as e:
            logger.warning(f"fetch_markets pre-load failed (non-fatal): {e}")

        await self._load_contract_lot()
        await self._set_leverage()

        # Seed candle history via REST before entering WebSocket loop
        self._closed_candles = await self._fetch_closed_candles()
        if len(self._closed_candles) >= MIN_HISTORY:
            self._recompute_indicators()
            logger.info(
                f"Indicators ready — {len(self._closed_candles)} closed candles loaded  "
                f"BB=({self._last_bb[2]:.0f}/{self._last_bb[1]:.0f}/{self._last_bb[0]:.0f})"
                if self._last_bb else "Indicators ready"
            )
        else:
            logger.warning(
                f"Only {len(self._closed_candles)} candles available — "
                f"need {MIN_HISTORY}. Bot will wait for more history."
            )

        # Seed funding rates now so a trade closing before the first candle
        # close is still charged correctly.
        await self._refresh_funding_rates()

        # Sent before the restart guard so an abandonment alert reads in order.
        self.notifier.started(self.symbol_key, self.mode, self.capital)

        await self._handle_restart()
        await self._websocket_loop()

    async def _load_contract_lot(self):
        """
        Fetch contract lot size from MEXC futures market info.
        Paper mode: skip — use config fallback directly (no exchange call needed).
        Live mode: fetch_markets("swap") targets the futures API, avoids the
        spot /capital/config/getall endpoint that ccxt.load_markets() hits.
        Falls back to config value on any error.
        """
        if self.mode == "paper":
            logger.info(f"[PAPER] Contract lot: {self.contract_lot} (from config)")
            return
        try:
            markets = await self.ws_exchange.fetch_markets({"type": "swap"})
            for m in markets:
                if m.get("symbol") == self.ccxt_symbol:
                    lot = m.get("contractSize")
                    if lot:
                        self.contract_lot = float(lot)
                        logger.info(f"Contract lot from MEXC: {self.contract_lot}")
                        return
        except Exception as e:
            logger.warning(f"fetch_markets failed: {e}")
        logger.warning(f"Using fallback contract lot from config: {self.contract_lot}")

    async def _set_leverage(self):
        if self.mode == "paper":
            logger.info(f"[PAPER] Leverage: {self.leverage}x")
            return
        try:
            await self.ws_exchange.set_leverage(self.leverage, self.ccxt_symbol)
            logger.info(f"Leverage set: {self.leverage}x")
        except Exception as e:
            logger.warning(f"set_leverage failed (may already be set): {e}")

    async def _fetch_closed_candles(self) -> list:
        """Fetch the last CANDLES_NEEDED closed candles via REST.

        Uses a fresh synchronous ccxt.mexc instance (run in a thread executor)
        so it is completely independent of the ws_exchange connection state.
        A WS timeout or broken WS connection never blocks this REST call.
        The last candle in the response is the currently-forming candle —
        we drop it so the list contains only fully closed candles.
        """
        def _sync_fetch():
            ex = ccxt.mexc({
                "apiKey": os.getenv("MEXC_API_KEY",    ""),
                "secret": os.getenv("MEXC_API_SECRET", ""),
                "options": {"defaultType": "swap"},
            })
            return ex.fetch_ohlcv(self.ccxt_symbol, CANDLE_TF,
                                  limit=CANDLES_NEEDED + 1)
        try:
            raw = await asyncio.get_event_loop().run_in_executor(None, _sync_fetch)
            return raw[:-1] if len(raw) >= 2 else raw
        except Exception as e:
            logger.error(f"fetch_ohlcv (seed) failed: {e}")
            return []

    async def _refresh_funding_rates(self):
        """
        Refresh the cached funding-rate history.

        Perpetuals charge funding at fixed settlement times (every 8h on MEXC).
        Rates only change on those settlements, so this is polled lazily on
        candle closes rather than fetched at close time: a trade must never
        block, or fail, on a network call in its exit path. If the fetch fails
        the previous rates stay in use and the next candle retries.
        """
        now = time.time() * 1000
        if now - self._funding_fetched_ms < FUNDING_REFRESH_MS and self._funding_rates:
            return

        def _sync():
            ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
            ex.fetch_markets({"type": "swap"})
            return ex.fetch_funding_rate_history(self.ccxt_symbol, limit=200)

        try:
            raw = await asyncio.get_event_loop().run_in_executor(None, _sync)
            rates = sorted(
                ({"ts": int(r["timestamp"]), "rate": float(r["fundingRate"])}
                 for r in raw
                 if r.get("timestamp") is not None and r.get("fundingRate") is not None),
                key=lambda r: r["ts"],
            )
            if rates:
                self._funding_rates = rates
                self._funding_fetched_ms = now
                logger.debug(f"Funding rates refreshed: {len(rates)} settlements")
        except Exception as e:
            logger.warning(f"Funding rate refresh failed (keeping previous): {e}")

    def _funding_cost(self, direction: str, qty: int, entry: float,
                      entry_ts: int, exit_ts: int) -> tuple:
        """
        Funding paid over the life of a position, in USDT, plus the number of
        settlements charged. Positive = paid out, negative = received.

        A position is charged if it is open when a settlement lands, so the
        window is (entry_ts, exit_ts]. When the rate is positive longs pay
        shorts, hence the sign flip. Notional is marked at the closed-candle
        price nearest each settlement rather than at entry, since that is what
        the exchange charges against; entry price is the fallback.

        Returns (0.0, 0) when rates are unavailable — a missing rate history
        must not stop a trade closing, and under-charging is visible in the
        dashboard (which recomputes funding independently) rather than silent.
        """
        if not self._funding_rates or not entry_ts or not exit_ts:
            return 0.0, 0

        by_bucket = {c[0]: c[4] for c in self._closed_candles}
        tf_ms = CANDLE_MS
        total, n = 0.0, 0
        for r in self._funding_rates:
            if not (entry_ts < r["ts"] <= exit_ts):
                continue
            px = by_bucket.get((r["ts"] // tf_ms) * tf_ms) or entry
            pay = qty * px * self.contract_lot * r["rate"]
            total += pay if direction == "long" else -pay
            n += 1
        return total, n

    async def _handle_restart(self):
        """Safe recovery when restarting with an active position."""
        if self.state["phase"] != "in_position":
            return

        logger.warning("Restarting with in_position — cancelling orders, reverting to cooldown.")
        await self.executor.cancel_all_orders()
        self._notify_abandoned()

        if self.mode == "live":
            sl  = self.state.get("pos_sl")
            bs  = self.state.get("pos_backstop")
            qty = self.state.get("pos_quantity", 0)
            dir = self.state.get("pos_direction")
            if sl and bs and qty and dir:
                sl_oid, bs_oid = await self.executor.place_dual_sl(dir, qty, sl, bs)
                self.state["pos_sl_order_id"]       = sl_oid
                self.state["pos_backstop_order_id"] = bs_oid
                logger.info(f"Dual SL re-placed on restart: SL={sl:.2f}  Backstop={bs:.2f}")
            else:
                logger.warning("Incomplete position state — market closing for safety")
                await self.executor.close_position_market()

        self.state["phase"]    = "cooldown"
        self.state["long_cd"]  = COOLDOWN_N
        self.state["short_cd"] = COOLDOWN_N
        save_state(self.state, self.symbol_key)

    def _notify_abandoned(self):
        """
        Alert that a live position was dropped by the restart guard.

        The position is discarded, not closed: no P&L is booked and nothing is
        written to the trade log, so without this it disappears silently. Marked
        to market off the last closed candle purely to report what it was worth
        at the moment it was dropped.
        """
        direction = self.state.get("pos_direction")
        entry     = self.state.get("pos_entry")
        qty       = self.state.get("pos_quantity", 0)
        if not direction or not entry:
            logger.warning("Abandoned position has incomplete state — alert sent without detail.")
            self.notifier.position_abandoned(direction or "?", entry or 0.0, qty, self.mode)
            return

        mark = pnl_pct = pnl_usdt = held_h = None
        try:
            if self._closed_candles:
                mark = float(self._closed_candles[-1][4])
                # Taker both ways — what it would have cost to market out here.
                pnl_pct, pnl_usdt = calc_pnl(
                    direction, entry, mark, self.leverage,
                    self.capital, self.sizing_pct,
                    fee_entry_rate=FEE_TAKER,
                    fee_exit_rate=FEE_TAKER,
                )
            entry_ts = self.state.get("pos_entry_ts")
            if entry_ts:
                held_h = (time.time() * 1000 - entry_ts) / 3_600_000
        except Exception as e:
            logger.warning(f"Could not mark abandoned position to market: {e}")

        logger.warning(
            f"ABANDONED  {direction.upper()}  entry={entry:.2f}  qty={qty}"
            + (f"  mark={mark:.2f}  unrealised={pnl_usdt:+.2f} USDT" if pnl_usdt is not None else "")
            + "  — not closed, no P&L booked, not logged as a trade."
        )
        self.notifier.position_abandoned(
            direction, entry, qty, self.mode, mark, pnl_pct, pnl_usdt, held_h
        )

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _reseed_indicators(self):
        """
        Re-fetch closed candles via REST and recompute all indicators.
        Called after every WebSocket reconnect so that _last_bb, _prev_ema,
        and _prev_close are never stale after a long sleep/disconnect period.
        Without this, a Mac waking after hours of sleep would enter the loop
        with indicator values frozen from whenever the last candle was processed
        — potentially many candles / hours out of date.
        """
        fresh = await self._fetch_closed_candles()
        if fresh and len(fresh) >= MIN_HISTORY:
            self._closed_candles = fresh
            self._recompute_indicators()
            logger.info(
                f"Re-seeded after reconnect — {len(fresh)} candles  "
                f"BB=({self._last_bb[2]:.0f}/{self._last_bb[1]:.0f}/{self._last_bb[0]:.0f})  "
                f"EMA={self._prev_ema:.0f}"
                if self._last_bb and self._prev_ema else "Re-seeded (indicators pending)"
            )
        else:
            logger.warning("Re-seed skipped — insufficient candle history")

    async def _websocket_loop(self):
        """
        Main loop. Receives forming candle updates from MEXC WebSocket.
        Detects candle closes when the forming candle timestamp changes.

        On every reconnect (network error or timeout) the loop re-seeds
        indicator state from REST before resuming. This prevents stale
        _last_bb / _prev_ema / _prev_close values from bypassing the trend
        filter or setting a wrong TP when the bot wakes after a long sleep.
        """
        prev_forming_ts = None

        while True:
            try:
                candles = await self.ws_exchange.watch_ohlcv(
                    self.ccxt_symbol, CANDLE_TF, limit=WS_LIMIT
                )
                if not candles:
                    continue

                forming    = candles[-1]
                forming_ts = forming[0]
                forming_h  = forming[2]
                forming_l  = forming[3]
                price      = forming[4]          # last trade — the live price

                # Update latest forming candle price (used by flip check)
                self._forming_h  = forming_h
                self._forming_l  = forming_l
                self._forming_ts = forming_ts
                self._price      = price

                # Candle close detected: forming timestamp changed
                if prev_forming_ts is not None and forming_ts != prev_forming_ts:
                    if len(candles) < 2:
                        # Delta update contained only the new forming candle —
                        # the closed candle isn't in this batch. Skip and wait
                        # for the next tick which will have the full window.
                        prev_forming_ts = forming_ts
                        continue
                    newly_closed = candles[-2]
                    self._closed_candles.append(newly_closed)
                    if len(self._closed_candles) > CANDLES_NEEDED:
                        self._closed_candles = self._closed_candles[-CANDLES_NEEDED:]

                    self.state["last_candle_ts"] = newly_closed[0]

                    if len(self._closed_candles) >= MIN_HISTORY:
                        self._recompute_indicators()

                    # Kept off the trade-exit path deliberately — see the
                    # docstring. Self-throttles to FUNDING_REFRESH_MS.
                    await self._refresh_funding_rates()

                    await self._on_candle_close()

                prev_forming_ts = forming_ts

                # Real-time price monitoring
                if len(self._closed_candles) >= MIN_HISTORY and self._last_bb is not None:
                    await self._on_price_tick(forming_h, forming_l, price)

                save_state(self.state, self.symbol_key)

            except ccxtpro.NetworkError as e:
                logger.warning(f"WebSocket network error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)
                prev_forming_ts = None          # discard stale forming-candle reference
                await self._reseed_indicators() # refresh BB/EMA/prev_close from REST
            except Exception as e:
                logger.error(f"WebSocket error: {e} — reconnecting in 5s")
                self.notifier.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)
                prev_forming_ts = None
                await self._reseed_indicators()

    def _recompute_indicators(self):
        """Recompute BB and EMA from the current closed candle history."""
        bb_vals  = compute_bb(self._closed_candles)
        ema_vals = compute_ema(self._closed_candles)
        last_idx = len(self._closed_candles) - 1
        prev_idx = last_idx - 1

        self._last_bb    = bb_vals[last_idx]
        self._prev_ema   = ema_vals[prev_idx]   if prev_idx >= 0 else None
        self._prev_close = self._closed_candles[prev_idx][4] if prev_idx >= 0 else None

    # ── Per-tick price monitoring ─────────────────────────────────────────────

    async def _on_price_tick(self, h: float, l: float, price: float):
        """Called on every WebSocket update with forming candle's H, L and last price."""
        phase = self.state["phase"]

        # Entries allowed from idle AND cooldown (per-direction cd gates each direction)
        if phase in ("idle", "cooldown"):
            await self._check_entry(h, l)
        elif phase == "in_position":
            await self._check_position(h, l, price)

    async def _check_entry(self, h: float, l: float):
        """Check if forming candle wick touches a band. Enter immediately if so."""
        if self._last_bb is None or self._prev_ema is None or self._prev_close is None:
            return

        upper, _mid, lower = self._last_bb
        long_cd  = self.state["long_cd"]
        short_cd = self.state["short_cd"]

        direction = None
        if long_cd == 0 and l <= lower and self._prev_close > self._prev_ema:
            direction = "long"
        elif short_cd == 0 and h >= upper and self._prev_close < self._prev_ema:
            direction = "short"

        if direction is None:
            return

        entry_price = lower if direction == "long" else upper
        await self._enter_position(direction, entry_price)

    def _stamp_levels(self):
        """
        Record which candle the SL/TP levels were set in, and how much of that
        candle's range had already printed at that moment.
        """
        self.state["pos_lvl_candle_ts"] = self._forming_ts
        self.state["pos_lvl_h_at"]      = self._forming_h
        self.state["pos_lvl_l_at"]      = self._forming_l

    def _live_basis(self, h: float, l: float, price: float) -> tuple:
        """
        The high/low the current SL/TP levels may legitimately be tested against.

        The forming candle's H/L are cumulative since the candle opened, so a
        level set mid-candle inherits a range that partly happened before it
        existed — that is how a freshly-snapped stop could be "hit" by a wick
        from minutes earlier. Where the wick has not advanced beyond what was
        already printed when the level was set, use the live price instead.

        Once the candle rolls over the snapshot no longer applies and the plain
        wick is used again, so real moves between ticks are still caught.
        """
        if price is None or self.state.get("pos_lvl_candle_ts") != self._forming_ts:
            return h, l
        h_at = self.state.get("pos_lvl_h_at")
        l_at = self.state.get("pos_lvl_l_at")
        if h_at is None or l_at is None:
            return h, l
        return (h if h > h_at else price), (l if l < l_at else price)

    async def _check_position(self, h: float, l: float, price: float):
        """Check TP hit, SL snap, and SL hit on every forming candle tick."""
        direction = self.state["pos_direction"]
        tp        = self.state["pos_tp"]
        sl        = self.state["pos_sl"]
        snap_at   = self.state["pos_snap_at"]

        # Live: detect exchange-side close (TP/SL filled on MEXC)
        if self.mode == "live":
            if not await self._live_position_still_open():
                exit_price, reason = await self._get_live_exit_info(h, l)
                await self._close_position(direction, exit_price, reason, int(time.time() * 1000))
                return

        eff_h, eff_l = self._live_basis(h, l, price)

        # TP checked before SL (priority rule per STRATEGY_SPEC.md)
        if tp is not None and check_tp_hit(direction, eff_h, eff_l, tp):
            await self._close_position(direction, tp, "TP", int(time.time() * 1000))
            return

        # SL snap — one-time trigger using current dynamic snap_at
        if snap_at is not None and not self.state["pos_sl_snapped"]:
            if check_snap_triggered(direction, eff_h, eff_l, snap_at):
                await self._do_snap(direction)
                # _do_snap moves the SL and re-stamps the snapshot. The wick that
                # triggered the snap predates the new stop, so re-read both the
                # basis and the stop before testing it.
                eff_h, eff_l = self._live_basis(h, l, price)
                sl = self.state["pos_sl"]

        # SL hit
        if sl is not None and check_sl_hit(direction, eff_h, eff_l, sl):
            await self._close_position(direction, sl, "SL", int(time.time() * 1000))

    # ── Candle close handler ──────────────────────────────────────────────────

    async def _on_candle_close(self):
        """
        Called once per 15m candle close.
        Advances cooldown counters, updates dynamic snap_at,
        trails SL monotonically, and refreshes monitored TP target.
        """
        phase      = self.state["phase"]
        last_close = self._closed_candles[-1][4] if self._closed_candles else None

        # Advance per-direction cooldown counters (when not in position)
        if phase != "in_position":
            new_long_cd, new_short_cd = advance_cooldown(
                self.state["long_cd"],
                self.state["short_cd"],
                last_close,
                self._last_bb,
            )
            self.state["long_cd"]  = new_long_cd
            self.state["short_cd"] = new_short_cd

            if new_long_cd == 0 and new_short_cd == 0 and phase == "cooldown":
                self.state["phase"] = "idle"
                logger.info("Cooldown expired — idle")
            elif phase == "cooldown":
                logger.info(f"Cooldown: long_cd={new_long_cd}  short_cd={new_short_cd}")
            return

        # In-position candle close processing
        if self._last_bb is None:
            return

        direction       = self.state["pos_direction"]
        upper, mid, lower = self._last_bb

        # Update dynamic snap_at from current bands (STRATEGY_SPEC DP3)
        new_snap_at = core_snap_trigger(direction, self._last_bb)
        self.state["pos_snap_at"] = new_snap_at

        # Monotone SL trail after snap (STRATEGY_SPEC DP3)
        if self.state["pos_sl_snapped"]:
            new_sl = trail_sl(direction, self.state["pos_sl"], mid)
            if new_sl != self.state["pos_sl"]:
                new_backstop = core_backstop_price(
                    direction, new_sl, sl_pct=0.0, backstop_gap=0.005
                )
                qty = self.state["pos_quantity"]
                sl_oid, bs_oid = await self.executor.update_dual_sl(
                    direction, qty, new_sl, new_backstop,
                    self.state["pos_sl_order_id"],
                    self.state["pos_backstop_order_id"],
                )
                self.state["pos_sl"]                = new_sl
                self.state["pos_backstop"]          = new_backstop
                self.state["pos_sl_order_id"]       = sl_oid
                self.state["pos_backstop_order_id"] = bs_oid
                logger.info(f"SL trailed: {new_sl:.2f}  backstop: {new_backstop:.2f}")
                self.notifier.sl_trailed(new_sl, new_backstop)

        # Update monitored TP target
        new_tp = core_tp_price(direction, self._last_bb)
        if new_tp != self.state["pos_tp"]:
            self.state["pos_tp"] = new_tp
            logger.debug(f"TP target updated: {new_tp:.2f}")

    # ── Enter position ────────────────────────────────────────────────────────

    async def _enter_position(self, direction: str, entry_price: float):
        """
        Market entry + dual SL. Phase set to in_position immediately
        (before any await) to prevent re-entry on concurrent ticks.
        """
        # Guard — set phase before any await
        self.state["phase"] = "in_position"

        # Live: sync capital from exchange
        if self.mode == "live":
            bal = await self.executor.get_balance()
            if bal is not None:
                self.capital = bal

        qty = core_contracts(self.capital, entry_price, self.sizing_pct, self.contract_lot,
                             self.leverage)
        if qty == 0:
            logger.warning(
                f"Trade skipped — qty=0: capital={self.capital:.2f} USDT  "
                f"price={entry_price:.2f}  lot={self.contract_lot}"
            )
            self.notifier.min_contracts_skip(self.symbol_key, self.capital, entry_price)
            self.state["phase"] = "idle" if (self.state["long_cd"] == 0 and self.state["short_cd"] == 0) else "cooldown"
            return

        sl       = core_sl_price(direction, entry_price)
        backstop = core_backstop_price(direction, entry_price)
        tp       = core_tp_price(direction, self._last_bb)
        snap_at  = core_snap_trigger(direction, self._last_bb)

        # Market entry — paper uses entry_price (band), live returns actual fill
        fill_price, _order_id = await self.executor.place_market_entry(direction, qty, entry_price)
        if fill_price is None:
            logger.error("Market entry failed — reverting")
            self.notifier.error(f"Market entry failed ({direction.upper()})")
            self.state["phase"] = "idle"
            return

        # Dual SL
        sl_oid, bs_oid = await self.executor.place_dual_sl(direction, qty, sl, backstop)
        if sl_oid is None:
            logger.error("Dual SL placement failed — market closing for safety")
            await self.executor.close_position_market()
            self.notifier.error("Dual SL placement failed — position aborted")
            self.state["phase"] = "idle"
            return

        self.state.update({
            "phase":                  "in_position",
            "capital":                self.capital,
            "pos_direction":          direction,
            "pos_entry":              fill_price,
            "pos_quantity":           qty,
            "pos_sl":                 sl,
            "pos_backstop":           backstop,
            "pos_tp":                 tp,
            "pos_sl_order_id":        sl_oid,
            "pos_backstop_order_id":  bs_oid,
            "pos_sl_snapped":         False,
            "pos_snap_at":            snap_at,
            "pos_entry_ts":           int(time.time() * 1000),
        })
        # SL/TP were just set mid-candle — the range already printed in this
        # candle happened before they existed and must not be able to hit them.
        self._stamp_levels()

        margin = qty * fill_price * self.contract_lot / self.leverage
        logger.info(
            f"ENTERED {direction.upper()} @ {fill_price:.2f} | "
            f"SL={sl:.2f}  Backstop={backstop:.2f}  TP={tp:.2f}  "
            f"qty={qty}  margin=${margin:.2f}"
        )
        self.notifier.entered(direction, fill_price, sl, backstop, tp, qty, self.mode,
                              margin=margin)
        self._check_capital_warnings()

    # ── SL snap ───────────────────────────────────────────────────────────────

    async def _do_snap(self, direction: str):
        """Move SL to current mid-band (fires once per trade)."""
        if self._last_bb is None:
            return
        _, mid, _    = self._last_bb
        new_sl       = mid
        new_backstop = core_backstop_price(direction, new_sl, sl_pct=0.0, backstop_gap=0.005)
        qty          = self.state["pos_quantity"]

        sl_oid, bs_oid = await self.executor.update_dual_sl(
            direction, qty, new_sl, new_backstop,
            self.state["pos_sl_order_id"],
            self.state["pos_backstop_order_id"],
        )
        self.state["pos_sl"]                = new_sl
        self.state["pos_backstop"]          = new_backstop
        self.state["pos_sl_order_id"]       = sl_oid
        self.state["pos_backstop_order_id"] = bs_oid
        self.state["pos_sl_snapped"]        = True
        # The stop just moved mid-candle — re-snapshot so the wick that triggered
        # the snap cannot immediately "hit" the new level.
        self._stamp_levels()
        logger.info(f"SL snapped to mid: {new_sl:.2f}  backstop: {new_backstop:.2f}")
        self.notifier.sl_snapped(new_sl, new_backstop)

    # ── Close position ────────────────────────────────────────────────────────

    async def _close_position(
        self,
        direction:  str,
        exit_price: float,
        reason:     str,       # "TP" | "SL" | "SL_BACKSTOP"
        exit_ts:    int,
    ):
        """Compute P&L, log trade, notify, route to flip / idle / cooldown."""
        entry = self.state["pos_entry"]
        qty   = self.state["pos_quantity"]

        # SL via stop-limit = maker fee; TP or backstop = taker
        fee_exit = FEE_MAKER if reason == "SL" else FEE_TAKER
        pnl_pct, pnl_usdt = calc_pnl(
            direction, entry, exit_price, self.leverage,
            self.capital, self.sizing_pct,
            fee_entry_rate=FEE_TAKER,
            fee_exit_rate=fee_exit,
        )

        # Funding is a cost of the trade like the taker/maker fees already in
        # pnl_usdt, so it comes out of capital too. This is not just reporting:
        # position size is 25% of capital, and capital compounds — leaving
        # funding uncharged would size every later trade off a figure that is
        # slightly too high, and the error accumulates.
        entry_ts = self.state.get("pos_entry_ts")
        funding_usdt, funding_windows = self._funding_cost(
            direction, qty, entry, entry_ts, exit_ts
        )
        net_usdt = pnl_usdt - funding_usdt
        self.capital += net_usdt

        logger.info(
            f"CLOSED ({reason})  {direction.upper()}  {entry:.2f}→{exit_price:.2f} | "
            f"pnl={pnl_pct:+.2f}%  {pnl_usdt:+.2f} USDT  "
            f"funding={funding_usdt:+.4f} ({funding_windows}w)  "
            f"net={net_usdt:+.2f}  capital={self.capital:.2f}"
        )
        self.notifier.trade_closed(
            direction, entry, exit_price, pnl_pct, pnl_usdt, reason, self.capital, self.mode,
            funding_usdt=funding_usdt, funding_windows=funding_windows, net_usdt=net_usdt,
        )

        try:
            append_trade({
                "symbol":    self.symbol_key,
                "mode":      self.mode,
                "entry_ts":  self.state.get("pos_entry_ts"),
                "exit_ts":   exit_ts,
                "direction": direction,
                "entry":     entry,
                "exit":      exit_price,
                "quantity":  qty,
                "reason":    reason,
                "pnl_pct":   pnl_pct,
                "pnl_usdt":  pnl_usdt,          # net of trading fees, before funding
                "funding_usdt":    round(funding_usdt, 6),
                "funding_windows": funding_windows,
                "net_usdt":        round(net_usdt, 6),   # what actually hit capital
                "capital":   round(self.capital, 4),
            }, self.symbol_key)
        except Exception as e:
            # A trade-log write failure must NEVER abort the close sequence —
            # the state reset and order cancellation below are critical.
            logger.error(f"append_trade failed (trade still closed): {e}")

        # Cancel remaining SL/backstop orders, verify position closed
        await self.executor.close_trade(
            self.state.get("pos_sl_order_id"),
            self.state.get("pos_backstop_order_id"),
        )

        # Reset position fields
        self.state.update({
            "capital":                self.capital,
            "pos_direction":          None,
            "pos_entry":              None,
            "pos_quantity":           0,
            "pos_sl":                 None,
            "pos_backstop":           None,
            "pos_tp":                 None,
            "pos_sl_order_id":        None,
            "pos_backstop_order_id":  None,
            "pos_sl_snapped":         False,
            "pos_snap_at":            None,
            "pos_entry_ts":           None,
            "pos_lvl_candle_ts":      None,
            "pos_lvl_h_at":           None,
            "pos_lvl_l_at":           None,
        })

        self._check_capital_warnings()

        # Route to next phase
        if reason == "TP":
            await self._attempt_flip(direction)
        else:
            # SL (any variant) → per-direction cooldown
            if direction == "long":
                self.state["long_cd"] = COOLDOWN_N
            else:
                self.state["short_cd"] = COOLDOWN_N
            self.state["phase"] = "cooldown"
            logger.info(
                f"Cooldown started — long_cd={self.state['long_cd']}  short_cd={self.state['short_cd']}"
            )

    # ── Flip ──────────────────────────────────────────────────────────────────

    async def _attempt_flip(self, closed_direction: str):
        """
        After TP: try to open the opposite position immediately.
        Uses latest forming candle H/L to check if price is still at the band.
        """
        if self._last_bb is None or self._prev_ema is None or self._prev_close is None:
            self.state["phase"] = "idle"
            return

        upper, _mid, lower = self._last_bb
        flip_dir = "short" if closed_direction == "long" else "long"

        # Use current forming candle H/L to check if price is still at the band
        # (not a stale proxy — _forming_h/l updated on every WebSocket tick)
        check_price = self._forming_h if flip_dir == "short" else self._forming_l
        target_band = upper           if flip_dir == "short" else lower

        if flip_valid(
            closed_direction,
            check_price,
            self._last_bb,
            self._prev_ema,
            self._prev_close,
            self.state["long_cd"],
            self.state["short_cd"],
        ):
            logger.info(f"Flip → {flip_dir.upper()} (price={check_price:.2f}  band={target_band:.2f})")
            entry_price = target_band
            await self._enter_position(flip_dir, entry_price)
        else:
            logger.info(
                f"Flip skipped — {flip_dir.upper()}  price={check_price:.2f}  band={target_band:.2f}"
            )
            self.notifier.flip_skipped(flip_dir, check_price, target_band)
            self.state["phase"] = "idle"

    # ── Live position helpers ─────────────────────────────────────────────────

    async def _live_position_still_open(self) -> bool:
        """Query MEXC for open position. Returns True on API error (safe default)."""
        try:
            positions = await self.ws_exchange.fetch_positions([self.ccxt_symbol])
            return any(abs(float(p.get("contracts") or 0)) > 0 for p in positions)
        except Exception as e:
            logger.error(f"fetch_positions failed: {e}")
            return True

    async def _get_live_exit_info(self, h: float, l: float) -> tuple:
        """
        Determine exit price and reason from filled orders.
        Checks SL orders first (stop-limit then backstop), then TP by H/L proximity.
        Falls back to H/L heuristic if orders are unreadable.
        TP priority maintained: if H/L indicates TP, return TP.
        """
        direction = self.state["pos_direction"]
        sl_price  = self.state["pos_sl"]
        tp_price  = self.state["pos_tp"]

        # Check TP hit first (priority rule)
        if tp_price is not None and check_tp_hit(direction, h, l, tp_price):
            return tp_price, "TP"

        # Check SL orders
        for oid_key, reason in [
            ("pos_sl_order_id",       "SL"),
            ("pos_backstop_order_id", "SL_BACKSTOP"),
        ]:
            oid = self.state.get(oid_key)
            if not oid:
                continue
            try:
                order = await self.ws_exchange.fetch_order(oid, self.ccxt_symbol)
                if order.get("status") in ("closed", "filled"):
                    fill = float(order.get("average") or order.get("price") or 0)
                    if fill > 0:
                        return fill, reason
            except Exception as e:
                logger.warning(f"fetch_order {oid_key} failed: {e}")

        # Fallback: infer from H/L
        logger.warning("Exit not determinable from orders — inferring from H/L")
        if direction == "long":
            return (tp_price, "TP") if (tp_price and h >= tp_price) else (sl_price, "SL")
        return (tp_price, "TP") if (tp_price and l <= tp_price) else (sl_price, "SL")

    # ── Capital warnings ──────────────────────────────────────────────────────

    def _check_capital_warnings(self):
        if self._start_cap <= 0:
            return
        pct_lost = (self._start_cap - self.capital) / self._start_cap * 100
        for threshold in (25, 50, 75):
            if pct_lost >= threshold and threshold not in self._warned:
                self._warned.add(threshold)
                self.notifier.capital_warning(threshold, self.capital)
                logger.warning(f"Capital warning: −{threshold}%  current={self.capital:.2f} USDT")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BB Channel Rider bot")
    parser.add_argument(
        "--symbol", required=True,
        help="Symbol key from config.yaml bb_bot.symbols (e.g. BTC, ETH)"
    )
    args = parser.parse_args()

    cfg        = load_config()
    symbol_cfg = find_symbol_cfg(cfg, args.symbol)
    BBBot(cfg, symbol_cfg).run()
