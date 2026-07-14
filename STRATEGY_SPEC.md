# BB Channel Rider — Definitive Strategy Specification
**Locked: 2026-05-10. All parameters and behaviors frozen until re-validated via backtest.**

---

## 1. Instrument & Constants

| Parameter | Value |
|-----------|-------|
| Instrument | BTC/USDT perpetual futures, MEXC |
| Timeframe | 15-minute candles |
| Leverage | 10x |
| Position sizing | 25% of current capital per trade |
| Starting capital (paper) | 1,000 USDT |
| BB period | 20 candles |
| BB std dev multiplier | 3.0 (population variance, not sample) |
| Trend EMA period | 150 candles |
| SL distance from entry | 1.0% |
| SL snap threshold | 95% of mid→band distance |
| Cooldown after SL | 2 candles |
| Cooldown after TP | None (immediate idle or flip) |

---

## 2. Indicators

### Bollinger Bands
- **Mid**: simple moving average of last 20 closes
- **Std dev**: population variance — `sqrt(sum((x − mean)²) / 20)`, NOT sample variance (`/ 19`)
- **Upper**: mid + 3.0 × std dev
- **Lower**: mid − 3.0 × std dev
- First 19 candles return no value (insufficient history)

### EMA-150
- Seeded with SMA of the first 150 closes
- Then: `ema = close × k + prev_ema × (1 − k)` where `k = 2 / 151`
- First 149 candles return no value
- 300 closed candles fetched so EMA has 150 extra candles to converge before the live window

### Candle data
- Bands and EMA are always computed from **closed candles only** — the current forming candle is never included
- This eliminates look-ahead bias and band repainting
- Band values are stable for 15 minutes between candle closes

---

## 3. Entry Signal

Evaluated on each candle close (Option B: backtest) or each WebSocket price tick vs last closed candle bands (Option A: live/paper bot).

### Long signal
- Candle **low** touches or crosses the **lower band** (`low <= lower`)
- AND the previous candle's **close** was **above** the previous candle's **EMA-150** (uptrend confirmation)
- Entry price: **lower band** value at signal candle

### Short signal
- Candle **high** touches or crosses the **upper band** (`high >= upper`)
- AND the previous candle's **close** was **below** the previous candle's **EMA-150** (downtrend confirmation)
- Entry price: **upper band** value at signal candle

### Trend filter detail
- Uses **previous candle's** close and EMA — both fully known before the signal candle opens
- No look-ahead: the signal candle's own EMA is never used for this check

### Blocked entries
- If `long_cd > 0`: long signals are skipped (cooldown active for longs)
- If `short_cd > 0`: short signals are skipped (cooldown active for shorts)
- These are **separate counters** — a long cooldown does not block shorts, and vice versa

---

## 4. Execution Modes

### Option B — Backtest reference
- Detection: candle-close (15m resolution)
- Entry: instant fill at band price on signal candle — no delay
- Orders: simulated (no exchange calls)
- Fee: see Section 7

### Option A — Live / paper bot
- Detection: WebSocket real-time price feed vs last closed candle bands
- Entry: **market order** the moment `current_price` touches the band
- Orders: sent to MEXC (paper mode simulates fills in-process without sending to exchange)
- Fee: see Section 7

---

## 5. Order & Position Setup (on entry fill)

### Contract sizing
```
quantity = floor(capital × 0.25 / (entry_price × 0.0001))
```
- 1 contract = 0.0001 BTC on MEXC perpetuals
- If `quantity = 0`: skip trade, log it, send Telegram notification, stay in idle
- Bot remains active — next valid signal will be re-evaluated

### Stop-loss (dual order, `reduceOnly=True`)
1. **Stop-limit** at `entry × (1 − 0.01)` for long / `entry × (1 + 0.01)` for short — normal path, 0.04% maker fee
2. **Stop-market backstop** at `entry × (1 − 0.015)` for long / `entry × (1 + 0.015)` for short — fires only if price gaps through stop-limit, 0.06% taker fee
- Gap between them: 0.5% of entry price
- When position closes via either order, MEXC auto-cancels the remaining `reduceOnly` order

### Take-profit
- Initial TP = **opposite band** at entry candle: upper band for long, lower band for short
- Order type: **market order** triggered when `current_price` reaches TP level (Option A) or candle H/L check (Option B)

### snap_at (stored at entry, updated each candle)
- Long: `snap_at = current_mid + 0.95 × (current_upper − current_mid)`
- Short: `snap_at = current_mid − 0.95 × (current_mid − current_lower)`
- Recalculated from the latest closed candle's bands every 15m — not fixed at entry

---

## 6. In-Position Management (per candle close)

### Step 1 — Live exit detection (live mode only)
- Query MEXC `fetch_positions` for any open BTC/USDT:USDT position
- If none found: position was closed by TP or SL on exchange
  - Check TP order status first (TP priority), then SL order
  - If both unreadable: infer from last candle H/L (TP wins if `high >= tp`)
- On API error: assume still open (never log a phantom close)

### Step 2 — SL snap (one-time trigger)
- Check: did price cross `snap_at` this candle? (`high >= snap_at` for long, `low <= snap_at` for short)
- If yes and not yet snapped:
  - New SL = current **mid** band
  - Replace both stop-limit and stop-market backstop orders at new level (same 0.5% gap preserved)
  - Mark `sl_snapped = True` — never triggers again on this trade
  - Send Telegram notification

### Step 3 — SL monotone trail (every candle after snap)
- Long: `new_sl = max(current_sl, current_mid)` — SL only ever moves up
- Short: `new_sl = min(current_sl, current_mid)` — SL only ever moves down
- Update both stop-limit and backstop orders together on every candle where mid moves favorably
- No minimum threshold — update fires whenever mid crosses current SL level

### Step 4 — TP update (every candle)
- Recalculate TP as opposite band at current candle: upper for long, lower for short
- Update TP order whenever new value differs from stored value
- No minimum threshold

### Step 5 — TP / SL hit check (paper / backtest only)
- TP checked before SL: if both triggered in same candle, TP wins
  - Assumption: up-move came before the reversal (correct directional assumption for mean-reversion)
  - Live bot: moot — WebSocket detects whichever is hit first in real time

---

## 7. Trade Close & P&L

### Outcome routing
| Reason | Next phase |
|--------|-----------|
| TP hit | Attempt flip (see Section 8), else → idle |
| SL hit (either order) | `long_cd = 2` or `short_cd = 2`, → cooldown |

### P&L formula
```
raw_move = (exit − entry) / entry          # long
raw_move = (entry − exit) / entry          # short

fee_entry   = 0.06% (market order, taker)
fee_exit_tp = 0.06% (market order, taker)
fee_exit_sl = 0.04% (stop-limit, maker) | 0.06% (stop-market backstop, taker)

net_pnl_pct = raw_move × leverage × 100 − (fee_entry + fee_exit) × leverage × 100
pnl_usdt    = (net_pnl_pct / 100) × (capital × 0.25)
capital    += pnl_usdt                     # 100% compounding
```

### Backtest fee (Option B)
- Entry + TP exit: 0.04% maker both sides (limit order assumption in backtest)
- SL exit: 0.04% maker (stop-limit assumption)
- Note: slightly optimistic vs live (no taker fees) — acceptable for backtest reference

---

## 8. Flip on TP

When TP is hit, bot attempts to immediately open the opposite position.

### Sequence
1. TP fires — close current position with **market order** (capital released)
2. Check: is `current_price >= upper_band` (for short flip) or `current_price <= lower_band` (for long flip)?
3. If **yes** and trend filter passes: open opposite position with market order, place dual SL + TP
4. If **no** (price already pulled back from band): go to **idle**, no cooldown

### Skipped flip behavior
- No cooldown applied (no position was opened)
- Bot watches normally in idle
- If price touches the band again on the next candle with trend filter satisfied → fresh valid signal, treated normally

---

## 9. Cooldown

Entered only after **SL hit**. Two independent counters: `long_cd` and `short_cd`.

### Counter behavior (per candle close)
- `long_cd > 0` and last candle **close** is above the lower band (`close > lower`): decrement `long_cd`
- `short_cd > 0` and last candle **close** is below the upper band (`close < upper`): decrement `short_cd`
- Each direction checks only its own relevant band — a long cooldown advances when price recovers above the lower band; a short cooldown when price falls back below the upper band
- Wicks do not count — only the candle close matters
- If BB bands unavailable: decrement unconditionally
- When counter reaches 0: that direction is unblocked, returns to idle evaluation

---

## 10. Telegram Notifications

| Event | Message |
|-------|---------|
| Bot started | Mode, capital |
| Entry placed | Direction, price, SL, capital deployed |
| Entry filled | Direction, fill price, TP |
| Entry expired (not filled) | Direction, price (Option B only) |
| SL snapped | New SL price |
| Trade closed | Direction, entry, exit, reason, P&L% |
| Min contracts skip | Symbol, capital, reason |
| Capital warning thresholds | At −25%, −50%, −75% of starting capital (exact thresholds TBD) |
| Bot error | Error description |

---

## 11. State Persistence

State saved to `data/bb_bot_state.json` after every candle close. Survives restarts.

### Restart behavior
- If phase was `pending_entry` or `in_position`: cancel all open orders, revert to cooldown (`long_cd = short_cd = 2`)
- If was `in_position` (live mode): re-place both SL orders immediately to protect open position
- If position state is incomplete: emergency market close

---

## 12. Behavioral Rules Summary

- **Bands**: closed candles only, never include forming candle
- **Trend filter**: always uses previous candle's close vs previous candle's EMA (no look-ahead)
- **Cooldown**: per-direction, SL only, 2 candles, close must be within bands to count
- **SL snap**: one-time per trade, dynamic threshold (recalculated each candle from current bands)
- **SL trail**: monotone — only ever moves in the profitable direction, no minimum threshold
- **TP priority**: always checked before SL when both are triggered on the same candle
- **Flip**: conditional on price still being at the band at the moment of re-entry
- **Capital**: 100% compounding, 25% sizing per trade
- **Contract floor**: skip and notify if quantity = 0, bot stays running
