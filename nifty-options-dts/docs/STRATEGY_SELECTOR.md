# Strategy selector: one of four intraday strategies, or NO TRADE

PAPER only. Not financial advice. The selector **proposes**; it enters nothing and simulates no fill. Every threshold is our
own choice in `config/selector_thresholds.json`, not validated on market data. Run it with:

```
python -m nifty_core.cli select-strategy --scenario bullish_trend --at 12:30 --profile intraday [--recheck-at HH:MM] [--position-open] [--json] [--brief]
```

## What it does

The strategy is not fixed in advance. At a decision time it (1) reads the market from completed 5-minute candles, (2) tests
each of the four strategies against its own **required** conditions, (3) scores the ones that pass, and (4) returns the best
fit, or **NO TRADE**. It never picks a strategy just to produce a trade. All four are defined-risk, intraday, one open at a
time, flat by `flat_by_ist` (15:15 by default), and never held overnight.

## 1. Reading the market (`selector/analysis.py`)

| Measure | How | Used for |
|---|---|---|
| Net move | last close / day open - 1 | trend direction and size |
| Efficiency | net move / total path (1 = straight line, near 0 = chop) | trending vs ranging |
| Volatility | stdev of 5-minute % moves: LOW <= 0.055, HIGH >= 0.12; *expanding* if the last 6 candles are 1.5x the day | which premium to buy or sell |
| Momentum | % change of the close over 4 candles: UP/DOWN >= 0.10%, FLAT <= 0.05% | trend confirmation |
| Support / resistance | opening range, day high and low, swing pivots; nearest below / above spot | strike placement, room to run |
| Structure | TRENDING_BULLISH / TRENDING_BEARISH / RANGE_BOUND / UNCLEAR | which family of strategy can apply |

An `UNCLEAR` day (neither trending nor ranging) supports no strategy.

## 2. The four strategies

Every required condition must pass. Each also needs a real, quoted structure that passes the liquidity rules of the strategy
engine (two-sided, bid-ask <= 5%, volume, open interest, depth), a reward-to-risk floor, and a worst case (loss + entry and
exit costs + slippage) within the per-trade risk limit (INR 2,500 by default).

| Strategy | Required conditions | Structure and strikes |
|---|---|---|
| **Directional Debit Spread** | trending; momentum aligned; volatility LOW/NORMAL (cheap premium); room to the next opposing level >= 0.15% (pressing the day's extreme with momentum counts as open air); entry by 14:00 | bull call / bear put from the strategy engine; reward-to-risk >= 1.0 |
| **Directional Credit Spread** | trending; momentum not against; volatility NORMAL/HIGH (premium worth selling) and not expanding; entry by 13:30 | bull put / bear call; short strike >= 0.25% out and beyond support / resistance + 0.05%; credit >= 25% of width |
| **Iron Condor** | range-bound; volatility not HIGH and not expanding; momentum flat-ish; price in the middle 25-75% of the day range; entry by 13:30 | short strikes beyond the day's high / low + 0.05% and >= 0.35% out; credit >= 20% of width |
| **Iron Butterfly** | range-bound and tight (day range <= 0.40%); efficiency <= 0.08; volatility LOW; flat momentum; spot within 0.10% of the day average; entry by 13:30 | short straddle at the nearest strike; credit >= 40% of width |

Choosing between debit and credit: both need a trend. Cheap premium and strong momentum favour the **debit** spread; richer
premium with a steadier trend favours the **credit** spread, whose short strike sits beyond a support or resistance level.

### Confidence

`confidence = 40 + 60 x (weight of confirmations that agree / total weight)`, at most 100. Confirmations are extras (a strong
trend, low volatility, centred price, a credit well above its floor, tight option spreads). It counts agreeing independent checks.
It is **not** a probability of profit. `min_confidence` (65) is the floor; the highest confidence wins, ties go to the smaller
worst case.

## 3. NO TRADE

NO TRADE is the answer, with reasons for every strategy, when: no strategy passes all its required conditions; the best one is
under `min_confidence`; the market is closed or outside the entry window; data is missing, late, stale or unverified; the
regime engine reports abnormal volatility; a HIGH-impact event is scheduled; a trade is already open (`--position-open`); a
`trader/KILL` file exists; or no structure fits liquidity, reward-to-risk and the risk limit. Fewer than 6 completed candles means
the market cannot be read yet.

## 4. Output

Market Condition, Selected Strategy, Why This Strategy (including why each other one was not chosen), Trade Trigger (numbered
conditions to hold at entry), Trade Setup (legs, prices, net credit or debit, max profit, max loss, worst case with costs,
risk-reward, breakevens), Exit Rules (target, stop-loss, time exit, early exits) and Confidence. `--json` gives the same plus the
full pass/fail table of every condition.

| Family | Target | Stop |
|---|---|---|
| debit spread | +60% of the debit | -50% of the debit |
| credit structures | buy back at 50% of the credit captured | loss of 100% of the credit, but never more than 60% of the worst case |

Early exits: a 5-minute close through the structure's key level (day average for debit spreads, short strike or support /
resistance for credit spreads, the day's range for condors and butterflies), abnormal volatility or data loss, three bad data
checks, a HIGH-impact event, a KILL file.

## 5. Re-evaluation before entry

`--recheck-at HH:MM` re-runs the whole decision at the entry time. The result is `CONFIRMED` only if the same strategy and
structure are still the best fit and spot has moved at most 0.15%. Otherwise it is `RE_EVALUATED` (the original strikes are
dropped and the new decision is shown) or `CANCELLED` (now NO TRADE).

## 6. Credit structures and the existing coverage rule

The V1 debit-spread model treats a credit spread's short leg as uncovered, so credit structures live in
`selector/structures.py` with their own rule: every short call needs a long call at a **higher** strike and every short put a
long put at a **lower** strike, same expiry, at least the same quantity, equal wings, and a net credit below the width. The
loss is then capped at width - credit. Nothing else changes: the debit-spread model, risk engine and paper execution are as before.

## What this does not do

- It is **not wired into `run-day` or the live paper runner**. The risk engine, paper execution and position manager still only
  handle debit spreads; credit structures would need their own entry, mark-to-market and exit handling there. Today the
  selector is a decision and explanation tool.
- Expiry-payoff figures (max profit / loss, breakevens) describe the shape; the trade is closed the same day, so real P&L depends
  on the exit prices.
- The index has no volume, so there is no volume confirmation. There is no implied-volatility model: "rich" and "cheap" premium are
  inferred from realised movement.
- The thresholds are guesses until measured. On the six synthetic fixtures the selector picks a debit spread, an iron condor or
  NO TRADE; the credit spread and butterfly are exercised with constructed candle paths in the tests. None of this shows a profit.
