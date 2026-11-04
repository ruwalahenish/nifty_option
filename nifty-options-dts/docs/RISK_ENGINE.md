# Risk engine (P7)

`nifty_core/risk/engine.py` decides **APPROVED or BLOCKED** for a strategy candidate. It never changes a strike, premium, width, quantity or strategy assumption to make a trade fit. Every check runs and every failure is recorded.

## Total Defined Risk (the per-trade figure)

```
net debit quoted            (ask of long leg - bid of short leg) x units
+ entry slippage            configured points per leg x units, both legs, adverse
= net debit paid
+ entry costs               brokerage, STT, exchange charge, IPFT, SEBI fee, stamp duty, GST, leg by leg
+ estimated exit costs      both legs closed at their entry quotes (conservative: keeps STT on the long leg's sale)
= TOTAL DEFINED RISK        compared with risk_per_trade_inr (2,500.00 by default)
```

Equal to the limit passes; one paisa above blocks. There is no stop-loss input anywhere in the engine: a stop-loss can never reduce this figure (a test inspects the signatures). The calculation comes from the P2 cost engine and the P4 spread, so costs follow the dated, source-tracked rates; unverified rates outside fixture mode give `COST_RATES_UNVERIFIED`.

## Six different numbers (never interchangeable)

| Name | Meaning |
|---|---|
| `account_capital_inr` | the virtual account size (250,000); constant inside the day |
| `capital_committed_inr` | cash tied up by OPEN positions: net debit paid + entry costs; released on close |
| defined risk | for ONE trade: the total above; what the per-trade limit tests |
| `open_risk_reserved_inr` | the sum of defined risk over OPEN positions |
| `realized_pnl_inr` | net P&L of positions closed today (profit and loss) |
| `remaining_daily_risk_capacity_inr` | `max_daily_loss - realized loss used - open risk reserved`, never below zero |

`realized_loss_used = max(0, -realized_pnl)`. **Realized profit does not raise the loss budget** (a deliberate conservative choice; see open questions).

## Checks (all run; all failures reported)

| Code | Rule |
|---|---|
| paper-only | `assert_paper_only` raises `LiveTradingNotSupported`; not a decision, a refusal |
| `KILL_SWITCH_ACTIVE` | KILL file present. `kill_dir` is a required argument; there is no config key or environment variable to skip it |
| `CANDIDATE_INVALID`, `CANDIDATE_INCONSISTENT` | the candidate's summary fields must match its own validated `DebitSpread` |
| `STRUCTURE_INVALID`, `NAKED_SHORT` | independent re-check: one long, one covered short, same type/expiry/quantity |
| `CANDIDATE_FROM_FUTURE`, `CANDIDATE_STALE`, `CANDIDATE_OTHER_DAY` | decided after now; older than `data_stale_after_seconds` (inclusive); not today |
| `LEDGER_DAY_MISMATCH`, `OVERNIGHT_POSITION_PRESENT` | one ledger per IST day; no open position from an earlier day |
| `OUTSIDE_ENTRY_WINDOW`, `AFTER_FLAT_BY` | now inside the entry window (inclusive); strictly before flat-by |
| `DATA_MODE_MISMATCH`, `DATA_UNVERIFIED` | candidate mode equals configuration; no synthetic data outside fixture mode |
| `LOT_SIZE_MISMATCH`, `LOT_SIZE_UNVERIFIED`, `EXPIRED_CONTRACT` | lot size agrees with the contract layer; no expired contract |
| `MAX_LOTS_EXCEEDED`, `MAX_LOTS_EXCEEDED_COMBINED` | lots <= `min(max_lots, HARD_MAX_LOTS=1)`; open lots count |
| `MAX_TRADES_REACHED` | trades today (open and closed) < `max_trades_per_day` |
| `PER_TRADE_RISK_EXCEEDED` | total defined risk <= `risk_per_trade_inr` |
| `DAILY_LOSS_LIMIT_REACHED`, `DAILY_RISK_CAPACITY_EXCEEDED` | realized loss below the daily limit; defined risk <= remaining capacity |
| `CAPITAL_EXCEEDED` | capital committed after approval <= account capital |
| `AVERAGING_BLOCKED` | no overlap in contracts with an open position |
| `CANDIDATE_ALREADY_USED` | the exact candidate (SHA-256) was already approved today: no splitting or repeating |
| `RECOVERY_TRADE_NOT_INDEPENDENT`, `RECOVERY_SIZE_INCREASE` | after a loss, the new candidate must be decided strictly after the loss closed, and lots may not increase |

## How the limits interact

- The binding per-trade limit is on **total defined risk**, not on the net debit: costs and slippage (about 7 to 8 percent on top of the debit at default settings) can push a debit under 2,500 over the limit.
- A position's defined risk is **reserved** against the 5,000 daily budget while it is open, and released when it closes, at which point the actual loss (if any) replaces it. With the default one-lot ceiling only one position can be open, so the daily budget mainly bites after a loss: a 3,000 loss leaves 2,000 of capacity, and most real candidates cost more than that.
- `max_lots` in configuration can lower the lot limit but never raise it above `HARD_MAX_LOTS = 1`.
- The daily-loss stop is permanent for the day once the realized loss reaches the limit.

## Approvals and bypass protection

An approval carries the candidate's SHA-256 fingerprint (over its complete journal record, including the mutable inner dictionaries), the approved lots, the defined risk and its own digest. `RiskManager.request_entry` evaluates and, only if approved, reserves the risk in the ledger in the same step, so a second approval for the same exposure is impossible. `RiskManager.confirm_entry(approval, candidate, lots)` must be called by any later entry step; it refuses (with `RiskViolation` and a code) a tampered approval, a modified candidate, a changed quantity, an approval not open in the ledger, or one already used once, and re-checks paper mode and the kill switch. The digest detects accidental or careless modification; it is not a cryptographic secret, because the engine runs in the same process as its caller.

## Journal record

`decision_to_journal` gives plain JSON: outcome, candidate id, time, every reason, the full calculation (every component), all limits, the ledger before and after, trades before and after, and the configuration, strategy and regime fingerprints.

## Limitations and open questions

- Exit cost uses the entry quotes (a conservative estimate); the true exit price is unknown.
- The ledger lives in memory for one day; persistence belongs to the journal phase.
- Realized profit not raising the loss budget is a judgment call (net P&L would be the alternative).
- A re-quote of the same contracts after a close is a new independent decision only if it is decided later than the close; the engine cannot judge whether the new decision is truly independent in substance.
- "Averaging" is detected by contract overlap with an open position.
- Every default fixture candidate is blocked on the per-trade limit (see the P7 report).
