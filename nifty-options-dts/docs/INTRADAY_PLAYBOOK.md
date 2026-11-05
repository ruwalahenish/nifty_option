# Intraday playbook: opening-range breakout, entered on a retest

PAPER only. Real Upstox quotes, simulated fills, no real order is ever sent. Not financial advice. The rules below are my
reasoning, not tested on enough data to claim an edge: use them to collect evidence, not to expect profit.

## The idea in one paragraph

The first 30 minutes (09:15-09:45) set an **opening range**. A close beyond the range is a **breakout**. Most breakouts
that run straight away are chased at the worst price, so the setup waits for price to **pull back to the broken level**
and then **resume**. If the pullback fails (price closes back inside the range) there is no trade. The trade itself is the
same defined-risk debit spread as before (bull call spread on an up-breakout, bear put spread on a down-breakout), sized by
the existing risk gate.

## The setup rules (config/profiles/intraday/setup_thresholds.json)

For an up-breakout (a down-breakout is the mirror image):

| # | Rule | Value |
|---|---|---|
| 1 | Opening range complete | first 6 five-minute candles, at least 0.10% wide |
| 2 | Breakout | a 5-minute close above the range high plus 0.03% |
| 3 | Intact | every close since stays above the range high (a close back inside cancels it; a later breakout is new) |
| 4 | Not chasing | last close at most 0.20% above the range high |
| 5 | Retest | after the breakout candle, a low within 0.05% of the range high |
| 6 | Resumption | the last candle closes up |
| 7 | Side of day | last close above the day's average price (no volume on the index, so a plain average, not VWAP) |
| 8 | Time | no new setup after 13:30 |

On top of that, unchanged: the regime engine's data gates, its volatility brake and its SIDEWAYS verdict still win (no trade
on a missing-data, too-volatile or going-nowhere day), HIGH-impact event days are blocked, option quotes must be fresh and
liquid, the spread must fit the INR 2,500 per-trade limit (the system picks the best-ranked one that fits), at most 2 trades
a day, and by default a qualifying setup is entered **automatically** as a paper trade (`--entry-mode auto`; use `--entry-mode manual`
to require your "yes" for each entry).

## The day, step by step

**Before 09:15**
- Open a terminal in `nifty-options-dts`, set `$env:UPSTOX_ANALYTICS_TOKEN` (a new terminal after `setx`).
- Check `my_real_config.json`: `"data_mode": "real_readonly"` and `"entry_window_start_ist": "09:45"`.
- Make sure there is no `trader/KILL` file (its presence blocks entries on purpose).

**09:15** Start: `python -m nifty_live.live_run --config my_real_config.json --dashboard`, then open
http://127.0.0.1:8765/live. (It refuses to start before 09:15 or on a non-trading day.) Do not run the plain recorder too.

**09:15-09:45 Opening range forms.** Nothing to do. The page says the system is waiting for its first slot.

**09:45-13:30 Scan.** Every 5 minutes the *Latest decision* card tells you exactly what it is waiting for:
1. `waiting: no 5-minute close above the opening-range high ...` : no breakout yet.
2. `waiting: no pullback to the broken level ...` : it broke out; do not chase, the system will not either.
3. `waiting: the last candle closed down, not up` : it pulled back, now it needs a resuming candle.
4. `price is back inside the opening range (the ... breakout failed)` : cancelled, waiting for a fresh breakout.
5. A **ticket** appears : breakout + retest + resumption all happened and a spread fits the limit.

**Automatic mode (default).** When the setup qualifies and a spread fits the risk limit, the system enters the paper trade by
itself. The Live page shows the position and its unrealised P&L within a poll, and the journal records the entry as
`auto_paper_entry`. To stop new entries at any time create an empty file `trader/KILL` (open positions are still managed and
closed). Your job is to watch, not to approve.

**Manual mode (`--entry-mode manual`): when a ticket appears (you have 60 seconds)**
Read the ticket, then answer only if you agree. Quick checklist:
- Does the direction match what you see on your own chart (not just the system)?
- Is the defined risk on the ticket within what you are happy to lose (it is capped at INR 2,500)?
- Is there news or an event in the next hour that the schedule would not know about?
- If unsure, do nothing: an unanswered ticket is released and costs nothing.
To approve: `python -m nifty_live.live_run answer <fingerprint> yes`

**After entry** the system manages the paper position by itself at each 5-minute slot, and the Live page shows the unrealised
P&L every poll. Exits are automatic: profit target (+60% of the defined risk), stop (-50%), **a failed breakout (a 5-minute
candle closes back inside the opening range)**, the regime flipping against you, three bad-data checks, or flat by 15:15. Do not add to it, average down or move the stop: the system has no such command by design.

**After 13:30** no new setup is taken. Open positions are still managed. **15:15** forced flat. **15:30** the session ends
and the journal is closed.

## End-of-day review (5 minutes, every day)

```
python -m nifty_core.cli report --journal trader/journal/live_<date>.jsonl
python -m nifty_core.cli verify-journal --journal trader/journal/live_<date>.jsonl
```
Write down, per trade: the setup time, the ticket's defined risk, the exit reason, net P&L after costs, and whether the
breakout was clean or marginal. Per day without a trade: which rule stopped it (the *Latest decision* history shows it). After
about 30 completed trades the report will show P&L; before that it deliberately does not.

## Today (2026-11-05) as a worked example

Opening range 24,296.30-24,391.95 (0.39% wide). Breakout level 24,399.27.
- 09:45 candle closed 24,413.10: **breakout**.
- 09:50 candle closed 24,437.80 (0.19% above the level): inside the not-chasing limit but no pullback yet: **wait**.
- 09:55 candle touched 24,401.55 (the **retest**) but closed down at 24,402.50: no resuming candle: **wait**.
- 10:00 candle dipped to 24,384.60 and closed 24,400.15 down again: **wait**.
- 10:05 candle closed 24,373.60, back inside the range: **breakout failed, setup cancelled**.
No trade all morning. A long entered on the breakout would have been underwater within 20 minutes. That is the point of the
retest rule, but it is **one day**, not proof.

## Tuning (change one thing at a time, and note the date)

| Want | Change in `setup_thresholds.json` |
|---|---|
| More trades | `"require_retest": false` (enter on the breakout itself: chases more), or `max_extension_pct` up |
| Fewer, cleaner | `breakout_buffer_pct` up to 0.05, `min_range_pct` up to 0.20 |
| Later trades | `latest_setup_ist` (the afternoon is slower and options lose time value) |
| Earlier | `opening_range_candles` 3 (15-minute range; needs `min_bars` 6 in the regime file, so the first slot stays 09:45) |

Compare against the other rule sets with `--profile human` (trend votes) or `--profile default` (strict reference).

## What this does not do

- The failed-breakout exit uses a plain close back inside the range, so a one-candle whipsaw can stop you out of a trade that
  would have recovered.
- Index candles have no volume, so there is no volume confirmation and no true VWAP.
- Exits are checked every 5 minutes, and the simulated fills are optimistic about size.
- Nothing here has been shown to make money. Judge it by net P&L per trade after costs over many days.
