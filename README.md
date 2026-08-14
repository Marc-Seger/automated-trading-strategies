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

**A backtest dashboard** (Flask + a single-page UI) to run any strategy against real MEXC OHLCV history, tune parameters, and inspect trade-by-trade results, plus a live/paper bot status view. It runs on a cloud VPS at **[bot.marcseger.dev](https://bot.marcseger.dev)** — the live bot's current position, trade history and the full methodology docs are all there.

**A validation pipeline**, applied so far to the one strategy that runs live.

---

## BB Channel Rider: validated results

Frozen spec (2026-05-10), BTC/USDT perpetual futures on MEXC, 15-minute candles, 10x leverage, 25% position sizing, entry on a band touch confirmed by the EMA-150 trend, full spec in [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md).

Backtest over a full year of real 15-minute data (2025-08 → 2026-08), 25% sizing, compounding, charging the fees the bot actually pays:

- **311 trades, 60.5% win rate, +$730.45 on a $1,000 start (+73%), 20.1% max drawdown**
- **Out-of-sample** (only data after the 2026-05-10 freeze, which the parameters never saw): **66 trades, 65.2% win rate, +$359.73, 10.1% max drawdown**
- **Live paper bot**, running since 2026-08-04 on a $1,000 paper balance and currently in profit. Deliberately not quoted precisely here: it closes trades every few days, so any figure written into a README is stale within the week. The dashboard has the live numbers. The count is still far too small to mean anything either way — that is the point of leaving it running.

**Fees dominate.** The same year with fees switched off returns +$3,078, so roughly three quarters of the gross edge goes to the exchange. At 10x a round trip costs ~1.2% of margin, which means a take-profit on a small move can still close at a net loss.

**What these numbers still don't capture.** Paper mode uses real prices and charges real fees, but assumes zero slippage and instant fills, so live trading would run somewhat below this. The backtest also can't tell a stop-limit fill from the market backstop, so stop-loss exits are costed at the cheaper maker rate. Both push the figures optimistic.

- **Gate 1** (`research/gate1_audit.py`): calls `strategies/bb_channel.simulate` with no parameter overrides, so it can only ever measure the frozen constants the live bot imports from that same module.
- **Gate 2** (`research/gate2_grid_search.py`): a 16,200-combination grid search, 67.5% of combinations profitable — the strategy family isn't balanced on a knife edge. Note the frozen set ranks 14th of 16,200 *on the window it was selected from*, so that ranking is a selection, not independent evidence. The out-of-sample figure above is the honest test.

> **Superseded numbers.** This section previously read *290 trades, 63.8% win rate, +$10,348*. That came from a standalone audit script that had drifted from the deployed strategy on five counts, each one flattering: SL snap 85% vs 95%, EMA-200 vs EMA-150, 100% of capital per trade vs 25%, maker fees on both legs when entry and take-profit are market orders, and a win counted as a take-profit exit rather than a net gain. It measured a configuration that was never deployed. Corrected 2026-08-12; the old script is deleted and Gate 1 now imports the live strategy module directly.

The other four strategies share the same backtest engine and dashboard but haven't been through this level of validation, they're there to build and test, not to trust blindly.

---

## How it works

```
strategies/       one file per strategy, pure functions (candles in, trades out)
backtest/         the engine that runs any strategy over historical OHLCV
bots/bb_bot.py    the live/paper trading bot (BB Channel Rider only, for now)
exchange/         MEXC exchange interface (ccxt)
research/         Gate 1 audit + Gate 2 grid search scripts
dashboard/        Flask app + UI for backtesting and live monitoring
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
