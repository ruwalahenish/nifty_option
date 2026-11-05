# Live paper trading with a dashboard

PAPER only. Real Upstox quotes (read-only token), SIMULATED fills, nothing is ever sent to a broker, and the dashboard cannot
approve or change anything. Not financial advice.

**Entry mode.** `live_run` defaults to `--entry-mode auto`: a setup that passes every rule and fits the risk limits is entered
as a paper trade without asking you, and exits are automatic. Use `--entry-mode manual` to make every entry wait for your
"yes" (described below). The main `nifty_core.cli` tools still have no automatic approval and no flag for one.

## Start it (market hours, Mon-Fri 09:15-15:30 IST)

```
$env:UPSTOX_ANALYTICS_TOKEN = "..."          # an Analytics token: it cannot trade
python -m nifty_live.live_run --config my_real_config.json --dashboard
```

Open **http://127.0.0.1:8765/live**. `my_real_config.json` is a copy of `config/config.example.json` with
`"data_mode": "real_readonly"` (see `REAL_DATA.md`). Do not run `nifty_live.recorder` at the same time: `live_run` records the
same files (`data/recordings/<date>/chain.csv`, `candles.csv`) itself, so you can still replay the day afterwards with
`run-day`.

Without `--dashboard`, run `python tools/dashboard.py` in a second terminal instead; it reads the same state file.

## How live it is

- `live_run` polls Upstox every **10 s** by default (`--interval`, minimum 5 s). Each poll refreshes the spot, the option quotes,
  the unrealised P&L and the open-position marks. Snapshots are kept in memory, so fast polling stays cheap; every snapshot is
  still written to `data/recordings/<date>/chain.csv` for a later replay.
- The page re-fetches its content **every second** and swaps it in place: no reload, no flicker, and the "last data written N s
  ago" figure and the green dot keep moving. The dot turns red (and a banner appears) if the runner stops writing or the
  dashboard server cannot be reached. With scripts turned off it falls back to a plain 3-second reload.
- The one script on the page only does a `GET` of the page fragment; there are still no forms, buttons or inputs.
- Quotes and candles come from Upstox REST polling, not a streaming feed, so "live" means as fresh as the last poll.

## What the Live page shows

| Block | Meaning |
|---|---|
| NIFTY spot, quote age | latest recorded snapshot and how old it is |
| NIFTY chart | the day's price, every live tick, with the opening range, the long/short trigger levels and the day average drawn as lines (intraday profile) |
| Option chain | the 5 strikes either side of the money for the expiry in play: last, bid and ask for calls and puts, the at-the-money row in bold, and BUY / SELL marks on the legs of an open paper position |
| Day P&L / Unrealised / Realised | **unrealised** is shown from the first poll after an entry, refreshed every poll (default 30 s). It is a simulated mark to liquidation: long leg at bid minus slippage, short leg at ask plus slippage, minus the actual entry costs and estimated exit costs. It is labelled "not evidence of profitability". |
| Open paper position | both legs: paper entry price, current bid/ask, exit mark, per-leg P&L, and a gauge between the stop level and the profit-target level (from `config/position_thresholds.json`) |
| Ticket waiting for your reply | the exact ticket and the one command that approves it |
| Latest decision, events, closed trades, P&L curve | what the engines decided and why, and what happened since the start |

If the runner stops writing for two minutes the page says **NOT UPDATING** and shows the last known state. If a quote is
missing, stale or one-sided the mark shows "unavailable" with the reason; a price is never invented.

## Entry rules: profiles

`live_run` now defaults to `--profile intraday`: an opening-range breakout entered on a retest, described in
`INTRADAY_PLAYBOOK.md` (rules, daily routine and review checklist). The older `--profile human` (vote-based trend, below) and
`--profile default` (strict) are still available.

### The "human" profile

The original thresholds were deliberately strict (a trend needed 60 minutes of candles and a 0.30% move from the open, with
every filter passing), so on most days nothing qualified. `--profile human` (`config/profiles/human/`) reads the tape the way a discretionary
trader does. Use `--profile default` for the strict reference rules (the replay tools and tests use those unless you pass
`--profile`).

| Rule | strict (default files) | human |
|---|---|---|
| Candles before a direction is called | 12 (10:15) | 9 (10:00) |
| Move from the open that counts as direction | 0.30% | 0.15% |
| Day-range zones (bull above / bear below) | 65% / 35% | 60% / 40% |
| Momentum / structure windows | 4 of last 6 / 12 candles | 3 of last 5 / 9 candles |
| "Too volatile" brakes (one bar / stdev) | 0.50% / 0.10% | 0.80% / 0.15% |
| Spread widths tried | 100, 150 | 50, 100, 150 |
| Long strike out of the money | 0-1 steps | 0-2 steps |
| Option liquidity (bid-ask / volume / OI) | 3% / 5,000 / 20,000 | 5% / 2,000 / 10,000 |

To start entries at 10:00, set `"entry_window_start_ist": "10:00"` in your config (the human profile classifies from then).

Unchanged, in every profile: still 3 of 4 evidence items agreeing with none against, no trade on sideways / high-volatility /
no-data days, no trade around HIGH-impact events, fresh two-sided quotes, debit at most 65% of the width, the per-trade risk
limit (1% of capital), the daily loss limit, 2 trades a day, exits, the kill switch, paper only, and your "yes" for every entry.

**Sized to the budget.** The strategy ranks the spreads that pass (lowest bid-ask friction first). The pipeline now offers them
to the risk gate in that order and takes the first one that fits the per-trade limit, instead of giving up when the
top-ranked (usually the widest) is too big. The journal records which higher-ranked spreads were passed over.

## How a trade happens

In **auto** mode (default) steps 2-4 are skipped: the ticket is journaled, an `APPROVAL` record with `source`
`auto_paper_entry` (and no human reply) is written, and the paper entry follows at once. Everything else below is the same:
the strategy rules, the per-trade and daily risk limits, 2 trades a day, one position at a time, the kill switch (create
`trader/KILL` to stop new entries; open positions are still managed and closed) and the simulated fill checks. Manual mode:

1. Every five minutes (slots from 10:15 to the close, as in a replay) the pipeline runs: regime, strategy, risk gate.
2. If a candidate passes, a **ticket** appears on the page and in the runner's terminal, with its 16-character fingerprint.
3. You approve that exact order in another terminal:
   `python -m nifty_live.live_run answer FINGERPRINT yes`
   Your reply is recorded word for word. Only a reply that starts with "yes" and matches the ticket counts (same rules as
   `--answer` in `run-day`).
4. The reply is applied within a second, but only while the approval is fresh: `data_stale_after_seconds` (60 by default)
   after the ticket was made. A late reply is refused (`APPROVAL_STALE`) and an unanswered ticket is released. Nothing is
   ever entered automatically.
5. After entry the position is managed at each slot with the same exit rules as a replay (stop, target, regime change, data
   safety, flat-by 15:15). The `KILL` file in `trader/` still blocks new entries.

## Differences from a replay, on purpose

- Decisions and exits happen once per slot, at the real time the slot is processed. Between slots only the display refreshes,
  so a stop level can be crossed on screen a few minutes before the exit happens.
- A ticket waits at most one approval window. In a replay it is answered at its own decision time.
- Journal: `trader/journal/live_<date>.jsonl`, same hash chain and `report`/`verify-journal` tools. New record kind `MARK`
  (unrealised P&L at each slot while a position is open) and follow-up `DECISION` records (`ENTERED`, `DECLINED`,
  `ENTRY_NOT_FILLED`, `TICKET_EXPIRED`) that resolve a ticket.
- The journal report still hides realised statistics until 30 completed real paper trades; the Live page is a live view, not
  a performance statement.

## Limits to know

- **Restarting is safe, but a position left open is not resumed.** If `live_run` stops (Ctrl+C, a crash, a closed laptop, a
  killed process) while a paper position is open, nothing manages it. When you start `live_run` again the same day it reads
  today's journal, closes that position **administratively** at its last recorded mark (or at minus its entry costs if no
  mark was recorded), journals it as an `EXIT` with reason `RUNNER_STOPPED` and `administrative: true` (not a simulated exit
  fill), and carries every earlier trade of the day into the risk ledger. So the 2-trades-a-day and daily-loss limits and the
  "this spread was already traded" check continue; restarting cannot reset them. The Live page lists such trades with their
  reason. Do not run two sessions at once on the same journal.
- Polling is REST every 30 s, not a websocket stream. Sparse or missing snapshots make quotes stale and the mark unavailable.
- Real quotes can be wider and thinner than the fill model assumes; fills here are optimistic about size.

## Preview without the market

```
python -m nifty_live.live_run --replay data/recordings/2026-11-05 --config my_real_config.json --dashboard --speed 1
python -m nifty_live.live_run --replay bullish_trend --dashboard          # synthetic fixture, fixture-mode config
```

A replay uses a simulated clock (the page says so), writes its own `liveview_*.jsonl` journal, and takes your reply the same
way (`answer`). The synthetic fixtures only have four chain snapshots a day, so the mark is blank between them; use a real
recording to see it move. With the default fixture config the risk gate usually blocks the fixture candidates; the tests use a
larger virtual capital.
