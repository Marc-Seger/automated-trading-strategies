# Automated Trading Strategies

A framework for building, backtesting, and validating systematic, rules-based BTC trading strategies, plus one strategy (BB Channel Rider) that's been through full validation and runs live in paper mode.

This is not a signal-following bot. Every entry and exit here comes from indicator logic on OHLCV data: no external calls, no discretionary input, no black box.

---

## What's here

**Five strategies**, all built on the same shared indicator library and backtest engine:

| Strategy | Idea |
|---|---|
| **BB Channel Rider** | Bollinger Band mean-reversion with an EMA trend filter, the validated one (see below) |
| MA Cross | Fast/slow moving average crossover |
| Price vs MA | Price crossing a single moving average |
| Bollinger Bounce | Band touch + close back inside (reversal) |
| RSI Reversal | RSI exiting an oversold/overbought zone |

**A backtest dashboard** (Flask + a single-page UI) to run any strategy against real MEXC OHLCV history, tune parameters, and inspect trade-by-trade results, plus a live/paper bot status view.

**A validation pipeline** for the strategy that's actually meant to run with money on the line.

---

## BB Channel Rider: validated results

Frozen spec (2026-05-10), BTC/USDT perpetual futures on MEXC, 15-minute candles, 10x leverage, 25% position sizing, entry on a band touch confirmed by the EMA-150 trend, full spec in [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md).

- **290 trades, 63.8% win rate, +$10,348 net on a $1,000 starting balance over 11 months** (paper mode, compounding)
- **Gate 1** (`research/audit_bb_channel.py`): a line-by-line audit of all 290 trades against the spec, passed
- **Gate 2** (`research/gate2_grid_search.py`): a 16,200-combination parameter grid search, 67.5% of combinations were profitable, so the result isn't a cherry-picked lucky parameter set

The other four strategies share the same backtest engine and dashboard but haven't been through this level of validation, they're there to build and test, not to trust blindly.

---

## How it works

```
strategies/          one file per strategy, pure functions (candles in, trades out)
backtest/            the engine that runs any strategy over historical OHLCV
bots/bb_bot.py        the live/paper trading bot (BB Channel Rider only, for now)
exchange/             MEXC exchange interface (ccxt)
research/             Gate 1 audit + Gate 2 grid search scripts
dashboard/            Flask app + UI for backtesting and live monitoring
```

Strategies are self-contained: no exchange calls, no file I/O, just data in and decisions out. That's what makes the backtest and the live bot guaranteed to agree, they run the exact same code.

---

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in MEXC API keys (paper/testnet is fine)
python3 dashboard/server.py
```

Then open `http://localhost:5050`.

To run the bot itself:
```bash
python3 bots/bb_bot.py --symbol BTC
```

---

## Disclaimer

This runs in paper mode by default. Nothing here is financial advice, past backtest performance isn't a guarantee of future results, and you're responsible for anything you do with real money.
