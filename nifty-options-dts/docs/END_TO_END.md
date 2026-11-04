# End to end, offline (P10)

```
data (read-only provider) -> regime -> strategy -> risk -> TICKET -> your "yes" -> paper entry -> position management -> journal
```

`python -m nifty_core.cli run-day --scenario NAME` replays one fixture day in five-minute steps from the entry-window start (10:15 IST) to the session close. At each step open positions are managed first; then, if no position is open and the day is not locked, one decision is made and journaled.

## Commands

```
python -m nifty_core.cli regime --scenario NAME --at HH:MM
python -m nifty_core.cli candidate --scenario NAME --at HH:MM
python -m nifty_core.cli risk --scenario NAME --at HH:MM
python -m nifty_core.cli ticket --scenario NAME --at HH:MM
python -m nifty_core.cli run-day --scenario NAME [--journal PATH] [--answer FINGERPRINT="yes"]
python -m nifty_core.cli report --journal PATH
python -m nifty_core.cli verify-journal --journal PATH
```

`regime`, `candidate`, `risk` and `ticket` are stateless views of one moment (a fresh day ledger is assumed for `risk` and `ticket`). Only `run-day` carries state through a day.

## What each decision becomes

| Status | Meaning |
|---|---|
| `NO_TRADE` | the strategy engine found no candidate (regime, window, data or liquidity). Reasons are journaled |
| `BLOCKED` | a candidate exists but the risk engine refused it. Every reason code is journaled |
| `AWAITING_APPROVAL` | the risk engine approved and a ticket was printed; no reply was supplied, so nothing was entered and the reserved risk was released |
| `DECLINED` | a reply was supplied but was not a clean "yes" for that ticket (recorded word for word) |
| `ENTRY_NOT_FILLED` | approved, but the paper entry was pending or rejected (missing quotes, price moved, stale approval) |
| `ENTERED` | filled; the position manager takes over |

## The approval flow

1. Run `run-day` (or `ticket --at HH:MM`) and read the ticket. It states the contracts, the net debit, the Total Defined Risk and the per-trade limit, and prints a 16-character fingerprint.
2. If you agree, re-run with `--answer FINGERPRINT="yes"`. The reply must start with "yes". A question, a condition or doubt ("yes but..."), a batch or standing instruction ("yes to all"), or a number that is not on the ticket makes it not count. The reply is stored in the journal exactly as typed, whether accepted or not.
3. There is no automatic approval and no flag for one. The tool cannot distinguish a person from a script that supplies the reply.
4. The replay applies the reply at the decision timestamp. A real-time flow would have to re-run the whole pipeline on fresh data after the human replies, because an approval goes stale after the 60-second staleness limit.

## The journal

Append-only JSON Lines, hash-chained (`verify-journal` finds any edit, deletion or reordering). One file holds one data source, "fixture" or "real"; the two are never mixed. Timestamps are decision times, so a replay is byte-for-byte repeatable. Record kinds: RUN_START, DECISION, TICKET, APPROVAL, EXECUTION, POSITION, EXIT, CRITICAL, DAY_LOCKED, RUN_END.

`report` shows decisions by status, why candidates were blocked, approvals, completed trades with sample size, net P&L after costs and the largest loss. Fixture reports carry "Simulation on synthetic or model-priced data. This is not evidence of profitability." Real-data reports make no performance claim below 30 completed paper trades.

## Behaviour to expect with the default settings

Every fixture candidate is BLOCKED on the per-trade limit (see docs/RISK_ENGINE.md). To see a full entry and exit, the tests use a separate configuration with twice the virtual capital. This is for testing the machinery and is not a recommended setting. With sparse fixture snapshots (four a day) an open position soon has stale quotes and is closed on DATA_SAFETY or, if a position is left open at the session end, reported as a critical exception.

## Limitations

- Only fixture data; there is no real-data provider (P12 is blocked on that choice).
- The kill switch blocks entries; it does not force an exit (exits stay possible, as approved after P8).
- Time machine (model-priced replay) and alerts skills from the original plan are not built.
- State between runs lives only in the journal; the risk ledger and positions are rebuilt per run.
