---
name: nifty-paper
description: >-
  Show the ticket for one exact paper order and record the user's reply word for word, then run the simulated entry, management and exit for a fixture day. Needs an explicit 'yes' for that exact ticket. Paper-only, no real order, not financial advice. Use when the user wants to paper-trade, approve, or run a day.
argument-hint: "[scenario]"
---

# nifty-paper

**PAPER TRADING ONLY. Not financial advice.**

Nothing is entered without a reply that STARTS with "yes" for that one ticket: a question, a condition, a doubt, a batch or a standing instruction, or a number that is not on the ticket all fail. Ask the user; never type the yes yourself and never approve on their behalf. There is no flag to approve automatically.

```bash
python -m nifty_core.cli ticket --scenario bullish_trend --at 12:30
python -m nifty_core.cli run-day --scenario bullish_trend --answer FINGERPRINT="yes"
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
