# Position management (P9)

`nifty_core/positions/manager.py` manages a paper position only after a successful paper entry. Version 1 has two actions: **HOLD** and **CLOSE**. There is no operation to add lots, average, roll a strike, add a leg or run a rescue trade (a test lists the manager's public methods and a test checks the exit uses exactly the entry contracts and lots).

## States

```
OPEN ──► CLOSED                  both legs exited by simulated fills
  │
  ├────► EXIT_PENDING ──► CLOSED     a close was decided but could not be filled; retries with fresh data
  │            │
  └────────────┴──► UNCLOSED_CRITICAL  the session ended with the position open: a critical exception, never "closed"
```

`CLOSED` and `UNCLOSED_CRITICAL` are terminal. Every transition is kept in `history`. A position in `UNCLOSED_CRITICAL` stays open in the risk ledger and in the paper executor.

## Exit reasons (priority order; every reason that applies is recorded)

| Reason | Rule |
|---|---|
| `FORCED_FLAT_BY` | `now >= flat_by_ist` (15:15 by default, configurable). An exit attempt is made on every call until the session closes |
| `MANUAL_PAPER_EXIT` | explicitly requested |
| `DATA_SAFETY` | exit quotes or the regime data unusable on `data_safety_failed_checks` consecutive checks (a good check resets) |
| `STOP_LOSS` | unrealized net P&L <= -(stop_loss_pct_of_defined_risk % of the approved defined risk) |
| `REGIME_INVALIDATION` | the regime turned to the opposite direction, or to HIGH_VOLATILITY (each switchable) |
| `PROFIT_TARGET` | unrealized net P&L >= profit_target_pct_of_defined_risk % of the approved defined risk |

Once a close is decided it is **sticky**: the manager keeps trying even if the trigger later fades. Stop-loss and profit-target need valid exit quotes to be evaluated at all, so a close triggered by them always has quotes that pass the fill checks; a pending exit arises from forced, manual, data-safety, regime or an earlier decision.

## Assumptions (`config/position_thresholds.json`, kind STRATEGY_ASSUMPTIONS)

| Name | Default |
|---|---|
| `profit_target_pct_of_defined_risk` | 60 |
| `stop_loss_pct_of_defined_risk` | 50 (at most 100) |
| `invalidate_on_opposite_regime` | true |
| `invalidate_on_high_volatility` | true |
| `data_safety_failed_checks` | 3 |

Chosen by reasoning. **Not validated on real data and not tuned on the synthetic fixtures.** The forced flat-by time stays in `config/config.example.json`. The risk engine never imports or reads any of this: a stop-loss does not reduce Total Defined Risk.

## Unrealized versus realized

| Figure | Source |
|---|---|
| estimated at approval | the P7 figures (risk gate only) |
| actual entry fills and costs | simulated entry fills, cost engine |
| unrealized (trigger only) | mark to liquidation: long leg at bid - slippage, short leg at ask + slippage, minus ACTUAL entry costs and ESTIMATED exit costs. Labelled |
| actual exit fills and costs | simulated exit fills, cost engine |
| gross P&L | both legs' fill prices |
| net realized P&L | gross - actual entry costs - actual exit costs; passed to the risk ledger |

`pnl_report` keeps these in separate keys; realized fields are `None` until the position is really closed.

## Forced flat-by and the session end

At or after flat-by every `manage` call requires an exit attempt. If data is missing, stale or one-sided, or the quoted size is insufficient, the position is `EXIT_PENDING` with `forced = true` and the reasons, and the attempt repeats on each call while the market is open. At or after the session close (15:40 from 2026-08-03) no exit is attempted; the position becomes `UNCLOSED_CRITICAL`, a `CriticalException` is recorded, and nothing is marked closed.

## Entry guards

Beyond the risk engine, `enter` refuses (and logs) a second position on the same contracts (`DUPLICATE_POSITION`) or beyond the lot ceiling (`POSITION_LOT_CEILING`). The kill switch blocks entries but not exits.

## Known limitations and open questions

- Profit-target and stop-loss use unrealized P&L at the liquidation price; real exits will differ.
- Exits are only attempted when the caller calls `manage`; scheduling belongs to the run loop (P10).
- Regime flicker (P5) may trigger REGIME_INVALIDATION on noise only when the label turns to the opposite direction or HIGH_VOLATILITY; NO_TRADE and SIDEWAYS do not.
- A regime `NO_DATA` counts as a data-safety strike, which couples position handling to candle data quality.
- State is in memory for one day.
