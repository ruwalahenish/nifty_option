# Data model (P3)

## Three kinds of setting, kept apart

| Kind | Where | Examples |
|---|---|---|
| Verified exchange facts | `config/contracts/`, `config/market/` | lot size by expiry, expiry weekday, tick size, strike interval, session hours, holidays |
| Broker and statutory cost rules | `config/costs/` | STT, exchange charge, SEBI fee, stamp duty, GST, Zerodha brokerage |
| Our strategy and risk choices | `config/config.example.json` (grouped by `KEY_GROUPS` in `nifty_core/config.py`) | entry window, flat-by time, slippage, risk limits |

The entry window (10:15 to 14:30) and flat-by time (15:15) are **strategy assumptions, not exchange requirements**. The exchange only fixes the session (09:15 to 15:40 from 2026-08-03). `check_config_fits_session` checks that our choices fit inside the real session of a given date. A test fails if an exchange or cost fact appears in the config file, or a strategy choice in the data files.

## Models (`nifty_core/data/models.py`)

All immutable. Prices are `Decimal` (binary floats are rejected). Times are timezone-aware and stored in IST.

| Model | Holds |
|---|---|
| `Candle` | start, interval, open, high, low, close, volume (None for the index, which has no traded volume) |
| `OptionContract` | underlying, expiry (date), strike, `CE` or `PE`. Equal fields mean the same contract. |
| `OptionQuote` | contract, timestamp, bid, ask, last traded price, bid and ask quantity, volume, open interest. Anything the source lacks is `None`, never `0`. **No lot size.** |
| `OptionChain` | snapshot time, spot, all quotes at that moment (several expiries allowed), a `data_label` and a source name |
| `Event` | a dated or timed scheduled event with an impact level |

`data_label` is `FIXTURE_SYNTHETIC`, `RECORDED_REAL` or `LIVE_READONLY`. It is what stops synthetic data being used where real data is required.

## Validation rules

Rejected when the object is built or the file is loaded (`DataValidationError`, with a stable `.code`):

| Code | Rule |
|---|---|
| `NAIVE_TIMESTAMP`, `BAD_TIMESTAMP` | every timestamp is timezone-aware; other zones are converted to IST |
| `DUPLICATE_CONTRACT` | a contract appears once per snapshot |
| `BID_GT_ASK` | bid may not exceed ask (locked, equal, is allowed) |
| `NEGATIVE_PRICE`, `NEGATIVE_VOLUME`, `NEGATIVE_OPEN_INTEREST` | no negative prices, quantities, volume or open interest |
| `BAD_NUMBER` | no binary floats, no NaN, no junk |
| `CANDLE_INCONSISTENT` | low <= open and close <= high |
| `QUOTE_FROM_FUTURE` | a quote may not be dated after its snapshot |
| `OFF_GRID_STRIKE`, `OFF_TICK` | strikes and prices follow the strike interval and tick from the contract layer |
| `UNKNOWN_EXPIRY` | the expiry date is a real NIFTY weekly or monthly expiry under the calendar and expiry rules |
| `EXPIRED_CONTRACT` | the contract expired before the snapshot date |
| `LOT_SIZE_IN_DATA` | a data file may not carry a lot size anywhere |
| `LOT_SIZE_UNAVAILABLE` | the contract layer has no lot size for that expiry |
| `MARKET_CLOSED`, `CANDLE_OUTSIDE_SESSION`, `CANDLE_ORDER`, `CHAIN_ORDER` | data sits inside a real session, in time order |
| `BAD_SCHEMA`, `BAD_LABEL`, `WRONG_UNDERLYING`, `BAD_OPTION_TYPE`, `BAD_IMPACT`, `MISSING_REQUIRED` | structure |

## Gate for a new entry (`check_entry_data` / `require_entry_data`)

Well-formed data may still be unusable. For each leg (`BUY` or `SELL`) it reports problems and never repairs them:

| Problem | Meaning |
|---|---|
| `DATA_UNVERIFIED` | synthetic fixture data while `data_mode` is not `fixture` |
| `MISSING_QUOTE` | the contract is not in the snapshot |
| `NO_ASK` / `NO_BID` | a buy needs an ask above zero; a sell needs a bid above zero |
| `STALE_DATA` | quote older than `data_stale_after_seconds` |
| `QUOTE_FROM_FUTURE` | snapshot after "now" |
| `EXPIRED_CONTRACT` | past expiry, or expiry day at or after the session close |
| `LOT_SIZE_UNVERIFIED` | the contract layer cannot give a verified lot size for the mode |
| `CALENDAR_UNAVAILABLE` | the calendar does not cover the date (non-fixture) |

`assess_quality` separately flags `ONE_SIDED`, `ZERO_BID`, `MISSING_OPEN_INTEREST`, `MISSING_VOLUME`, `STALE` and `WIDE_SPREAD` across a whole chain. These describe the data; whether a flag matters is a strategy decision for a later phase.

## Provider interface

`MarketDataProvider` has six getters and nothing else: `get_clock`, `get_underlying_bars`, `get_option_chain`, `get_quote`, `get_expiries`, `get_event_calendar`. `as_of` is the moment the caller stands at: no unfinished bar and no later snapshot is ever returned. `FixtureProvider` serves a validated scenario file, deterministically, with no network or disk writes.

## Scenario fixtures (`tests/fixtures/scenarios/`)

Synthetic: a seeded random walk for the index and a Black-76 model (zero rates, forward equal to spot) for the options, rounded to the contract tick. Regenerate with `python tools/make_fixtures.py`; `--check` confirms the files on disk match.

| Scenario | Date | Character |
|---|---|---|
| `bullish_trend` | 2026-09-16 | gap up, steady climb (+1.8%) |
| `bearish_trend` | 2026-09-17 | gap down, steady decline (-0.9%) |
| `sideways` | 2026-09-23 | mean-reverting chop, 0.4% range |
| `high_volatility` | 2026-10-07 | large bar moves, a jump at a synthetic 11:45 announcement, rich option premiums, expiry shifted by the Dussehra holiday |
| `illiquid_options` | 2026-09-24 | wide spreads, thin volume and open interest, many zero bids |
| `missing_invalid_data` | 2026-09-30 | loads but has candle gaps, a stale snapshot, one-sided and zero bids, missing strikes, open interest and volume, and a missing snapshot |

`tests/fixtures/invalid/` holds 15 files that must each be rejected, with the expected error code written inside the file. Fixture prices are not real and show nothing about how any strategy would perform.
