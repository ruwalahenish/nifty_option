# Paper execution (P8)

`nifty_core/execution/paper.py` produces **simulated** fills for an approved debit spread. There is no broker, network, credential or order interface in it. It reads quotes through the read-only provider (`get_quote` only; a test enforces that) and writes a record of what a conservative simulation says would have happened.

## Fill rules

Adverse slippage is `slippage_points_per_leg` (0.5 by default) on every leg execution, and a sold price never goes below one tick.

| | Long option | Short option |
|---|---|---|
| Entry | BUY at ask + slippage | SELL at bid - slippage |
| Exit | SELL at bid - slippage | BUY at ask + slippage |

Quotes are those known at the execution time. Nothing fills at a mid price or an old price.

## No invented fills

A leg has no usable quote when it is missing, stamped after now, older than `data_stale_after_seconds`, one-sided (a bid or ask missing or zero), off the tick grid, for an expired contract, or when the quoted size on the side we trade is missing or below the order's units. Any such leg gives **PENDING** with reasons and **no fill at all**. Both legs fill together or not at all: no partial fills and no single-leg positions. A pending entry leaves the approval valid, so it can be retried until it goes stale.

## Entry: integrity checks (REJECTED)

Run first, in `RiskManager.verify_entry` (which does not use the approval up): authentic approval digest, identical candidate fingerprint, same lots, approval open in the ledger and not already used, kill switch, paper-only guard. A broken paper-only guard **raises** `LiveTradingNotSupported` (a refusal, not a result). Then: approval not from the future, not older than the staleness limit, candidate from today, inside the entry window, before flat-by. A stale or out-of-window approval is REJECTED and its reserved risk is released (`cancel_unused`); the candidate stays marked as used so it cannot be re-approved.

## Entry: the risk guard at fill time

Fresh quotes may differ from those the candidate was built on. The Total Defined Risk is recomputed at the fill quotes with the same formula. If it exceeds the **approved** figure, no fill is made (PENDING, `FILL_EXCEEDS_APPROVED_RISK`). A better price fills at the better price. The engine never changes strikes, quantity, structure or prices on its own.

## Estimated versus actual

| Where | Meaning |
|---|---|
| `estimated_at_approval` | the P7 figures (exit costs assume both legs close at their entry quotes). Used only for the risk gate. Labelled as an estimate |
| `actual` | costs computed by the cost engine from the simulated fill prices, and P&L from both legs' fill prices |

Net P&L = gross P&L from the fill prices - actual entry costs - actual exit costs. The estimated exit cost never appears in `actual`. The actual net P&L is passed to the risk ledger on exit (`record_close`).

## Exits

An exit needs no approval (it reduces risk), so it stays possible under the kill switch; the paper-only guard still applies. It uses the same quote rules. A missing quote leaves the position OPEN and PENDING. An exit before the entry, on another day, twice, or for an unknown position is REJECTED.

## Audit record (`result_to_journal`)

Execution ID, approval ID, candidate fingerprint, decision / approval / execution timestamps, both contracts, side, lots and units per leg, market bid and ask, quote time and age, reference price, slippage, fill price, actual costs by component and per leg, the estimated block, and fingerprints (configuration, candidate, strategy thresholds, regime thresholds, execution model version, slippage). Every attempt, filled or not, is kept in `PaperExecutor.log`. Records are marked paper and SIMULATED.

## Known limitations

- Fills assume the full quoted size is available at the quoted price plus a fixed slippage; there is no queue, partial fill, latency or market impact.
- A quote with a missing or too-small size blocks the fill (conservative), which can leave an exit PENDING; deciding what to do about a stuck exit (flat-by) belongs to the position-management phase.
- Slippage is a fixed number of points, not a function of spread or size.
- The exit-time risk is not limited by any approval: P9 decides when and why to exit.
- State is in memory for one day.
- Results are only as good as the quotes; the fixtures are synthetic.
