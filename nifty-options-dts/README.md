# nifty-options-dts

A NIFTY intraday options **paper-trading** system for defined-risk debit spreads (Bull Call Debit Spread, Bear Put Debit Spread). It is an educational project.

**No live trading.** Version 1 has no capability to place a real order, now or through any future data-provider interface. Paper mode cannot be switched off by config, environment variable or command line.

**Not financial advice.** Nothing here is a recommendation, and a simulation or fixture result is not evidence of profitability. The default virtual capital (₹2,50,000) is a simulation setting, not a suggestion for real money.

Status: under construction, see [PLAN.md](PLAN.md). Phases P0 to P10 are implemented: scaffold, safety guards, configuration, versioned contract layer, IST clock, NSE calendar and expiry rules, the Indian options cost model, the market-data models with a read-only fixture provider and synthetic scenarios, the two-leg debit spread model, the market regime engine, and the strategy engine (candidate or NO TRADE; no sizing, no orders), the risk engine (APPROVED or BLOCKED), simulated paper execution, position management (HOLD or CLOSE), and the skills, command line, approval step, append-only journal and end-to-end offline run (P10). Real-data collection (P12) is blocked until a read-only data source is chosen.

Based on the design of the original [day-trader-skill](../day-trader-skill), which is kept unchanged as a read-only reference (a test checks this).

## Run the tests

```bash
python -m unittest discover -t . -s tests -v
```

Standard library only, Python 3.8+. Tests run offline and fail if they open a network connection.

## Exchange values

Lot size, expiry rules, tick size and costs live in `config/contracts/nifty_contract_specs.json`, each with a status, a source and a verification date. See `docs/VERIFICATION_LOG.md`. Values not confirmed from a primary source are marked `UNVERIFIED` and are usable only in fixture mode; non-fixture mode refuses them. The cost model is described in `docs/COSTS.md`.

## Safety statement

- **Paper only.** There is no live trading, no broker connection, no credential and no order API anywhere in this project. Fills are simulated from quotes with adverse slippage. Paper mode cannot be turned off by configuration, environment variable or command-line flag.
- **Not financial advice.** Nothing here is a recommendation. The default paper settings (virtual capital, 1% risk per trade, 2% daily loss, 1 lot, 2 trades a day) are simulation settings and are not a recommendation for real money.
- **Fixture results are not evidence.** Runs on the synthetic fixtures check that the logic works. "Simulation on synthetic or model-priced data. This is not evidence of profitability."
- **A human "yes" for every order.** A paper order is entered only after a reply that starts with "yes" for that exact ticket, recorded word for word. The tool cannot tell a person from a script that supplies the reply.
- Risk limits are read when a run starts and are never loosened by the pipeline. A candidate that does not fit is BLOCKED, never adjusted.

## Skills and the command line

The skills in `skills/` are thin front-ends over `nifty_core`. They are installed **as a whole plugin** (`.claude-plugin/plugin.json`), because they share the `nifty_core` package; an individual skill cannot be copied out on its own. Run the commands from the project root.

| Skill | What it does |
|---|---|
| `nifty-regime` | describe the market regime at a time |
| `nifty-strategy` | propose a debit-spread candidate or NO TRADE |
| `nifty-risk` | APPROVED or BLOCKED, with the defined-risk arithmetic |
| `nifty-paper` | show the ticket, record your reply, run the simulated day |
| `nifty-positions` | how open positions are managed and exited |
| `nifty-journal` | verify the journal and build a report |

```bash
python -m nifty_core.cli regime --scenario bullish_trend --at 12:30
python -m nifty_core.cli candidate --scenario bullish_trend --at 12:30
python -m nifty_core.cli risk --scenario bullish_trend --at 12:30
python -m nifty_core.cli ticket --scenario bullish_trend --at 12:30
python -m nifty_core.cli run-day --scenario bullish_trend --journal trader/journal/bullish.jsonl
python -m nifty_core.cli report --journal trader/journal/bullish.jsonl
python -m nifty_core.cli verify-journal --journal trader/journal/bullish.jsonl
```

Scenarios are the fixture names in `tests/fixtures/scenarios` (or a path). See [docs/END_TO_END.md](docs/END_TO_END.md) for the approval flow and the journal.

## Documents

[docs/REGIME_ENGINE.md](docs/REGIME_ENGINE.md), [docs/STRATEGY_ENGINE.md](docs/STRATEGY_ENGINE.md), [docs/RISK_ENGINE.md](docs/RISK_ENGINE.md), [docs/PAPER_EXECUTION.md](docs/PAPER_EXECUTION.md), [docs/POSITION_MANAGEMENT.md](docs/POSITION_MANAGEMENT.md), [docs/END_TO_END.md](docs/END_TO_END.md), [docs/COSTS.md](docs/COSTS.md), [docs/DATA_MODEL.md](docs/DATA_MODEL.md), [docs/SPREAD_MODEL.md](docs/SPREAD_MODEL.md).
