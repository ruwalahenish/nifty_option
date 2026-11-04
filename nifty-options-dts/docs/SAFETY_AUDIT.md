# P11 Safety audit

PAPER trading only. Not financial advice. This audit checks the 15 guard requirements in PLAN.md section 6 against the
code and tests as they stand after P10. It reports what is covered, what differs from the plan, and what remains a risk.

## Result by requirement

| # | Requirement | Where it is enforced | Tests | Status |
|---|---|---|---|---|
| 1 | No order-capable API | `nifty_core/data/provider.py` (six getters only) | `tests/safety/test_provider_readonly.py` | Covered |
| 2 | Static scan for order/credential patterns | scan helper used by the tests | `tests/safety/test_static_scan.py` | Covered |
| 3 | Immutable paper mode (config, env, CLI) | `nifty_core/guards/paper_only.py`, `config.py`, CLI has no live flag | `tests/safety/test_paper_guard.py`, `test_config_paper_only.py`, `tests/integration/test_cli.py` | Covered |
| 4 | Execution only via the paper engine with a valid approval | `execution/paper.py`, `risk/engine.py` (`verify_entry`, fingerprints) | `tests/unit/test_execution.py`, `test_risk.py` | Covered |
| 5 | Human approval; a scheduled run cannot approve | `approval.py`, pipeline requires a matching reply | `tests/unit/test_journal_approval.py`, `tests/integration/test_pipeline.py` | **Partly**: see residual risk R1 |
| 6 | Naked-short guard | `market/spread.py`, risk and execution re-check | `tests/unit/test_spread.py`, `test_risk.py`, `test_execution.py` | Covered |
| 7 | Intraday only, flat-by enforced by the engine | `positions/manager.py`, `pipeline.py` | `tests/unit/test_positions.py`, `tests/integration/test_pipeline.py` | **Differs**: see D1 |
| 8 | No averaging | `risk/engine.py`, `positions/manager.py` | `tests/unit/test_risk.py`, `test_positions.py`, `tests/safety/test_config_paper_only.py` | Covered |
| 9 | Limits cannot be loosened at runtime | config read once; approval carries a config fingerprint; `RUN_START` journals it | `tests/unit/test_risk.py`, `test_execution.py` | Covered |
| 10 | Kill switch | `guards/paper_only.py`, risk engine, executor | `tests/safety/test_paper_guard.py`, `tests/integration/test_cli.py` | **Differs**: see D2. **Fixed in P11**: see F1 |
| 11 | Stale or missing data blocks entry | `data/validation.py`, strategy and executor | `tests/unit/test_data_validation.py`, `test_strategy.py`, `test_execution.py` | Covered |
| 12 | No credentials in the repo | static scan, config check | `tests/safety/test_static_scan.py`, `test_repo_hygiene.py` | Covered |
| 13 | Network tripwire in tests | `tests/_base.py` (`NoNetworkTestCase`) | `tests/safety/test_paper_guard.py` | Covered |
| 14 | Labelled outputs | CLI banner, report caveat | `tests/integration/test_cli.py`, `tests/unit/test_journal_approval.py`, `tests/safety/test_skills_docs.py` | Covered |
| 15 | Upstream untouched | `tools/upstream_hash.py` | `tests/safety/test_upstream_unchanged.py` | Covered |

## Finding fixed in this phase

**F1. The CLI could miss the kill switch.** `--kill-dir` defaulted to `trader`, relative to the folder the command was run
from. Running from another folder, or passing `--kill-dir` pointing somewhere else, meant a `KILL` file in the project's
`trader/` folder was not seen, so entries were not blocked. Fix: `cli._kill_dir` now always checks the project's own
`trader/` folder first; if a `KILL` file is there it wins over the flag. The flag can add a place to look but cannot remove
one. Two tests in `tests/integration/test_cli.py` cover it.

**F2 (found while building P12). Real-data days would have been NO_DATA.** The regime engine refuses non-fixture modes
unless it is told the data label, and the pipeline, position manager and CLI did not pass it. Under `real_readonly` every
decision would have been NO_TRADE, which is safe but wrong. The label is now passed through. It fails closed: a provider that
cannot state its label is treated as unverified in real mode.

**F3 (P12). The CLI could read real data under the fixture mode, or synthetic data under the real mode.** The CLI built its
context with the fixture mode whatever the config said. Now the config's `data_mode` is used, and a label/mode mismatch is
refused before anything runs.

## Differences from the plan (approved earlier, listed for completeness)

- **D1. Forced flat-by is attempted, never silently done.** The plan said any open spread is force-closed at flat-by. You
  approved a stricter version at P9: a pending forced exit is retried while the market is open and, if it cannot fill by
  session close, is reported as a critical exception. A position is never marked closed without a fill.
- **D2. The kill switch blocks entries but does not force a flatten.** The plan said it "triggers flatten". At P8 you
  approved: new entries blocked, exits still allowed after checking current data. Say so if you want forced flatten.

## Residual risks (not fully closable in code)

- **R1. A script can supply the "yes".** The tool cannot tell a person from a program passing `--answer`. It does require the
  exact ticket fingerprint, a reply that starts with "yes", and no question, condition or mismatched number, and it records the
  reply word for word. Do not wire `--answer` into a scheduler or an automated agent.
- **R2. The journal is tamper-evident, not tamper-proof.** Someone with file access can rewrite the whole file with a fresh
  chain. Keep copies if the history matters.
- **R3. Risk state is rebuilt per run.** Daily limits (2 trades, 5,000 loss budget) are enforced within one run. For
  recorded real data this is closed in P12 by refusing a second run of a day that already has an entered paper trade
  (`DAY_ALREADY_TRADED`). For fixture data re-runs are allowed on purpose (they are replays). Limits are still not carried
  across different days, which is correct because they are daily.
- **R4. Fixture results prove logic, not profitability.**

## Checks run for this audit

- Full suite, four time zones; fixture regeneration check; upstream hash check (see `docs/VERIFICATION_LOG.md`).
