# Cost model

Two layers, kept apart so a second broker never touches strategy or risk code.

| Layer | File | Holds |
|---|---|---|
| Statutory and exchange | `config/costs/statutory_charges.json` | STT, exchange transaction charge, IPFT, SEBI fee, stamp duty, GST (rate and base) |
| Broker | `config/costs/brokers/<broker>.json` | Brokerage scheme and broker-only surcharges |

A new broker is a new JSON file with the same shape. Schemes available: `flat_per_executed_order`, `lower_of_pct_or_flat`, `zero`. Anything else needs a small code addition in `nifty_core/costs/brokers.py`.

Every rate is a list of versions with `effective_from`, `effective_to`, `status`, `sources` and `verified_on`, looked up by the trade date. See `docs/VERIFICATION_LOG.md`.

## A spread is two leg executions

There is no flat cost per spread. Each leg is costed on its own price and units (`lots x lot_size`):

| Charge | Applies to | Basis |
|---|---|---|
| Brokerage | each leg order | broker scheme |
| STT | SELL legs only | premium turnover |
| Exchange transaction charge, IPFT, SEBI fee | both sides | premium turnover |
| Stamp duty | BUY legs only | premium turnover |
| GST | each leg | on brokerage + SEBI fee + exchange charge + IPFT |

Each component is rounded to the paisa (half up) and then summed.

## Three different money figures

The model keeps these apart on purpose. They answer different questions.

1. **Entry risk** (`entry_risk_inr`) is the cash at risk the moment the spread is opened: the net debit actually paid (after entry slippage) plus the entry-side costs of both legs.
2. **Round-trip costs** (`round_trip_costs_inr`) are entry costs plus exit costs. This is what reduces a trade's net P&L, win or lose. It is not capital at risk by itself.
3. **Total defined risk** (`total_defined_risk_inr`) is entry risk plus the estimated exit costs. It is the worst case: the whole debit is lost and both sides' costs are paid. **This is the figure the risk gate compares with the per-trade limit.**

Stop-losses are trade management only. `defined_risk` has no stop-loss input, and no stop can lower these figures.

Exit costs are estimated before the exit happens, with the closing legs priced at their entry quotes (`entry_price`, the conservative default, which keeps STT on the long leg's sale at its entry premium) or at one tick (`floor_tick`, a spread that expired worthless). The costs of the exit that actually happened are computed afterwards from the paper fills with `actual_costs`.

## Worked example (2026-11-04, Zerodha profile, 1 lot = 65 units, slippage 0.5 point per leg)

Buy the lower-strike call quoted at 150, sell the higher-strike call quoted at 100:

| Figure | Rs |
|---|---|
| Quoted net debit (50 points x 65) | 3,250.00 |
| Entry slippage (0.5 x 2 legs x 65) | 65.00 |
| Net debit paid | 3,315.00 |
| Entry costs (brokerage 40.00, STT 9.70, exchange 5.78, stamp 0.29, SEBI 0.02, GST 8.25) | 64.04 |
| **Entry risk** | **3,379.04** |
| Estimated exit costs | 68.86 |
| Round-trip costs | 132.90 |
| **Total defined risk** | **3,447.90** |

Against the approved Rs 2,500 per-trade limit this spread is BLOCKED. A cheaper spread (80 and 60 quoted, a 20-point debit) has a total defined risk of about Rs 1,481 and fits. Round-trip costs were Rs 116 to Rs 133 in these two cases. These examples are illustrations, not market quotes.

## Gate for non-fixture use

`CostEngine.assert_ready(date)` and every cost call raise `CostRateUnverified` (with `.missing` naming each item) in a non-fixture mode if any needed rate is unverified, missing, or not valid for that date. In `fixture` mode unverified or extrapolated rates are used but each result carries warnings. `verification_report(date)` lists every rate and its status.
