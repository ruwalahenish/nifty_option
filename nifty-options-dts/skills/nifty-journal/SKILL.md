---
name: nifty-journal
description: >-
  Show or verify the append-only paper trading journal and build a report that keeps fixture and real data apart and shows sample sizes. Paper-only, educational, not financial advice. Use when the user asks for the journal, a report, results or P&L.
argument-hint: "[journal path]"
---

# nifty-journal

**PAPER TRADING ONLY. Not financial advice.**

The journal is append-only and hash-chained; every NO TRADE, BLOCKED and approved decision is recorded. A fixture report states: Simulation on synthetic or model-priced data. This is not evidence of profitability.

```bash
python -m nifty_core.cli report --journal trader/journal/bullish_trend.jsonl
python -m nifty_core.cli verify-journal --journal trader/journal/bullish_trend.jsonl
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
