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

**No demonstrated edge, net of fees.** These numbers replace an earlier "the edge is real" figure that turned out to be wrong for a second, independent reason — see below.

### The number that actually counts

Performance on data the parameters never saw. The grid search that chose them ran to 2026-05-09 and the spec was frozen the next day, so everything after is a genuine out-of-sample test:

> **116 trades · 57.8% win rate · +$5.62 · 18.1% max drawdown** — 2026-05-10 → 2026-08-28

Essentially breakeven. There is a real gross signal in this window (fees off, the same trades return +$396), but at 10x leverage the ~1.2%-of-margin cost of a round trip consumes nearly all of it.

### The full-year backtest

2025-09 → 2026-08, 25% sizing, compounding, charging the fees the bot actually pays:

> **437 trades · 53.8% win rate · -$683.90 on a $1,000 start (-68%) · 72.8% max drawdown**

**Read this one as the pessimistic figure, for the opposite reason the old one was optimistic.** Its window overlaps the parameter search by roughly nine months, so it's exactly as flattered-by-its-own-selection-window as the number it replaces was, just in the direction of a strategy that lost on the data used to pick it. The out-of-sample number above is the one that isn't affected either way.

**Fees are not the whole story here.** The same year with fees switched off returns only +$70, barely positive — the gross edge over the full period is thin to begin with, unlike the out-of-sample window above where a real gross signal exists and fees are what erase it.

**What these numbers still don't capture.** Paper mode uses real prices and charges real fees, but assumes zero slippage and instant fills, so live trading would run somewhat below this. The backtest also can't tell a stop-limit fill from the market backstop, so stop-loss exits are costed at the cheaper maker rate. Both push the figures optimistic, on top of everything above.

### Why the number changed a second time

The first correction (2026-08-12) fixed an audit script that had drifted from the deployed bot on five counts. It produced 311 trades, 60.5%, +$730.45, and was published as the honest number. It was still wrong.

Two bugs in `strategies/bb_channel.py`'s `simulate()` itself, both making the backtest friendlier than the live bot could ever be:

1. **Look-ahead in the band.** Each candle's wick was tested against a Bollinger Band computed from that same candle's own close, a level not knowable at the moment it would have to act on it. The live bot only ever uses the last *closed* candle's band. Fixed 2026-08-16 (`signal_basis`/`fill_mode`, both now default to what the bot actually does).
2. **A stop-loss threshold frozen at entry.** `STRATEGY_SPEC.md` says the SL-snap threshold recalculates from the latest closed candle's bands every 15 minutes. The backtest computed it once at entry and never touched it again. Fixed 2026-08-28.

Gate 1 already imports the live strategy module directly (the fix from the first correction), so this wasn't a third instance of audit-script drift — it was the simulation logic itself.

- **Gate 1** (`research/gate1_audit.py`): calls `strategies/bb_channel.simulate` with no parameter overrides, so it can only ever measure the frozen constants the live bot imports from that same module.
- **Gate 2** (`research/gate2_grid_search.py`): a 16,200-combination grid search under the *old* evaluation, 67.5% of combinations profitable, this configuration ranking 14th on the window it was selected from. Not re-run under the corrected evaluation yet — don't cite it as current evidence until it has been.

### Live

Running since 2026-08-04 on a $1,000 paper balance, 27 closed trades as of 2026-08-28, net -$18.94, currently flat. Still far too small a sample to mean anything on its own, but directionally consistent with the out-of-sample figure above, both landing close to breakeven over roughly the same period. The dashboard has current numbers; a public JSON endpoint (`/api/bb_bot_status`) backs the portfolio page's live figure too.

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

## Two corrections

This README previously reported *290 trades, 63.8% win rate, +$10,348*. Those figures came from an audit script that had drifted from the deployed strategy on five separate counts, every one of them flattering — it was measuring a configuration that had never actually run. The five: stop-loss snap threshold 85% vs the deployed 95%, EMA-200 vs EMA-150, 100% of capital per trade vs 25%, maker fees assumed on both legs when entry and take-profit are actually market orders, and a "win" counted as a take-profit exit rather than a net gain.

Corrected 2026-08-12 to *311 trades, 60.5% win rate, +$730.45*. Gate 1 now imports the live strategy module directly, so the audit and the bot cannot diverge again on those counts. That correction was itself still wrong — the simulation it called had its own look-ahead bug (a candle's wick tested against a band computed from that same candle's own close) and an SL-snap threshold frozen at entry instead of recalculated every candle like the live bot and `STRATEGY_SPEC.md` both require. Corrected again 2026-08-28 to the figures above: no demonstrated edge net of fees over the full period, roughly breakeven out-of-sample. See "Why the number changed a second time" above for the full diagnosis.

---

## Disclaimer

This runs in paper mode by default. Nothing here is financial advice, past backtest performance isn't a guarantee of future results, and you're responsible for anything you do with real money.
