---
name: nifty-select
description: >-
  Read the market at a time in a fixture day and choose the best-fitting of four intraday defined-risk strategies (directional credit spread, directional debit spread, iron condor, iron butterfly), or say NO TRADE. Explains why, the trigger, strikes, risk and reward, exits and confidence. A proposal only. Paper-only, educational, not financial advice. Use when the user asks which strategy suits the market now.
argument-hint: "[scenario] [HH:MM]"
---

# nifty-select

**PAPER TRADING ONLY. Not financial advice.**

The strategy is not fixed in advance. The selector reads the market (trend or range, volatility, momentum, support and resistance), tests each of the four strategies against its own required conditions, and returns the best fit or **NO TRADE**. It never picks a strategy just to produce a trade.

```bash
python -m nifty_core.cli select-strategy --scenario bullish_trend --at 12:30 --profile intraday
```

| Market | Strategy it can choose |
|---|---|
| strong, clean trend with momentum, cheap-to-normal premium | Directional Debit Spread (bull call / bear put) |
| steadier trend, premium worth selling, short strike beyond support or resistance | Directional Credit Spread (bull put / bear call) |
| range-bound, volatility low-to-normal and not expanding, price mid-range | Iron Condor |
| tight, pinned, low-volatility day, price on the day average | Iron Butterfly |
| anything else, bad data, abnormal volatility, a HIGH-impact event, a trade already open, a KILL file | NO TRADE |

The output has: Market Condition, Selected Strategy, Why This Strategy (and why not the others), Trade Trigger, Trade Setup (strikes, max risk, max profit, risk-reward), Exit Rules (target, stop, time exit, early exits) and Confidence. Every strategy must also fit the per-trade risk limit with costs and slippage.

Useful options: `--json`, `--brief` (hide the per-condition table), `--position-open` (one trade at a time), `--recheck-at HH:MM` (re-run at the entry time; if the market changed, the original trade is dropped, never taken blindly).

Confidence counts how many independent checks agree. It is not a probability of profit. All four strategies are intraday and flat by the configured time; nothing is carried overnight. See `docs/STRATEGY_SELECTOR.md`.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. A selection is a proposal: nothing is entered or filled by it.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The thresholds are our own choices, not validated on market data.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
