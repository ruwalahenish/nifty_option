# Spread model (P4)

Version 1 supports exactly two structures and nothing else.

| Structure | Long leg | Short leg |
|---|---|---|
| Bull Call Debit Spread (`BULL_CALL_DEBIT`) | call at the LOWER strike | call at the HIGHER strike |
| Bear Put Debit Spread (`BEAR_PUT_DEBIT`) | put at the HIGHER strike | put at the LOWER strike |

No credit spreads, naked positions, other multi-leg structures or adjustments. A spread is built only by `DebitSpread.create(legs, specs, trade_date, data_mode)` (or `builders.from_chain`, which buys the long leg at its ask, sells the short leg at its bid, and takes the lot size from the contract layer). It validates everything, **repairs nothing**, and raises `SpreadError` listing every problem. `.code` is the most specific reason, `.codes` is the full list.

## Formulas (expiry payoff concepts, per spread)

Let `units = lots x lot_size`, with `lot_size` from the versioned contract layer for the contract's expiry.

| | Bull call | Bear put |
|---|---|---|
| Width (points) | short strike - long strike | long strike - short strike |
| Net debit (points) | long entry price - short entry price | same |
| Gross maximum loss (Rs) | net debit x units | same |
| Gross maximum profit (Rs) | (width - net debit) x units | same |
| Expiry breakeven | long strike + net debit | long strike - net debit |

Always `gross max loss + gross max profit = width x units`.

### Hand-worked examples (1 lot = 65 units)

| | Bull call | Bear put |
|---|---|---|
| Legs | long 25000 CE @ 150.00, short 25100 CE @ 100.00 | long 25000 PE @ 120.50, short 24900 PE @ 85.25 |
| Width | 100 | 100 |
| Net debit | 50.00 | 35.25 |
| Gross max loss | 50 x 65 = 3,250.00 | 35.25 x 65 = 2,291.25 |
| Gross max profit | 50 x 65 = 3,250.00 | 64.75 x 65 = 4,208.75 |
| Expiry breakeven | 25,050 | 24,964.75 |

On an expiry that still had the old 75-unit lot (for example 2025-12-23), the same bull call example loses at most 50 x 75 = 3,750.00. The lot size is looked up per expiry, never assumed.

## Expiry payoff versus intraday P&L

**Width, net debit, gross maximum profit, gross maximum loss and the breakeven are expiry payoff concepts.** They say what the structure is worth if both legs are held to expiry, and they bound the worst case. This system is an intraday paper-trading system: positions are closed the same day, long before expiry, with time value still in both legs.

- Intraday P&L must come from the actual exit price of each leg: `gross_pnl_from_exit_prices(long_exit, short_exit)` = ((long exit - long entry) + (short entry - short exit)) x units, before costs. Example: bull call exited with the long leg at 170.00 and the short leg at 105.00 = (+20 - 5) x 65 = +975.00.
- Do not read the expiry figures as a forecast of an intraday exit price or profit. When the index sits exactly at the expiry breakeven, the expiry payoff is zero, but an intraday exit then can show a gain or a loss depending on the two leg prices.
- `expiry_payoff_inr(settlement_price)` exists for tests and for understanding the shape of the structure.

## Payoff is separate from costs

The spread object holds payoff figures only and never changes them. Costs come from the cost engine through separate calls.

`spread.cost_view(engine)` returns, with each leg costed as its own execution:

| Field | Meaning |
|---|---|
| `entry_costs_inr` | both legs bought / sold at the entry prices |
| `exit_costs_at_max_loss_inr` | closing each leg at one tick (the spread is worthless) |
| `exit_costs_at_max_profit_inr` | short leg bought back at its entry price, long leg sold at that price plus the width (the spread is worth its full width) |
| `net_max_loss_inr` | gross max loss + entry costs + exit costs (max-loss scenario) |
| `net_max_profit_inr` | gross max profit - entry costs - exit costs (max-profit scenario) |
| `net_max_profit_is_positive` | False when costs swallow the profit |

The two exit scenarios are cost scenarios, not predictions of any exit price.

`spread.defined_risk(engine, slippage_points)` is the bridge to the risk gate's figure (Total Defined Risk = net debit + entry costs + estimated exit costs + slippage). The spread model reports it and never adjusts a spread to fit a limit.

Hand-worked costs for the bull call example, Zerodha rates for 2026-09-16: entry 64.07 (buy leg 27.98, sell leg 36.09); exit 47.20 (worthless) or 75.10 (full width); net max loss 3,361.27; net max profit 3,110.83.

## Rejection rules

| Code | Rejected because |
|---|---|
| `WRONG_LEG_COUNT` | not exactly 2 legs (0, 1, 3, 4 or more) |
| `TWO_LONG_LEGS`, `TWO_SHORT_LEGS` | both legs on the same side |
| `MIXED_OPTION_TYPES` | a call with a put |
| `MIXED_UNDERLYING`, `UNSUPPORTED_UNDERLYING` | different underlyings; anything other than NIFTY |
| `MIXED_EXPIRY` | different expiry dates |
| `MIXED_LOT_SIZE`, `LOT_SIZE_MISMATCH` | legs disagree, or a leg's lot size differs from the contract layer's |
| `LOT_SIZE_UNAVAILABLE` | the contract layer has no usable (verified, for the mode) lot size for that expiry |
| `UNEQUAL_QUANTITY` | different numbers of lots |
| `SAME_STRIKE`, `WRONG_STRIKE_ORDER` | strikes equal, or not in the required order |
| `NOT_A_DEBIT` | net premium is a credit |
| `ZERO_DEBIT` | net premium is zero |
| `DEBIT_NOT_BELOW_WIDTH` | debit equals or exceeds the width, so no profit is possible |
| `NAKED_SHORT` | any short option not fully covered by a long of the same underlying, expiry and type at the same or a more protective strike |
| `OFF_GRID_STRIKE`, `OFF_TICK_PRICE` | strike or price not on the contract layer's grid |
| `LAYER_UNAVAILABLE` | the strike interval or tick size is not usable in this data mode |
| `EXPIRED_CONTRACT` | (new entry) a leg has expired; on expiry day entry stops at the session close |
| `NO_ASK`, `NO_BID`, `MISSING_QUOTE` | (builder) the chain cannot price a leg |
| `BAD_LEG`, `BAD_PRICE` | malformed leg or price |

`NAKED_SHORT` is listed last so the more specific structural reason is the primary code. A credit spread's short leg counts as not covered: this system does not trade credit spreads.

## Not done in P4

No strike selection, no width choice, no regime or strategy logic, no risk-limit decisions, no sizing, no execution. Those are later phases.
