---
name: nifty-strategy
description: >-
  Propose one NIFTY debit-spread candidate (Bull Call or Bear Put) or NO TRADE with reasons, for a time in a fixture day. A candidate is only a proposal. Paper-only, educational, not financial advice. Use when the user asks for a trade idea, candidate or spread.
argument-hint: "[scenario] [HH:MM]"
---

# nifty-strategy

**PAPER TRADING ONLY. Not financial advice.**

BULLISH looks for a Bull Call Debit Spread, BEARISH for a Bear Put Debit Spread; every other regime is NO TRADE. A valid spread is not enough: liquidity, staleness and data filters can still give NO TRADE. The candidate is not sized or risk-checked.

```bash
python -m nifty_core.cli candidate --scenario bullish_trend --at 12:30
```

Add `--json` for machine-readable output. Scenarios are fixture names (for example `bullish_trend`, `bearish_trend`, `sideways`, `high_volatility`, `illiquid_options`, `missing_invalid_data`) or paths to scenario files.

## Safety statement

- **Paper-only.** This skill never places a real order and has no broker connection, credential or live mode. Fills are simulated.
- **Not financial advice.** Results are educational. On the synthetic fixtures they are a simulation and not evidence of profitability. The default paper settings (virtual capital, 1% per trade, 2% daily loss, 1 lot) are not a recommendation for real money.
- Every output is labelled PAPER, and SIMULATED when the data is synthetic.
- Run commands from the project root (the folder holding `nifty_core`). The skills are installed as a whole plugin; copying one skill alone does not work, because they share `nifty_core`.
