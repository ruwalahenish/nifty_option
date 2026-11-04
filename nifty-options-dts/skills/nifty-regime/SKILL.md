---
name: nifty-regime
description: >-
  Describe the NIFTY market regime (BULLISH, BEARISH, SIDEWAYS, HIGH_VOLATILITY, NO_DATA, NO_TRADE) at a given time from completed 5-minute candles, with the evidence. Paper-only, educational, not financial advice. Use when the user asks what the NIFTY regime or trend is at a time in a fixture day.
argument-hint: "[scenario] [HH:MM]"
---

# nifty-regime

**PAPER TRADING ONLY. Not financial advice.**

The regime engine describes the market from candles that had finished at the decision time; it does not predict. Its evidence score is a model summary, not a probability of profit. It classifies nothing before 12 completed candles (10:15 IST at the defaults).

```bash
python -m nifty_core.cli regime --scenario bullish_trend --at 12:30
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
