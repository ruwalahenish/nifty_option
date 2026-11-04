# Regime engine (P5)

`nifty_core/regime/engine.py` describes the market at one decision time from the **completed 5-minute candles of the underlying**. It does not predict prices. Nothing it outputs is a probability of profit.

## Three kinds of input, kept apart

| Kind | Where it lives | Examples |
|---|---|---|
| Verified exchange facts | `config/contracts/`, `config/market/` | session open and close (used to check the data starts at the open and ends inside the session) |
| Market data | the provider's candles | open, high, low, close of each 5-minute bar |
| Strategy assumptions and thresholds | `config/regime_thresholds.json` (`kind: STRATEGY_ASSUMPTIONS`) | every number below |

The thresholds were chosen by reasoning. They were **not optimised on any data and are not validated on real markets.** Each result carries a `thresholds_fingerprint`, so a journal entry shows exactly which thresholds produced it.

## Look-ahead rule

`classify(candles, as_of, ...)` keeps only candles with `end <= as_of`. Candles after `as_of` or still forming are ignored and counted in `ignored_future_candles`. It uses no option-chain data, no events and no clock. Tests prove that appending, removing or corrupting later candles changes nothing, and that a candle still forming is not used.

## Outcomes

| Label | Meaning |
|---|---|
| `BULLISH`, `BEARISH` | enough agreeing evidence, none against, and the net move agrees |
| `SIDEWAYS` | narrow range for the day so far and a small net move |
| `HIGH_VOLATILITY` | abnormal movement. It takes precedence over direction and is not a signal to trade |
| `NO_TRADE` | the data is fine but the evidence conflicts or is too weak |
| `NO_DATA` | data is missing, late, gapped, stale, insufficient, malformed or of unverified origin |

## Decision order (first match wins)

1. **Data gate:** any data problem gives `NO_DATA`, with every problem listed.
2. **Volatility gate:** either volatility test below gives `HIGH_VOLATILITY`.
3. **Direction:** bull votes >= `votes_required`, no bear votes, **and the `net_move` item itself votes bull** gives `BULLISH`. The mirror gives `BEARISH`.
4. **Sideways:** day range <= `sideways_max_range_pct` and |net move| < `net_move_pct` gives `SIDEWAYS`.
5. **Otherwise** `NO_TRADE`: `CONFLICTING_EVIDENCE` if both sides have votes, else `INSUFFICIENT_EVIDENCE`.

Why net move must agree: it is the only evidence item with an absolute size. The other three (range position, recent candles, swing structure) are scale-free and can fire on noise in a very narrow range. Early exploration on the fixtures showed single-sample BULLISH and BEARISH flashes on a flat day with a net move near 0%; this rule removed them. It is a reasoned design change, not a threshold tuned to the fixtures.

## Inputs

| Name | Definition |
|---|---|
| day open / high / low | first candle's open, highest high, lowest low, over the completed candles |
| last close | close of the last completed candle |
| net move % | (last close / day open - 1) x 100 |
| range % | (day high - day low) / day open x 100 |
| range position | (last close - day low) / (day high - day low); undefined (neutral) if the range is zero |
| recent up / down | in the last `recent_window` candles: up is close > open, down is close < open, a doji counts as neither |
| block highs / lows | the last `structure_window` candles in three equal consecutive blocks, the high and low of each |
| max bar range % | the largest single-candle (high - low) / open x 100 |
| realised stdev % | population standard deviation of the 5-minute close-to-close % returns |

All arithmetic is exact `Decimal`.

## Evidence items (each votes BULL, BEAR or NEUTRAL)

| Item | BULL | BEAR |
|---|---|---|
| `net_move` | net move % >= +`net_move_pct` | net move % <= -`net_move_pct` |
| `range_position` | >= `range_position_high` | <= `range_position_low` |
| `recent_candles` | up candles >= `recent_dominance` | down candles >= `recent_dominance` |
| `swing_structure` | block highs AND block lows strictly rise (higher highs, higher lows) | both strictly fall |

`evidence_score` = (bull votes - bear votes) / 4. It is a **model summary of how the evidence leans**, not a probability and not a forecast. It never changes a label.

## Volatility tests

| Test | Triggers `HIGH_VOLATILITY` when |
|---|---|
| abnormal bar | max bar range % >= `abnormal_bar_range_pct` |
| realised volatility | realised stdev % >= `high_vol_stdev_pct` |

## Data gate: what gives `NO_DATA`

`NO_CANDLES`, `INSUFFICIENT_HISTORY` (fewer than `min_bars`), `GAP_IN_CANDLES` (a hole anywhere in the day: the engine does not splice, so nothing is classified for the rest of that session), `OUT_OF_ORDER`, `LATE_START` (the first candle is not at the session open), `OUTSIDE_SESSION`, `WRONG_INTERVAL`, `DAY_MISMATCH`, `STALE_DATA` (the last completed candle ended more than `max_data_lag_minutes` before the decision time, or before the close), `MARKET_CLOSED`, `INVALID_CANDLE`, `UNVERIFIED_SOURCE` (outside `fixture` mode the data must be labelled as real, not synthetic and not unlabelled).

## Thresholds (all in `config/regime_thresholds.json`)

| Name | Default | Meaning |
|---|---|---|
| `interval_minutes` | 5 | candle size (only 5 is supported) |
| `min_bars` | 12 | completed candles needed (one hour; the first classification is at 10:15) |
| `max_data_lag_minutes` | 10 | staleness limit, inclusive |
| `net_move_pct` | 0.30 | directional net move, inclusive |
| `range_position_high` / `_low` | 0.65 / 0.35 | position in the day's range, inclusive |
| `recent_window` / `recent_dominance` | 6 / 4 | candles looked at, and how many must agree |
| `structure_window` | 12 | candles in the swing test (three blocks of four) |
| `votes_required` | 3 | agreeing evidence items needed (of 4) |
| `sideways_max_range_pct` | 0.45 | day range limit for SIDEWAYS, inclusive |
| `abnormal_bar_range_pct` | 0.50 | single-bar range limit, inclusive |
| `high_vol_stdev_pct` | 0.10 | realised volatility limit, inclusive |

Validation rejects inconsistent sets (for example `min_bars` below the windows, or a dominance that is not a majority).

## Journal record

`result.to_journal_dict()` gives plain JSON: label, time, reasons (code and text), the four evidence items with observed value, rule and vote, the metrics, any data issues, vote counts, the evidence score, candles used, ignored future candles, the thresholds fingerprint and a note that this is a description, not a prediction.

## Known limitations

- It describes the past hour or day so far, so it lags. On the bearish fixture the market first rose above its open and the engine said BULLISH at 11:50 (four votes) before the day reversed; it said BEARISH from 14:00 only.
- A day-so-far range is small early in any session, so an early SIDEWAYS can precede a trend (the bullish fixture reads SIDEWAYS from 10:15 to 10:40).
- Labels flicker. There is no smoothing or hysteresis: on the bullish fixture the label changed 10 times in 65 samples, often between BULLISH and NO_TRADE when one recent-candles vote turns against it.
- Nothing is classified before `min_bars` (10:15 at the defaults). The strategy's entry window (a separate, strategy-level assumption) was aligned in P6 to open at 10:15, the first minute the regime engine can classify; `check_alignment` raises a config error if the window is ever set earlier than that.
- One data hole ends classification for the day. That is deliberate and conservative.
- It uses the index only: no volume, no VWAP, no events, no option liquidity or implied volatility. A calm index with illiquid options is still SIDEWAYS here; option liquidity is a separate gate.
- Thresholds are untested on real data. Fixture results show the logic works, not that the labels are useful.
