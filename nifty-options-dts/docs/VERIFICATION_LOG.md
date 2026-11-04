# Verification log

Every exchange-, tax- or broker-dependent value, how it was checked, and what is still open. The machine-readable form is in `config/` (each value has `status`, `sources`, `verified_on`, `effective_from`, `effective_to`). Source ids are in `config/sources.json` with the SHA-256 of the bytes read; text excerpts are in `docs/evidence/`.

Last full verification pass: **2026-11-04** (P0 and P2). Documents were downloaded from the publisher and read directly. PDFs were converted with `pdftotext`. Web pages were fetched as HTML; their hash pins what was read, but pages change, so re-check them when it matters.

Statuses:

- `VERIFIED_PRIMARY`: read in an NSE circular or web page, or in the Finance Act.
- `VERIFIED_BROKER_TARIFF`: read on the broker's own published tariff (primary for that broker's charges only).
- `UNVERIFIED`: not confirmed. Usable in `fixture` mode only, always flagged.

A value with an unknown `effective_from` is trusted only from its `verified_on` date. Asking for an earlier date works in fixture mode with a warning and is refused in any other mode.

## Verified values

### Contract specification (`config/contracts/nifty_contract_specs.json`)

| Value | Effective | Source |
|---|---|---|
| Lot size 75 | 2025-10-03 to expiry 2025-12-30 (weekly and monthly) | NSE circular 176/2025, NSE/FAOP/70616, 2025-10-03 |
| Lot size 65 | from expiry 2026-01-06 (first weekly), 2026-01-27 (first monthly) | same circular |
| Weekly expiry Thursday, monthly last Thursday | up to 2025-08-28; start unknown | NSE circular 108/2025, NSE/FAOP/68685, 2025-06-23 |
| Weekly expiry Tuesday, monthly last Tuesday | from 2025-09-09 (first weekly on the new rule; the week of 2025-09-01 has none) | same circular and its annexure; NSE NIFTY 50 F&O page |
| Holiday on expiry day: previous trading day | start unknown | NSE NIFTY 50 F&O page; NSE contract specifications page |
| Strike interval 50 (weekly and monthly) | start unknown | NSE NIFTY 50 F&O page |
| Tick size 0.05 | start unknown | NSE contract specifications page (read by column position in flattened HTML) |
| Session 09:15 to 15:40 | from 2026-08-03 | NSE closing-auction-session page; NSE/FAOP/75472 (go-live of derivatives modalities 2026-08-03) |

### Calendar (`config/market/nse_calendar.json`)

| Value | Source |
|---|---|
| 2026: 15 weekday holidays and 4 weekend holidays | NSE/FAOP/71777 (F&O, 2025-12-12); the Capital Market circular NSE/CMTR/71775 lists the same dates |
| Muhurat session Sunday 2026-11-08: pre-open 14:05 to 14:20, normal market 14:20 to 15:40 | NSE/FAOP/76940, 2026-10-19 |

### Statutory and exchange charges (`config/costs/statutory_charges.json`), NSE equity options

| Charge | Rate | Effective | Source |
|---|---|---|---|
| STT on SELL, on premium | 0.10% | 2024-10-01 to 2026-03-31 | NSE/FATAX/63809 (2024-09-09), NSE/FATAX/73524 |
| STT on SELL, on premium | 0.15% | from 2026-04-01 | NSE/FATAX/73524 (2026-03-31); Finance Act 2026 (Act 4 of 2026, assent 2026-03-30); Finance Bill 2026 memorandum clause 35 |
| Exchange transaction charge, each side | Rs 3,503 per crore of premium | 2024-10-01 to 2026-02-28 | NSE/FA/64232 (2024-09-27); NSE/FA/73061 |
| Exchange transaction charge, each side | Rs 3,552.99 per crore | from 2026-03-01 | NSE/FA/73061 (2026-02-27) |
| IPFT contribution, each side | Rs 50 per crore up to 2026-02-28 (start unknown); Rs 0.01 per crore from 2026-03-01 | | NSE/FA/73061 |
| SEBI turnover fee, each side | Rs 10 per crore | start unknown | NSE levies page; Zerodha charges page |
| Stamp duty, BUY side | 0.003% of premium | from 2020-07-01 | NSE levies page |
| GST | 18% on brokerage, SEBI fee, exchange transaction charge and IPFT | start unknown | NSE levies page (18%); Zerodha page (the base) |

Total NSE exchange outflow on options is Rs 3,553 per crore before and after 2026-03-01 (NSE/FA/73061 says the revision has no overall impact).

### Broker: Zerodha (`config/costs/brokers/zerodha.json`)

| Value | Source |
|---|---|
| Options brokerage: flat Rs 20 per executed order | Zerodha charges page, retrieved 2026-11-04 |
| Debit-balance surcharge: Rs 20 per executed order | same page |

## NOT verified or only partly verified

1. **A newer NIFTY lot-size circular after NSE/FAOP/70616.** None was found, but absence in a search is not proof. Before real use, check the current `NSE_FO_contract_ddmmyyyy.csv.gz`.
2. **Derivatives session before 2026-08-03** (assumed 09:15 to 15:30). Not read from a primary document. Marked UNVERIFIED, fixture mode only.
3. **Start dates** of the old Thursday expiry rule, the expiry holiday rule, strike interval, tick size, SEBI fee, GST and Zerodha tariff are unknown. They apply from 2026-11-04 in non-fixture mode.
4. **Holidays after 2026**, and any ad hoc holiday announced after 2025-12-12. The 2027 calendar does not exist yet, so non-fixture mode refuses 2027 dates.
5. **SEBI fee of Rs 15 per crore:** Zerodha's page says it will apply once SEBI announces a date. No date was found, so Rs 10 is used. Re-check.
6. **STT rounding.** The model rounds each charge to the paisa. Whether the exchange or broker rounds STT to the rupee was not verified.
7. **How a broker counts orders for a spread** (a basket, partial fills, or auto square-off orders that carry extra charges, such as Zerodha's Rs 50 call-and-trade charge) is not modelled. Each leg is one executed order.
8. **State stamp-duty variations,** DP charges and other non-trading fees are not modelled.
9. **Derivatives data timing:** the NIFTY index (spot) calculation hours may differ from the derivatives session now that options run to 15:40. Not verified; relevant to the market-data phase.
10. **Tick size reading:** taken from flattened HTML by column position (see notes in the data file).

## Assumptions in code (not exchange facts)

- Slippage 0.5 point per leg, entry window 10:15 to 14:30 IST (aligned with the regime engine in P6; was 09:45 before), flat-by 15:15 IST: config defaults chosen by the implementer.
- Config limits are editable; there are no hard ceilings above the approved defaults.
- Fixture mode allows UNVERIFIED and extrapolated values with warnings. `real_readonly` mode refuses them.

## Added in P3 (fixtures)

- All scenario data is SYNTHETIC: a seeded random walk for the index, Black-76 prices (zero rates, forward equal to spot) for options, rounded to the contract tick. Index levels near 25,000 are round numbers, not real. Nothing in a fixture is evidence of market behaviour.
- Fixture dates (2026-09-16 to 2026-10-07) use the verified 09:15 to 15:40 session. Other rules those dates rely on (strike interval, tick, expiry holiday rule) have an unknown start date, so the fixtures load in `fixture` mode only. Loading them in `real_readonly` mode raises a contract error, by design.
- The synthetic "policy announcement" on 2026-10-07 is invented and says so in its name.
- Fixture expiries are computed by the verified expiry rules; 2026-10-19 (a Monday) appears because Dussehra, 2026-10-20, is a holiday.

## Added in P4 (spread model)

- No new exchange or tax facts. Lot size, strike interval and tick size are read from the existing versioned layer for each lookup.
- The two exit-cost scenarios in `cost_view` (spread worthless, spread at full width) are modelling choices for cost estimates, not exchange facts and not predictions.
- A credit spread's short leg is treated as "not covered" on purpose, because credit spreads are outside Version 1.
- The expiry payoff figures assume European-style options held to expiry. How NSE settles or taxes an expiry-day exercise was not verified and is not used, because this system closes positions intraday.

## Added in P5 (regime engine)

- No new exchange facts. The engine uses the verified session open and close only to check that the day's candles start at the open and stay inside the session.
- Every regime threshold is a STRATEGY ASSUMPTION in `config/regime_thresholds.json`. None is an exchange fact or market data. They were chosen by reasoning, not optimised, and are not validated on real markets.
- The fixtures the tests use are synthetic, so fixture labels show the logic works and nothing about usefulness on real data.
- Resolved in P6 by your decision: `min_bars` stays 12; the entry window now starts at 10:15. Regime thresholds stay documented assumptions and are not tuned on synthetic fixtures.

## Added in P6 (strategy engine)
- Entry window 10:15 to 14:30 IST and flat-by 15:15 IST remain strategy assumptions, not exchange rules, configurable in `config/config.example.json`. A check refuses an entry window that opens before the regime engine can classify.
- Every number in `config/strategy_thresholds.json` is an implementer assumption, not verified against any source and not validated on real data. See `docs/STRATEGY_ENGINE.md`.
- Liquidity minimums (volume 5,000, open interest 20,000, depth 1 lot) are in fixture units and must be recalibrated against recorded real option chains.
- No exchange fact was added or changed in P6.

## Added in P7 (risk engine)
- No exchange fact or cost rate was added or changed. Risk limits come from `config/config.example.json` (capital 250,000, 1 percent per trade, 2 percent daily, 1 lot, 2 trades): paper-simulation settings only, not a recommendation for real-money capital.
- `HARD_MAX_LOTS = 1` is a Version 1 code constant, so configuration cannot raise the lot ceiling.
- Realized profit does not raise the daily loss budget (implementer's conservative choice).
- Strategy thresholds were not changed in P7.

## Added in P8 (paper execution)
- No exchange fact or cost rate was added or changed. Fills are simulated from quotes with the configured slippage; no real order capability exists.
- Approved by you after P7: risk and account settings unchanged, realized profit never raises the loss budget, 60-second staleness kept (configurable, a data-quality assumption).
- Design choices to confirm: exits are allowed under the kill switch; a quote with missing or insufficient size blocks a fill; fills that would exceed the approved defined risk are refused.

## Added in P9 (position management)
- No exchange fact or cost rate was added or changed.
- Every number in `config/position_thresholds.json` (profit target 60 percent and stop-loss 50 percent of the approved defined risk, regime invalidation switches, three failed data checks) is an implementer assumption, not validated on real data and not tuned on fixtures.
- Decisions you approved after P8: exits allowed under the kill switch; missing or insufficient quoted size returns PENDING; fills above the approved defined risk are refused; an expired approval never blocks a later fresh decision.

## Added in P10 (skills, CLI, approval, journal, end-to-end)
- No exchange fact or cost rate was added or changed.
- The original plan's P9 (journal and reports) was displaced when P9 became position management; it was built inside P10 (append-only, hash-chained, fixture and real kept apart).
- The minimum number of completed real paper trades before a report states performance (30) is an implementer assumption.
- The end-to-end tests use a test-only configuration with twice the virtual capital so a fixture candidate fits the per-trade limit; the default settings were not changed.
