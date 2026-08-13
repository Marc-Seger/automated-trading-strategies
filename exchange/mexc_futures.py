"""
mexc_futures.py
---------------
MEXC interface layer for the BB Channel Rider live bot.

Self-contained by design: it holds no shared state and imports nothing from the
strategy layer, so the exchange plumbing can be swapped or mocked without
touching trading logic.

Responsibilities:
  - Market entry orders
  - Dual SL: stop-limit at 1% from entry + stop-market backstop at 1.5% (both reduceOnly)
  - Update/replace both SL orders atomically on every trail step
  - Emergency market close if any order operation fails in live mode
  - get_balance, set_leverage, cancel_all_orders

Paper mode:
  - All methods return plausible values immediately (no exchange calls)
  - Entry fill_price = band price passed in (no slippage assumed)
  - Order IDs are synthetic PAPER-* strings
  - No candle H/L hit-checking here — indicator_bot.py handles that via _on_price_tick

Modes (set via config.yaml  mexc.mode):
  paper — real market data feeds, no orders sent to MEXC
  live  — real orders, real money
"""

import asyncio
import logging
import os
import time
from typing import Optional, Tuple

import ccxt
import ccxt.pro as ccxtpro
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# BTC/USDT perpetual on MEXC — 1 contract = 0.0001 BTC
# CONTRACT_LOT is fetched from load_markets() at startup; this is fallback only.
_DEFAULT_CONTRACT_LOT = 0.0001


class BBExecutor:

    def __init__(self, config: dict, symbol: str = "BTC/USDT:USDT"):
        mexc_cfg        = config.get("mexc", {})
        self.mode       = mexc_cfg.get("mode", "paper")
        self.symbol     = symbol
        self.api_key    = os.getenv("MEXC_API_KEY", "")
        self.api_secret = os.getenv("MEXC_API_SECRET", "")

        # Sync ccxt exchange (REST) — used for order placement in live mode
        self._exchange: Optional[ccxt.mexc] = None

        if self.mode == "paper":
            logger.info("BBExecutor: PAPER mode — orders simulated, no real trades.")
        else:
            logger.warning("BBExecutor: LIVE mode — real money at risk.")

    # ----------------------------------------------------------------
    # Setup
    # ----------------------------------------------------------------

    def connect_sync(self):
        """
        Build the sync REST ccxt.mexc instance.
        Call this once on startup (before the async event loop is running).
        Live mode only; paper mode skips exchange connection.
        """
        if self.mode == "paper":
            return
        self._exchange = ccxt.mexc({
            "apiKey":  self.api_key,
            "secret":  self.api_secret,
            "options": {"defaultType": "swap"},
        })
        self._exchange.fetch_currencies = lambda params={}: {}
        try:
            self._exchange.load_markets()
        except ccxt.BaseError as e:
            logger.warning(f"load_markets failed: {e}")

    # ----------------------------------------------------------------
    # Account
    # ----------------------------------------------------------------

    async def get_balance(self) -> Optional[float]:
        """Returns free USDT balance. Returns None on error or paper mode."""
        if self.mode == "paper":
            return None  # bot tracks its own capital
        try:
            bal  = await asyncio.to_thread(
                self._exchange.fetch_balance, {"type": "swap"}
            )
            usdt = bal.get("USDT", {})
            return float(usdt.get("free") or usdt.get("total") or 0)
        except ccxt.BaseError as e:
            logger.error(f"get_balance failed: {e}")
            return None

    # ----------------------------------------------------------------
    # Leverage
    # ----------------------------------------------------------------

    async def set_leverage(self, leverage: int):
        if self.mode == "paper":
            logger.info(f"[PAPER] Leverage set to {leverage}x")
            return
        try:
            await asyncio.to_thread(self._exchange.set_leverage, leverage, self.symbol)
            logger.info(f"Leverage set to {leverage}x")
        except ccxt.BaseError as e:
            logger.warning(f"set_leverage failed: {e}")

    # ----------------------------------------------------------------
    # Entry — market order (Option A)
    # ----------------------------------------------------------------

    async def place_market_entry(
        self,
        direction: str,
        quantity: int,
        price: Optional[float] = None,
    ) -> Tuple[Optional[float], Optional[str]]:
        """
        Send a market order to open a position.
        Returns (fill_price, order_id). Both None on failure.

        price: the band price that triggered the entry. In paper mode this is
        used as the fill price (instant fill assumption). In live mode it is
        ignored — the actual exchange fill price is returned instead.
        """
        side     = "buy" if direction == "long" else "sell"
        order_id = f"PAPER-ENTRY-{int(time.time()*1000)}"

        if self.mode == "paper":
            logger.info(
                f"[PAPER] MARKET {side.upper()} {quantity} contracts @ {price:.2f}"
                if price else f"[PAPER] MARKET {side.upper()} {quantity} contracts"
            )
            return (price, order_id)

        try:
            qty_str = self._exchange.amount_to_precision(self.symbol, quantity)
            order   = await asyncio.to_thread(
                self._exchange.create_market_order,
                self.symbol, side, float(qty_str),
            )
            fill  = order.get("average") or order.get("price")
            oid   = order["id"]
            logger.info(f"MARKET {side.upper()} {quantity} contracts — fill @ {fill}")
            return (float(fill) if fill else None, oid)
        except ccxt.BaseError as e:
            logger.error(f"place_market_entry failed: {e}")
            return (None, None)

    # ----------------------------------------------------------------
    # Dual SL — stop-limit + stop-market backstop (both reduceOnly)
    # ----------------------------------------------------------------

    async def place_dual_sl(
        self,
        direction: str,
        quantity: int,
        sl_price: float,
        backstop_price: float,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Place two stop orders simultaneously, both reduceOnly:
          1. Stop-limit at sl_price        — normal path, maker fee 0.04%
          2. Stop-market at backstop_price — fires only on gap, taker fee 0.06%

        Returns (sl_order_id, backstop_order_id). sl_order_id None = hard failure.
        On live failure: emergency market close before returning (None, None).
        """
        sl_side = "sell" if direction == "long" else "buy"
        ts      = int(time.time() * 1000)
        sl_oid  = f"PAPER-SL-{ts}"
        bs_oid  = f"PAPER-BS-{ts + 1}"

        if self.mode == "paper":
            logger.info(
                f"[PAPER] Dual SL: stop-limit @ {sl_price:.2f}  "
                f"backstop @ {backstop_price:.2f}"
            )
            return (sl_oid, bs_oid)

        placed_sl_oid = None
        placed_bs_oid = None
        try:
            qty_str    = self._exchange.amount_to_precision(self.symbol, quantity)
            sl_str     = self._exchange.price_to_precision(self.symbol, sl_price)
            bs_str     = self._exchange.price_to_precision(self.symbol, backstop_price)

            # 1. Stop-limit (normal SL path)
            sl_order = await asyncio.to_thread(
                self._exchange.create_order,
                self.symbol, "stop", sl_side, float(qty_str), float(sl_str),
                {"stopPrice": float(sl_str), "reduceOnly": True},
            )
            placed_sl_oid = sl_order["id"]
            logger.info(f"Stop-limit SL placed @ {sl_price:.2f}  ({placed_sl_oid})")

            # 2. Stop-market backstop
            bs_order = await asyncio.to_thread(
                self._exchange.create_order,
                self.symbol, "stop_market", sl_side, float(qty_str), None,
                {"stopPrice": float(bs_str), "reduceOnly": True},
            )
            placed_bs_oid = bs_order["id"]
            logger.info(f"Stop-market backstop placed @ {backstop_price:.2f}  ({placed_bs_oid})")

            return (placed_sl_oid, placed_bs_oid)

        except ccxt.BaseError as e:
            logger.error(f"place_dual_sl failed: {e} — emergency market close")
            # Clean up whatever was placed
            if placed_sl_oid:
                self._cancel_order_sync(placed_sl_oid)
            await self.close_position_market()
            return (None, None)

    async def update_dual_sl(
        self,
        direction: str,
        quantity: int,
        new_sl: float,
        new_backstop: float,
        old_sl_oid: Optional[str],
        old_bs_oid: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Replace both SL orders with new prices.
        Cancel old orders first, then place new ones.
        Called on every SL trail step (snap or monotone trail).
        """
        if self.mode == "paper":
            ts     = int(time.time() * 1000)
            sl_oid = f"PAPER-SL-{ts}"
            bs_oid = f"PAPER-BS-{ts + 1}"
            logger.info(
                f"[PAPER] SL updated: stop-limit @ {new_sl:.2f}  "
                f"backstop @ {new_backstop:.2f}"
            )
            return (sl_oid, bs_oid)

        # Cancel old orders (silently ignore if already gone)
        if old_sl_oid:
            self._cancel_order_sync(old_sl_oid)
        if old_bs_oid:
            self._cancel_order_sync(old_bs_oid)

        return await self.place_dual_sl(direction, quantity, new_sl, new_backstop)

    # ----------------------------------------------------------------
    # Trade close
    # ----------------------------------------------------------------

    async def close_trade(
        self,
        sl_oid: Optional[str],
        bs_oid: Optional[str],
    ):
        """
        Called by indicator_bot after TP or SL fires.
        Cancels whichever of the two SL orders is still open.
        In live mode: also verifies position is closed; market-closes if not.
        Paper mode: no-op (position tracking lives in indicator_bot state).
        """
        if self.mode == "paper":
            return

        # Cancel remaining reduceOnly orders (one already filled, one is still open)
        for oid in (sl_oid, bs_oid):
            if oid:
                self._cancel_order_sync(oid)

        # Verify position actually closed
        try:
            positions = await asyncio.to_thread(
                self._exchange.fetch_positions, [self.symbol]
            )
            for pos in positions:
                if abs(float(pos.get("contracts") or 0)) > 0:
                    logger.warning("close_trade: position still open — market closing")
                    await self.close_position_market()
                    break
        except ccxt.BaseError as e:
            logger.warning(f"close_trade: could not verify position closure: {e}")

    # ----------------------------------------------------------------
    # Emergency
    # ----------------------------------------------------------------

    async def close_position_market(self):
        """Market-close any open position. Paper: no-op (bot tracks state)."""
        if self.mode == "paper":
            logger.info("[PAPER] close_position_market called (no-op)")
            return
        try:
            positions = await asyncio.to_thread(
                self._exchange.fetch_positions, [self.symbol]
            )
            for pos in positions:
                contracts = float(pos.get("contracts") or 0)
                if contracts == 0:
                    continue
                side = "sell" if contracts > 0 else "buy"
                await asyncio.to_thread(
                    self._exchange.create_market_order,
                    self.symbol, side, abs(contracts),
                    {"reduceOnly": True},
                )
                logger.info(f"Position closed (market) — {abs(contracts)} contracts")
        except ccxt.BaseError as e:
            logger.error(f"close_position_market failed: {e}")

    async def cancel_all_orders(self):
        """Cancel all open orders on the symbol. Used on startup/restart recovery."""
        if self.mode == "paper":
            logger.info("[PAPER] cancel_all_orders called (no-op)")
            return
        try:
            await asyncio.to_thread(self._exchange.cancel_all_orders, self.symbol)
            logger.info(f"All open orders cancelled for {self.symbol}")
        except ccxt.BaseError as e:
            logger.error(f"cancel_all_orders failed: {e}")

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _cancel_order_sync(self, order_id: str):
        """Synchronous single-order cancel (used inside async-to-thread calls)."""
        try:
            self._exchange.cancel_order(order_id, self.symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or cancelled — expected on dual-SL cleanup
        except ccxt.BaseError as e:
            logger.warning(f"_cancel_order {order_id} failed: {e}")
