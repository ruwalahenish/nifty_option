# Recorded real data (P12)

PAPER trading only. Not financial advice. Real data feeds a simulation: fills are modelled from quoted bid, ask and size,
and no order is ever sent.

## What was built, and what was not

You chose "your own exported files" as the data source. So there is **no vendor connection**, no network code and no
credentials in this project. The `ReadOnlyBrokerProvider` named in the original plan (GET-only HTTP, endpoint allowlist,
environment credentials) was **not built**, because no vendor was chosen and the core stays offline. If you pick a vendor
later, that provider is a separate, reviewed step.

What exists:

- `tools/csv_to_scenario.py` converts two CSV exports (and an optional events CSV) into one data file. It only converts text.
- The same read-only provider as the fixtures serves the file. It has the same six getters and no order-like method.
- A data file labelled `RECORDED_REAL` is the real-data label. Everything downstream (regime, strategy, risk, paper
  execution, positions, journal) runs unchanged.
- `python -m nifty_core.cli check-data --scenario FILE` validates a file without trading.

## The rules that keep real and synthetic data apart

1. **Label and mode must match.** `FIXTURE_SYNTHETIC` data needs config `data_mode: "fixture"`. `RECORDED_REAL` data needs
   `data_mode: "real_readonly"`. A mismatch is refused (`DATA_MODE_MISMATCH`) before anything runs or is written.
2. **Real mode accepts only verified contract facts.** Lot size, strike interval, tick size, expiry rules and cost rates must
   be VERIFIED and in force on the day. Where a fact has no known start date, it is trusted only from its verification date
   (**2026-11-04** at the time of writing). So a real-data day **before that date is refused** (`no version covers ...`).
   The verification dates must be moved forward by re-checking the sources, never by editing the data.
3. **Separate journals.** A journal file holds fixture or real data, never both.
4. **No performance claim from a small sample.** A real-data report prints no P&L until 30 completed paper trades exist
   (`MIN_REAL_TRADES`, an assumption of mine, adjustable in `journal/report.py`). It always shows sample size.
5. **Daily limits cannot be reset by re-running a day.** Limits live inside one run. For real data, a day that already has an
   entered paper trade in the journal cannot be run again (`DAY_ALREADY_TRADED`). A run that only showed a ticket may be
   repeated to answer it, because it entered nothing.
6. **Kill switch, approval and paper-only rules are unchanged** and apply identically.

## Making a data file

`candles.csv` (5-minute NIFTY index candles, one row each):

```
start,open,high,low,close,volume
2026-11-05T09:15:00+05:30,25050.00,25060.10,25040.00,25055.30,
```

`chain.csv` (one row per option quote; rows with the same `snapshot_time` form one snapshot; `spot` must be the same inside
a snapshot):

```
snapshot_time,spot,expiry,strike,type,bid,ask,ltp,bid_qty,ask_qty,volume,open_interest,timestamp
2026-11-05T10:15:00+05:30,25055.30,2026-11-09,25000,CE,150.10,151.00,150.55,1300,1500,52000,980000,2026-11-05T10:15:00+05:30
```

`events.csv` (optional): `when,name,impact` with impact HIGH, MEDIUM or LOW.

Times carry `+05:30`. Bid, ask and quantities must be real quoted values: the fill model refuses to invent a price or a
size when they are missing, and a missing size leaves an order PENDING. Do not include a lot-size column; the loader refuses it.

```
python tools/csv_to_scenario.py --date 2026-11-05 --name nifty_2026-11-05 --candles candles.csv --chain chain.csv --out data/nifty_2026-11-05.json
python -m nifty_core.cli check-data --scenario data/nifty_2026-11-05.json --config my_real_config.json
python -m nifty_core.cli run-day  --scenario data/nifty_2026-11-05.json --config my_real_config.json --journal trader/journal/real.jsonl
```

`my_real_config.json` is a copy of `config/config.example.json` with `"data_mode": "real_readonly"`. Leave the risk limits at
the defaults. Answer a ticket by running the same command again with `--answer FINGERPRINT="yes"` and `--run-label 2`.

## Data-quality points to know before you trust a day

- **Export after the fact, honestly.** The system replays a day in order and only shows each moment what had been recorded by
  then. It cannot tell if an export was cleaned or back-filled later. Use snapshots as they were captured.
- **Snapshot spacing matters.** Quotes older than 60 seconds count as stale (a configurable data-quality assumption). With
  sparse snapshots most of the day is stale, so entries and exits will be blocked or pending. Capture a chain snapshot at
  least every minute during the 10:15 to 15:15 window if you want meaningful tests.
- **Index volume.** NIFTY spot has no traded volume; the volume column may stay empty.
- **Costs.** Costs use the dated, source-tracked rates in `config/`. The brokerage tariff in use is whatever the broker
  profile there says; check it against your broker.
- **Single days are anecdotes.** One day, or a few, tells you how the rules behaved, not whether the idea works.
