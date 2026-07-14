"""
backtest/indicator_backtest.py
-------------------------------
Thin dispatcher for indicator strategy backtests.

All strategy logic lives in strategies/<name>.py.
This module:
  - Routes simulate_indicator_trades() calls to the right strategy module
  - Re-exports indicator computations (compute_ma, compute_bollinger,
    compute_rsi) so dashboard/server.py can import them from one place

Adding a new strategy: add it to strategies/__init__.py — nothing here changes.
"""

from __future__ import annotations

# Re-exports for dashboard/server.py chart rendering (backward-compatible)
from strategies.indicators import compute_ma, compute_bollinger, compute_rsi  # noqa: F401
from strategies import STRATEGIES


def simulate_indicator_trades(
    candles:          list,
    strategy:         dict,
    sl_pct:           float = 0.05,
    tp_pct:           float = 0.10,
    leverage:         int   = 1,
    fee_rate:         float = 0.0,
    starting_capital: float = 1000.0,
) -> list[dict]:
    """
    Run a strategy backtest over OHLCV candles.

    strategy dict must contain "strategy_type" matching a key in STRATEGIES.
    All other keys are strategy-specific parameters passed through as-is.

    Returns a list of trade dicts (same shape across all strategies).
    """
    stype = strategy.get("strategy_type", "ma_cross")
    mod   = STRATEGIES.get(stype)
    if mod is None:
        return []

    return mod.simulate(
        candles,
        strategy,
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        leverage=leverage,
        fee_rate=fee_rate,
        starting_capital=starting_capital,
    )


def normalise_mexc_symbol(symbol: str) -> str:
    """Add /USDT suffix if symbol has no quote currency (e.g. 'BTC' -> 'BTC/USDT')."""
    if "/" not in symbol:
        return f"{symbol}/USDT"
    return symbol


def get_indicators(candles: list, strategy: dict) -> dict:
    """
    Return indicator series for the given strategy (used by dashboard for
    chart overlay lines).  Returns empty dict for unknown strategy types.
    """
    stype = strategy.get("strategy_type", "ma_cross")
    mod   = STRATEGIES.get(stype)
    if mod is None or not hasattr(mod, "indicators"):
        return {}
    return mod.indicators(candles, strategy)
