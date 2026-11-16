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

## 7. Live paper trading: the `adaptive` profile

The selector now decides live. `python -m nifty_live.live_run` defaults to `--profile adaptive` (`config/profiles/adaptive/`),
and `run-day --profile adaptive` replays it on a recorded or fixture day. The opening-range breakout is still available as
`--profile intraday` (see [INTRADAY_PLAYBOOK.md](INTRADAY_PLAYBOOK.md)); it is no longer the default and no longer decides entries
in the adaptive profile.

```
data -> selector (4 strategies or NO TRADE) -> same pick at N slots -> risk gate -> ticket -> paper entry -> management -> journal
```

- **One decision per 5-minute slot**, on a final candle (see the candle guard in `nifty_live/candle_guard.py`).
- **Confirmation.** A pick is only offered to the risk gate once it has been the pick at `entry_confirmations` consecutive slots
  (2 by default, in `config/profiles/adaptive/selector_thresholds.json`; 1 enters on the first pick). A pick that flickers on and off
  never trades. A slot skipped for want of a final candle breaks the run.
- **Every strategy goes through the same gates**: the per-trade risk limit (INR 2,500), the daily loss limit, 2 trades a day, one open
  position at a time, no averaging, the kill file, fresh two-sided liquid quotes, and flat by 15:15. The risk gate re-derives the
  worst case itself, from the structure's legs, and refuses a structure whose shorts are not each covered by a long wing.
- **Multi-leg paper execution.** A condor or butterfly is filled as four legs together or not at all (no single-leg positions), each at
  the adverse side of its quote plus slippage; the fills are re-checked against the approved worst case at the moment of the fill.
  Costs are charged leg by leg. The exit closes all legs together the other way.
- **Exits are fixed before entry.** Every candidate carries its target, its stop and the price level that invalidates it, and the
  journal records them with the entry.

| Structure | Profit target | Stop | Exits early when a 5-minute candle closes ... |
|---|---|---|---|
| bull call / bear put debit spread | +60% of the defined risk (net) | -50% of the defined risk (net) | back through the day average |
| bull put credit spread | 50% of the credit captured (gross) | the credit lost, but at most 60% of the worst case (gross) | below the short put / support |
| bear call credit spread | same | same | above the short call / resistance |
| iron condor / iron butterfly | same | same | outside the day's range it was sold inside, or within one strike interval of a short strike |

All of these also close on abnormal volatility, an opposite regime (directional trades), three bad-data checks and the flat-by time.

- **Margin is not modelled.** A credit structure's "capital committed" is its worst-case loss plus costs, a stand-in for the margin a
  broker would block.
- **Not validated.** The thresholds are still guesses and the strategies have not been shown to make money. The two recorded real days
  are far too few to say anything about profit. Judge it by net P&L per trade after
  costs over many sessions, and keep the 30-trade rule in mind.

## 8. Trade readiness: why it waited, what it waits for, where it would enter

The Live page and the journal explain every adaptive decision (`nifty_core/selector/readiness.py`). The dashboard only renders what
the backend computed from the selector's own conditions and thresholds; it holds no trading logic.

**Status** (one per decision): WAITING FOR SIGNAL, WAITING FOR CONFIRMATION, READY FOR RISK VALIDATION, READY TO EXECUTE, NO VALID
STRATEGY, TRADE EXECUTED, EXECUTION BLOCKED. READY TO EXECUTE is only set after the risk gate has approved (in manual mode, while the
ticket waits for your yes). A technical, safety or risk problem is EXECUTION BLOCKED; it is never shown as a market condition.

**Checklist** (for the selected strategy, or else the one nearest to qualifying), in the strategy's own words:

| Status | Meaning |
|---|---|
| PASSED | checked now and holds |
| FAILED | checked now and does not hold; re-checked at every 5-minute slot |
| PENDING | not reached yet: the option structure, the confirmation count and the risk gate are only checked once the conditions above pass |
| ERROR | a technical problem (stale data, no usable expiry), not a statement about the market |
| N/A | cannot apply: the NIFTY index has no traded volume, so there is no volume confirmation (option volume and open interest are checked under liquidity) |
| NOT CONFIRMED | an optional extra that does not hold; it lowers confidence and never blocks a trade |

**Expected entry.** Shown only when a real, quoted, liquid structure exists at the decision time: the underlying price, the strikes,
CE/PE, each premium, the net credit or debit, the worst case against the limit, reward-to-risk, the stop and the target, and when it was
priced. Otherwise it reads **ENTRY NOT AVAILABLE** with the reason, plus the price bands the conditions are measured against (for
example the mid-range band for an iron condor, computed from the thresholds). The listed entry conditions are only those the live logic
enforces; the selector's narrative also mentions an "entry candle" rule and a "spot within 0.15%" rule, which the live pipeline does not
check, so they are not shown as requirements. No time or price is promised: execution depends on live conditions and the final
validations.

**Even when something else blocks trading** (an unusable expiry, stale data, a scheduled event, the kill file) the four strategies are
still read, so the market-side conditions stay visible next to the technical reason.

**Decision history** (the Live page, `decision_history` in the state file and the API): one row per decision with the time, regime,
strategy evaluated, expected entry, NIFTY price, what held and what did not, why not, whether that strategy **later became valid**, and
whether a paper trade was entered. It is rebuilt from the journal when a session restarts mid-day.

**API** (GET only, same password as the pages): `/api/trade-readiness` (the latest decision's readiness, the legend and the next slot) and
`/api/decision-history` (the day's rows), both JSON.

**Expiry.** `live_run --expiry auto` records one expiry for the whole session. It now skips an expiry closer than the strategy's
minimum (2 days): on a Monday the current weekly expiry is Tuesday, one day away, and a session recorded for it could not trade all day
("no listed expiry is at least 2 days away"). An explicit `--expiry` is never changed.

## What this does not do

- Expiry-payoff figures (max profit / loss, breakevens) describe the shape; the trade is closed the same day, so real P&L depends
  on the exit prices.
- The index has no volume, so there is no volume confirmation. There is no implied-volatility model: "rich" and "cheap" premium are
  inferred from realised movement.
- The thresholds are guesses until measured. On the six synthetic fixtures the selector picks a debit spread, an iron condor or
  NO TRADE; the credit spread and butterfly are exercised with constructed candle paths in the tests. None of this shows a profit. Credit structures are now traded by the adaptive live profile; the same caveat applies.
