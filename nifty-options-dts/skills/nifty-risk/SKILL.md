---
name: nifty-risk
description: >-
  Check whether a candidate is APPROVED or BLOCKED by the risk engine, showing Total Defined Risk (net debit + entry costs + estimated exit costs + slippage) against the limit. Paper-only, educational, not financial advice. Use when the user asks if a trade fits the risk limits.
argument-hint: "[scenario] [HH:MM]"
---

# nifty-risk

**PAPER TRADING ONLY. Not financial advice.**

The risk engine never changes strikes, prices or size to make a trade fit. A stop-loss does not reduce the defined risk. BLOCKED is a normal, expected answer: with the default settings every fixture candidate is blocked on the per-trade limit.

```bash
python -m nifty_core.cli risk --scenario bullish_trend --at 12:30
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
