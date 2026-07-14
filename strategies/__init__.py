"""
strategies/__init__.py
----------------------
Strategy registry.

To add a new strategy:
  1. Create strategies/my_strategy.py with simulate() and indicators() functions
  2. Add one import + one entry below — nothing else needs to change

The backtest dispatcher (backtest/indicator_backtest.py) and the dashboard
call strategies via this registry so they never hardcode strategy names.
"""

from strategies import bb_channel
from strategies import ma_cross
from strategies import price_vs_ma
from strategies import bollinger_bounce
from strategies import rsi_reversal

# Maps strategy_type string (sent by dashboard) to its module.
# "bollinger" and "rsi" match the keys the dashboard already sends.
STRATEGIES: dict = {
    "bb_channel":  bb_channel,
    "ma_cross":    ma_cross,
    "price_vs_ma": price_vs_ma,
    "bollinger":   bollinger_bounce,
    "rsi":         rsi_reversal,
}
