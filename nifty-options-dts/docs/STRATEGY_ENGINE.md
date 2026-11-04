# Strategy engine (P6)

`nifty_core/strategy/engine.py` turns a market regime and an option-chain snapshot into **one trade candidate or NO_TRADE with explicit reasons**. It does not size a position, check the risk limit, approve anything, simulate a fill or place an order. Nothing in the strategy code refers to the risk limits, the cost engine or the 2,500 rupee figure (a test reads the source to enforce this).

## Inputs, kept apart

| Kind | Where it lives |
|---|---|
| Verified exchange facts (lot size, strike interval, tick, expiry rules, session) | `config/contracts/`, `config/market/` via the contract layer |
| Market data (regime candles, option chain, event schedule) | the read-only provider |
| Entry window, flat-by time, quote staleness | `config/config.example.json` (strategy and data-handling keys) |
| Candidate-selection assumptions | `config/strategy_thresholds.json` (`kind: STRATEGY_ASSUMPTIONS`) |
| Risk limits | `config/config.example.json` (risk keys): **not read** by this engine |

## Look-ahead rule

The decision at `as_of` uses the regime from candles that had finished by `as_of`, the latest chain with `snapshot_time <= as_of`, and the day's event schedule (a schedule known in advance is not look-ahead). If a provider hands back a chain or quote stamped after `as_of`, the engine refuses it (`CHAIN_FROM_FUTURE`) instead of using it. Tests remove and corrupt all later data for every fixture at every 5-minute time and require an identical decision.

## Decision order (first failing gate returns NO_TRADE; gates 1 and 2 report all their reasons together)

1. **Session and entry window.** No session gives `MARKET_CLOSED`. A time outside `entry_window_start_ist` to `entry_window_end_ist` (both inclusive) gives `OUTSIDE_ENTRY_WINDOW`. The window must also fit the session and must not open before the regime engine can first classify (`check_alignment` raises `ConfigError` otherwise).
2. **Regime.** Only `BULLISH` and `BEARISH` continue. `SIDEWAYS`, `HIGH_VOLATILITY`, `NO_DATA`, `NO_TRADE` give `REGIME_<label>`, with the regime's own reasons in the text. A regime not computed at the decision time gives `REGIME_NOT_AT_DECISION_TIME`.
3. **Event.** A HIGH-impact scheduled event on the day gives `HIGH_IMPACT_EVENT` (assumption `block_on_high_impact_event`).
4. **Chain gates** (all reported): `CHAIN_UNAVAILABLE`, `CHAIN_FROM_FUTURE`, `CHAIN_STALE` (snapshot older than `data_stale_after_seconds`), `DATA_UNVERIFIED` (synthetic data outside fixture mode), `CHAIN_INVALID` (strike grid, tick, real expiry date, lot size from the contract layer), `CONTRACT_DATA_UNVERIFIED` (the contract layer cannot vouch for the date in this mode), `SPOT_MISMATCH` (chain spot vs the last underlying candle close).
5. **Expiry.** The nearest listed expiry at least `min_days_to_expiry` days away; none gives `NO_ELIGIBLE_EXPIRY`.
6. **Candidates.** BULLISH tests Bull Call Debit Spreads (buy CE at L, sell CE at L+width). BEARISH tests Bear Put Debit Spreads (buy PE at L, sell PE at L-width). L is the strike nearest to spot (round half up on the contract layer's strike interval) moved `long_strike_offsets_steps` strikes out of the money. Every (offset, width) pair is tested. Entry prices are the **ask of the long leg and the bid of the short leg**.
7. **Per-candidate filters** (all rejections recorded, nothing repaired):
   - quotes present, both legs two-sided with bid and ask above zero (`MISSING_QUOTE`, `NO_BID`, `NO_ASK`, `ONE_SIDED_QUOTE`)
   - quote fresh (`STALE_DATA`, same limit as the chain)
   - bid-ask spread at most `max_leg_spread_pct` of mid (`WIDE_SPREAD`)
   - volume and open interest at least the minimums; missing is a rejection (`LOW_VOLUME`, `MISSING_VOLUME`, `LOW_OPEN_INTEREST`, `MISSING_OPEN_INTEREST`)
   - quoted size on the side we would trade (long: ask size, short: bid size) at least `min_depth_lots` x lot size, with the lot size from the contract layer (`THIN_DEPTH`, `MISSING_DEPTH`)
   - a structurally valid debit spread from the P4 model (`SPREAD_INVALID_<code>`: credit, same strike, wrong order, mixed expiry, naked short and so on)
   - net debit at most `max_debit_to_width` x width (`DEBIT_TOO_HIGH_VS_WIDTH`), a structure sanity filter, not a profit estimate
8. **Ranking among survivors.** Lowest *entry friction* (both legs' quoted bid-ask spread in points divided by the net debit, as a percentage), then narrower width, then nearer the money, then lower long strike. The ranking uses data quality only: no forecast, no profit estimate, no risk figure, no stop-loss.
9. **No survivor gives `NO_SUITABLE_CANDIDATE`**, with a count of rejection codes in the text and every tested candidate listed with its own reasons. A valid spread is never chosen merely because it exists.

## The candidate

`TradeCandidate` holds: strategy type (`BULL_CALL_DEBIT` or `BEAR_PUT_DEBIT`), regime at entry (label, evidence score, votes, thresholds fingerprint), decision time, chain snapshot time, data label and mode, expiry, long and short contracts, the entry prices used, width, net debit in points and rupees, `lots` and `lot_size`, liquidity evidence per leg (bid, ask, spread points and percent, sizes, volume, open interest, quote time, chain age), the selection record (rule, friction, debit/width, candidates tested and passed), the full assumptions used (strategy thresholds with fingerprint, entry window, flat-by, staleness limit, regime fingerprint, entry price convention), and the validated `DebitSpread` object for the risk engine. `lots` is the unit used to evaluate the candidate (assumption `candidate_lots`), **not a position size**. Net debit in rupees is information, not a verdict. `decision_to_journal` gives plain JSON, including every rejected candidate.

## Assumptions (`config/strategy_thresholds.json`)

| Name | Default | Meaning |
|---|---|---|
| `min_days_to_expiry` | 2 | smallest distance to expiry, in calendar days (inclusive) |
| `long_strike_offsets_steps` | [0, 1] | strikes out of the money from the nearest strike to spot |
| `width_points_options` | [100, 150] | spread widths tried, in index points |
| `candidate_lots` | 1 | evaluation unit, not sizing |
| `max_leg_spread_pct` | 3.0 | widest bid-ask spread per leg, % of mid (inclusive) |
| `min_leg_volume` | 5000 | smallest volume per leg (inclusive) |
| `min_leg_open_interest` | 20000 | smallest open interest per leg (inclusive) |
| `min_depth_lots` | 1 | quoted size needed on the traded side, in lots |
| `max_debit_to_width` | 0.65 | largest net debit as a fraction of the width (inclusive) |
| `max_spot_divergence_pct` | 0.25 | largest gap between chain spot and the last candle close (inclusive) |
| `block_on_high_impact_event` | true | no entry on a day with a HIGH-impact scheduled event |

From `config.example.json`: entry window **10:15 to 14:30 IST**, flat-by 15:15 IST (strategy assumptions, not exchange rules, configurable), and `data_stale_after_seconds` 60 (one staleness limit for both chain and quotes). The market-observation period runs from the session open; the first possible entry is the first minute the regime engine can classify (12 completed candles).

All values were chosen by reasoning. They are **not validated on real data and were not tuned on the synthetic fixtures.** Changing any value changes the `fingerprint` recorded in each candidate and decision.

## Known limitations

- **Sparse fixture snapshots.** The fixtures hold 4 chain snapshots a day (09:45, 10:30, 12:30, 14:30) and the staleness limit is 60 s, so a candidate can appear only at those minutes. On the bullish fixture 40 of the 46 in-window samples that would otherwise reach the chain gate are `CHAIN_STALE`. That is the rule working as designed, and a limit of the fixtures, not evidence about real markets. Real data needs frequent snapshots.
- **Lag inherited from the regime engine.** A candidate is only as good as the regime label; that label lags, flickers and is unvalidated.
- **Liquidity minimums are in fixture units.** Real NIFTY volume, open interest and depth figures differ; these defaults need calibrating against recorded real chains before they mean anything.
- **One chain per decision.** Only the nearest eligible expiry is examined; there is no search across expiries.
- **Friction ranking is a data-quality ranking.** Because friction is spread cost divided by net debit, a wider spread has a larger debit and so lower friction: the ranking tends to favour the widest allowed width. It does not estimate which spread is more likely to profit.
- **No exit logic.** Stop-loss, targets and the 15:15 flat-by exit belong to later phases.
- **No affordability test.** Many candidates will cost more than the 2,500 rupee Total Defined Risk gate (the bullish fixture candidate has a net debit of about 3,403 rupees before costs). That is the risk engine's decision in P7, and the system must be free to answer BLOCKED.
