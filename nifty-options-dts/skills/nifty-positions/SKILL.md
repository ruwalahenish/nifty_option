---
name: nifty-positions
description: >-
  Explain how open paper positions are managed (HOLD or CLOSE only): exit reasons, forced flat-by at 15:15 IST, pending exits, and critical exceptions at the session end. Paper-only, educational, not financial advice. Use when the user asks about exits, stop-loss, profit target, flat-by or open positions.
argument-hint: "[scenario]"
---

# nifty-positions

**PAPER TRADING ONLY. Not financial advice.**

Exit reasons: forced flat-by, manual paper exit, data safety, stop-loss, regime invalidation, profit target (thresholds are unvalidated assumptions). No averaging, extra lots, rolling or extra legs. A position still open at the session close is a critical exception and is never shown as closed.

```bash
python -m nifty_core.cli run-day --scenario bullish_trend --config CONFIG --answer FINGERPRINT="yes"
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
