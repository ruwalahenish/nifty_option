# nifty-options-dts: PLAN

Status: plan APPROVED with the decisions below. Implementation proceeds phase by phase, with a report and your go-ahead between phases.

| Phase | State |
|---|---|
| P0 Scaffold and verification | done (see docs/VERIFICATION_LOG.md) |
| P1 Safety guards, config, contract layer | done |
| P2 Market core and costs | done and approved (docs/VERIFICATION_LOG.md, docs/COSTS.md) |
| P3 Data models and fixture provider | done and approved (docs/DATA_MODEL.md) |
| P4 Spread / instrument model | done and approved (docs/SPREAD_MODEL.md) |
| P5 Regime engine | done and approved (docs/REGIME_ENGINE.md) |
| P6 Strategy engine | done and approved (docs/STRATEGY_ENGINE.md) |
| P7 Risk engine | done and approved (docs/RISK_ENGINE.md) |
| P8 Paper execution | done and approved (docs/PAPER_EXECUTION.md) |
| P9 Position management | done and approved (docs/POSITION_MANAGEMENT.md) |
| P10 Skills, CLI, approval, journal, end-to-end | done, awaiting your review (docs/END_TO_END.md). The journal and reports originally planned as P9 were built here |
| P11 Final safety audit | done, awaiting your review (docs/SAFETY_AUDIT.md). One gap fixed: the CLI could miss the project KILL file |
| P12 Real-data paper collection | done for your own exported files, awaiting your review (docs/REAL_DATA.md). No vendor provider built: none chosen |

### Approved decisions (override anything below that conflicts)

1. **Risk gate:** Total Defined Risk = net debit + entry costs + estimated exit costs + configured slippage (the stricter figure; entry risk alone is not used). The gate is never tuned to fit trades. A strategy stop-loss is for trade management only and never reduces the capital-at-risk figure.
2. **Paper capital:** ₹2,50,000. Risk per trade 1% = ₹2,500. Max daily loss 2% = ₹5,000. 1 lot. 2 trades per day. A simulation setting, not advice.
3. **Architecture:** whole plugin/project, `nifty_core` as the central domain layer. `day-trader-skill` stays unchanged.
4. **Contract values:** lot size, expiry schedule, charges and similar are never hard-coded in logic. They live in a versioned, source-tracked contract layer (`config/contracts/`, `nifty_core/contracts.py`). Fixture-only values must be marked UNVERIFIED with source, verification date and effective date where applicable.
5. **Safety:** V1 is paper-only with no capability to place a live order, including via future data-provider interfaces.
Reference (unchanged, read-only): `../day-trader-skill`

## 0. Objective and hard constraints

A NIFTY intraday options **paper-trading** system focused on capital protection and defined-risk structures. Educational use only.

Hard constraints (each is enforced by code and by a test, see section 6):

1. No live trading in Version 1. No code path may place a real order.
2. Only two strategies: Bull Call Debit Spread, Bear Put Debit Spread. Everything else is NO TRADE.
3. No naked short options, no uncovered positions, no averaging down, no overnight positions.
4. The system may return NO TRADE or BLOCKED. It never forces a trade to fit the limits.
5. The original `day-trader-skill` repo is never modified.
6. Model-priced results are logic validation, not evidence of profitability.

Defaults (all in config, see section 8 for the problems with them):

| Setting | Value |
|---|---|
| Virtual capital | ₹2,50,000 |
| Max position | 1 NIFTY lot |
| Max trades per day | 2 |
| Risk target per trade | 1% of capital (₹2,500), measured as net debit + costs + slippage |
| Max daily loss | 2% of capital (₹5,000) |
| Positions | closed intraday, flat-by time configurable (default 15:15 IST) |
| Averaging into losers | forbidden |

## 1. Final architecture

A central domain package, `nifty_core`, holds all logic. Skills are thin, replaceable front-ends that call it. The pipeline is one-directional; each stage consumes immutable data from the stage before.

```
 MarketDataProvider (read-only) ──► RegimeEngine ──► StrategyEngine ──► RiskEngine ──► PaperExecutionEngine
        │                               │                 │                │                  │
   fixtures / (later) broker-RO      regime +          SpreadCandidate   ALLOW/BLOCK +    fills (simulated)
                                     reasons           or NO_TRADE       reasons               │
                                                                                     PositionTracker (P&L)
                                                                                              │
                                                                                      TradingJournal (append-only)
```

### 1.1 Components

1. **Market data interface** (`nifty_core/data`). A `MarketDataProvider` abstract class with read-only methods only: `get_clock()`, `get_underlying_bars(interval, start, end)`, `get_option_chain(expiry, as_of)`, `get_quote(instrument, as_of)`, `get_expiries(as_of)`, `get_event_calendar(date)`. It has no method that accepts an order. Implementations: `FixtureProvider` (V1) and a documented slot for a future `ReadOnlyBrokerProvider`. Every returned object carries `as_of` and `source`; stale data is rejected by the risk engine.
2. **Market regime engine** (`nifty_core/regime`). Pure function: `classify(bars, context) -> RegimeResult(label, confidence, reasons[])`. Labels: `BULLISH`, `BEARISH`, `SIDEWAYS`, `CONFLICTED`, `UNKNOWN`. Inputs (index only, no volume): opening range and breakout state, EMA slope/alignment, ATR/range expansion, gap vs previous close, time of day, expiry-day flag, event flag. Rule: anything other than a clear, agreeing BULLISH or BEARISH reading is NO TRADE.
3. **Strategy engine** (`nifty_core/strategy`). `propose(regime, chain, config) -> SpreadCandidate | NoTrade(reasons)`. BULLISH builds a Bull Call Debit Spread, BEARISH builds a Bear Put Debit Spread. Applies strike selection, width, minimum days to expiry (or expiry-day rule), leg liquidity filter (bid/ask spread, OI), and a debit-fits-risk filter. If no valid one-lot spread fits the risk limit, it returns NO TRADE with the reason and the cheapest alternative it found.
4. **Spread / instrument model** (`nifty_core/instruments`). Immutable, validated objects (section 1.2).
5. **Risk engine** (`nifty_core/risk`). `evaluate(candidate_or_order, account, state, clock, config) -> RiskVerdict(ALLOW|BLOCK, reasons[], fingerprint)`. Includes the naked-short guard (section 1.3). It runs on the entry, and again on every exit and adjust request.
6. **Paper execution engine** (`nifty_core/execution`). Accepts only a `RiskVerdict(ALLOW)` whose fingerprint matches the order. Simulates fills for the whole spread atomically (both legs or neither) with configurable slippage and the full Indian cost model. Has no network code.
7. **Position and P&L tracking** (`nifty_core/portfolio`). Open spreads, mark-to-market from chain quotes, realised/unrealised P&L gross and net of costs, daily loss counter, trade counter.
8. **Trading journal** (`nifty_core/journal`). Append-only JSONL. One record per decision, including NO TRADE and BLOCKED, with inputs hash, regime, candidate, verdict, fills, exit reason, P&L and data source (`fixture` vs `real`). This is how real paper-trading evidence is collected later.

Supporting modules: `nifty_core/market` (IST clock, NSE holidays, expiry calendar, session bars), `nifty_core/costs` (brokerage, STT, exchange charges, SEBI fee, GST, stamp duty), `nifty_core/pricing` (Black-76 model for fixtures and the Time Machine only), `nifty_core/guards` (paper-only guards, section 6), `nifty_core/config`.

### 1.2 Spread model

`OptionLeg` (frozen): `underlying`, `expiry`, `option_type` (CE|PE), `strike`, `side` (BUY|SELL), `lot_size`, `lots`, `entry_price`.

`DebitSpread` (frozen) has exactly two legs and derives:

| Field | Bull Call Debit | Bear Put Debit |
|---|---|---|
| Long leg | CE, lower strike K1, BUY | PE, higher strike K2, BUY |
| Short leg | CE, higher strike K2, SELL | PE, lower strike K1, SELL |
| `spread_width` | K2 − K1 | K2 − K1 |
| `net_debit` (points) | long entry − short entry | long entry − short entry |
| `max_loss` (₹) | net_debit × lot_size × lots | same |
| `max_profit` (₹) | (width − net_debit) × lot_size × lots | same |
| `breakeven` | K1 + net_debit | K2 − net_debit |

Construction invariants (violation raises, never silently fixes): same underlying, expiry and option type; equal quantity; exactly one BUY and one SELL; correct strike ordering for the strategy; `0 < net_debit < width`; strikes on the 50-point grid; entry prices on the ₹0.05 tick; lot size from config, not hard-coded. Entry prices for sizing are conservative: buy at ask, sell at bid. `max_loss` is the theoretical maximum, before costs. A separate `max_loss_with_costs` adds estimated round-trip costs and slippage.

### 1.3 Naked-short rejection (risk engine, mandatory)

An order or position is BLOCKED if, at any time, any short leg is not fully covered. Coverage means a long leg exists with: same underlying, same expiry, same option type, quantity ≥ the short quantity, and a strike at least as protective (call: long strike ≤ short strike; put: long strike ≥ short strike). Additional rules:

- Any SELL-to-open outside a validated two-leg debit spread is blocked.
- A single-leg order is blocked unless it is a BUY-to-open that is not part of a spread (disabled in V1) or a spread exit.
- **Exit ordering:** a spread is closed atomically. If the engine ever has to close legs separately, the short leg is bought back first or at the same instant. Closing the long leg while the short remains is always blocked.
- Partial fills are not simulated in V1; a spread fills fully or not at all. If a future provider reports partial fills, the position is flagged as naked-risk and the journal records an incident.
- Evaluated on the entry, on every exit request, and on every portfolio reconciliation.

## 2. Folder structure

```
nifty-options-dts/
├── PLAN.md
├── README.md
├── LICENSE                         (MIT, as upstream)
├── pyproject.toml                  (stdlib-only runtime; dev: none required)
├── .claude-plugin/{plugin.json,marketplace.json}
├── nifty_core/
│   ├── __init__.py
│   ├── config.py                   (schema, defaults, validation, immutability of paper flag)
│   ├── errors.py
│   ├── guards/
│   │   ├── paper_only.py           (runtime guard, kill switch)
│   │   └── forbidden.py            (forbidden-identifier list used by tests)
│   ├── market/
│   │   ├── clock_ist.py            (UTC+5:30, no DST; 09:15-15:30; 75 five-minute bars)
│   │   ├── calendar_nse.py         (holidays, special sessions; data file + refresh note)
│   │   └── expiry.py               (weekly/monthly expiry rules, from data file)
│   ├── data/
│   │   ├── provider.py             (MarketDataProvider ABC, read-only)
│   │   ├── models.py               (Bar, Quote, ChainRow, Chain, Event)
│   │   ├── fixture_provider.py
│   │   └── readonly_broker_stub.py (interface docs + NotImplementedError; no network)
│   ├── regime/{indicators.py,engine.py}
│   ├── instruments/{leg.py,spread.py,symbols.py}
│   ├── strategy/{selector.py,bull_call.py,bear_put.py}
│   ├── pricing/black76.py          (fixture/sim only)
│   ├── costs/india_costs.py
│   ├── risk/{engine.py,rules.py,naked_short.py,fingerprint.py}
│   ├── execution/{paper_engine.py,fill_model.py}
│   ├── portfolio/{positions.py,pnl.py,limits.py}
│   └── journal/{journal.py,schema.py,report.py}
├── skills/
│   ├── nifty-regime/SKILL.md       (+ thin CLI)
│   ├── nifty-strategy/
│   ├── nifty-risk/
│   ├── nifty-paper/                (show ticket, record yes, run paper fill, flatten)
│   ├── nifty-positions/
│   ├── nifty-journal/
│   ├── nifty-timemachine/          (model-priced replay, labelled SIMULATION)
│   └── nifty-alerts/               (IST clock, flat-by, daily-loss alerts)
├── config/
│   ├── config.example.json
│   ├── nse_holidays.json
│   └── lot_sizes.json              (effective-dated, with source and verified_on fields)
├── tests/
│   ├── unit/ integration/ safety/ property/
│   └── fixtures/                   (index bars, option chains, event calendars, edge days)
└── trader/                         (runtime output, git-ignored: state, journal)
```

**Packaging decision:** upstream skills are standalone and can be copied one by one. This project shares `nifty_core`, so it is installed as a whole plugin (`skills` path plus the package at the root). Individual skill copying is not supported, and the README must say so. The IST clock replaces the nine duplicated New York clock copies, so duplication is removed rather than carried over.

**Reuse from upstream (ported by reading and re-implementing with attribution, never by editing the upstream repo):** the approval-phrase parser and order fingerprint (broker), the stop/target rounding style (exit), the seeded bootstrap and verdict wording (timemachine), the rule-engine pattern (alerts), and the SKILL.md conventions. Dropped: scanner, news, analyst, pelosi.

## 3. Phases, dependencies and acceptance criteria

Dependency graph:

```
P0 ─► P1 ─► P2 ─► P3 ─► P5 ─► P6 ─► P7 ─► P8 ─► P9 ─► P10
            │      └──► P4 ──┘      ▲
            └─────────────────────┘ (P6 needs P1,P3)
P11 (safety guards) starts in P1 and gates every phase
```

| Phase | Depends on | Scope |
|---|---|---|
| P0 | none | Decisions, verification list, scaffold |
| P1 | P0 | Safety guards, config, errors |
| P2 | P1 | IST clock, NSE calendar, expiry, cost model |
| P3 | P2 | Data models and fixture provider |
| P4 | P3 | Spread/instrument model |
| P5 | P3 | Regime engine |
| P6 | P4, P5 | Strategy engine |
| P7 | P4, P6, P1 | Risk engine |
| P8 | P7 | Paper execution, positions, P&L |
| P9 | P8 | Journal and reports |
| P10 | P1 to P9 | Skills (front-ends), docs, README, end-to-end |
| P11 | P1 onward | Safety guard suite (continuous; final audit before sign-off) |
| P12 | P10 | Real-data paper collection (blocked on data source choice) |

### P0 Scaffold and verification
- Create `nifty-options-dts` as a copy-by-reference project (no files copied into or changed in `day-trader-skill`). Record the upstream commit hash as the reference.
- Resolve the external verification list (section 9) for items that affect defaults.
- **Acceptance:** repo layout exists; `lot_sizes.json`, `nse_holidays.json` have `source` and `verified_on`; the upstream folder has an identical checksum before and after.
- **Tests:** a script test that hashes `../day-trader-skill` (excluding `.git`) and fails on any difference.

### P1 Safety guards, config, errors
- Config schema with validation; `paper` is not user-writable to false in V1 (setting it false raises). Kill-switch file `trader/KILL` halts execution and entries.
- **Acceptance:** every guard in section 6 exists and has a failing-then-passing test; config with `paper:false` or unknown keys is rejected.
- **Tests:** unit tests for config validation; safety tests per section 6.

### P2 Market core and costs
- IST clock (fixed +05:30), session 09:15 to 15:30, 75 five-minute bars, early/special sessions from data; holidays; weekly/monthly expiry resolver from data; cost model (brokerage per order, STT on sell-side premium, exchange transaction charge, SEBI fee, GST on brokerage and charges, stamp duty on buy side) with rates in a dated data file.
- **Acceptance:** boundary-time cases correct (09:14, 09:15, 15:29, 15:30); holiday and weekend handling; expiry resolver returns the right date for a table of known past cases; cost model matches hand-calculated worked examples to the paisa.
- **Tests:** unit, table-driven, with no dependency on the machine's time zone (tests run under three different `TZ` values).

### P3 Data models and fixture provider
- Immutable models; `FixtureProvider` reads JSON/CSV fixtures; staleness metadata; read-only ABC.
- **Fixtures:** at least a trending-up day, a trending-down day, a sideways day, a conflicted day, a gap day, an expiry day, an event day, a holiday, a half/special session, a chain with a wide-spread illiquid strike, a chain with missing strikes, and a stale quote. Option chains are synthetic or model-priced and are labelled `synthetic` in the file header.
- **Acceptance:** provider has no method that can send an order (checked by reflection test); malformed fixtures fail with clear errors.
- **Tests:** unit tests for parsing and validation; reflection test on the provider interface.

### P4 Spread / instrument model
- Implement section 1.2.
- **Acceptance:** all construction invariants enforced; both strategies compute `net_debit`, `max_loss`, `max_profit`, `breakeven` correctly; a single-leg or same-side two-leg construction is impossible.
- **Tests:** unit tests with worked examples for both spreads; property-based style tests (generated random valid and invalid inputs, hand-rolled with a seeded RNG) asserting `max_loss + max_profit == width × lot_size × lots`, `breakeven` inside the strikes, and payoff at expiry never below −max_loss or above max_profit.

### P5 Regime engine
- Deterministic classification from index bars and context. Every result lists the reasons. Default is NO TRADE for anything not clearly directional.
- **Acceptance:** each fixture day classifies to its expected label; no lookahead (a test feeds bars one at a time and asserts that the result at bar N is unchanged when later bars are appended); incomplete or missing bars give `UNKNOWN`.
- **Tests:** unit tests per indicator; fixture-based classification tests; the lookahead test; a determinism test.

### P6 Strategy engine
- Strike selection (long leg near ATM, short leg one width away; width from config, default 50 or 100 points), liquidity filters, risk-fit filter, expiry-day policy (default: no new entries on expiry day after a configured time, and a setting to disable entirely), no entries outside the entry window.
- **Acceptance:** BULLISH gives a bull call spread, BEARISH a bear put spread, all other regimes give NO TRADE; when no one-lot spread fits the risk budget the result is NO TRADE with the cheapest candidate and why it failed; never returns a candidate with a naked leg.
- **Tests:** unit tests per selector rule; integration test chain: regime fixture → candidate; a test that deliberately builds a day where the debit exceeds the risk limit and asserts NO TRADE.

### P7 Risk engine
- Rules: kill switch; clock/entry window; flat-by; data freshness; max trades per day; daily loss (gross and net); per-trade risk cap using `max_loss_with_costs`; naked-short check; defined-risk check; liquidity; no averaging (no second entry in the same direction and expiry while one is open or after a stop-out on the same signal); one open position at a time in V1; exits are never blocked by entry rules but are checked for naked-risk ordering.
- **Acceptance:** every rule has a pass case and a block case; every BLOCK carries a reason; the fingerprint changes when any leg field changes; a verdict from another day is invalid.
- **Tests:** a rule-by-rule matrix; naked-short adversarial tests (short call alone, short put alone, ratio quantity mismatch, wrong strike order, mismatched expiry, mismatched option type, closing the long leg first); fingerprint round-trip; daily-loss accumulation across trades.

### P8 Paper execution, positions, P&L
- Atomic two-leg fills at bid/ask with slippage; costs applied; mark-to-market; stop/target/time exits evaluated on spread value; forced flatten at flat-by; daily counters.
- **Acceptance:** an order without a matching ALLOW verdict is refused; no position survives the flat-by time; P&L reconciles to the cent: `net P&L = gross − costs` across a full simulated day; loss on a spread never exceeds `max_loss + costs` even with a gap fixture.
- **Tests:** unit tests for the fill model and P&L; integration tests for a winning day, a losing day, a stop-out, a flat-by exit, a two-trade day (second trade blocked at the limit), and a daily-loss lockout.

### P9 Journal and reports
- Append-only JSONL; each record includes inputs hash, regime, reasons, candidate, verdict, fills, exit, net P&L, and `data_source`. Reports keep `fixture` and `real` data separate and never mix them. Reports show sample size beside every statistic and print a fixed caveat when fewer than a configured number of real trades exist.
- **Acceptance:** NO TRADE and BLOCKED decisions are journaled; the file cannot be rewritten by the tool (append only, tested); a report built from fixture data states that it is not evidence of profitability.
- **Tests:** unit tests for schema and append-only behaviour; golden-file report test.

### P10 Skills, docs and end-to-end
- Thin SKILL.md front-ends and CLIs over `nifty_core`, the approval flow ("yes" recorded word for word for each order, taken from the upstream design), README, safety statement.
- **Acceptance:** an end-to-end offline run on fixtures goes from data to journal for each of the test days; the full suite is green; documentation matches the behaviour.
- **Tests:** integration tests that drive the CLIs; a docs test asserting each SKILL.md contains the paper-only and not-financial-advice text.

### P11 Safety suite (continuous)
- See section 6. Final audit before sign-off.

### P12 Real-data paper collection (blocked)
- Requires a chosen read-only data source and a decision on whether it is permitted. Adds a `ReadOnlyBrokerProvider` that implements only the read-only ABC, with credentials from environment variables, an HTTP method allowlist (GET only), and an endpoint allowlist.
- **Acceptance:** the safety suite still passes with the provider present; a recorded-session replay test passes; an incident test shows a provider that exposes an order endpoint cannot be wired in.

## 4. Test strategy

- **Runner:** standard-library `unittest` (matching upstream), offline, deterministic, no network access in tests (a guard fails the test if a socket is opened).
- **Unit:** clock, calendar, expiry, costs, models, indicators, pricing, each risk rule, fill model, P&L, journal.
- **Property-style:** seeded random generation for spread invariants and payoff bounds.
- **Integration:** regime → strategy → risk → paper → journal on every fixture day; includes NO TRADE and BLOCKED days.
- **Safety:** section 6.
- **Golden files:** worked examples for costs and for reports.
- **Time handling:** all tests run under several `TZ` settings.
- **Coverage targets:** every risk rule and every guard has both a pass and a block test. Line coverage is reported but is not the acceptance measure.

## 5. Dependencies and constraints

- Python 3.8+, standard library only at runtime (as upstream). A repo test asserts no third-party imports.
- Option pricing is a self-written Black-76 with a documented tolerance, used only for synthetic fixtures and the Time Machine.
- No runtime network access in V1. The provider interface is the only future network boundary.

## 6. Paper-trading safety guard requirements

Each item needs code and a test in `tests/safety/`.

1. **No order-capable API.** `MarketDataProvider` has only read methods. A reflection test fails if any provider class exposes a method whose name matches the forbidden list (`place`, `order`, `buy`, `sell`, `execute`, `submit`, `modify`, `cancel`, `square`, `exit_order`, and so on).
2. **Static scan.** A test greps the source tree for forbidden identifiers and endpoints (broker order URLs, `place_order`, `place_stock_order`, `/orders`, `kite.place`, and similar) and for network libraries imported outside `data/` read-only modules. Fails the build on any hit.
3. **Immutable paper mode.** `paper` is a constant in V1. Config attempts to set it false, environment overrides and CLI flags all raise `LiveTradingNotSupported`.
4. **Execution only through the paper engine.** The only object that changes position state is `PaperExecutionEngine`, and it accepts only an order carrying a risk verdict `ALLOW` with a matching fingerprint from the same day.
5. **Human approval retained.** Each paper order requires a recorded explicit "yes" for that exact order (the upstream phrase parser pattern). A background or scheduled run cannot approve.
6. **Naked-short guard** (section 1.3) at entry, exit and reconciliation, with adversarial tests.
7. **Intraday only.** Flat-by is enforced by the engine, not by an alert. Any open spread at flat-by is force-closed in simulation. Entries are blocked in the final configured window and on non-trading days. A test steps the clock across the close and asserts zero open positions.
8. **No averaging.** A second entry into the same direction and expiry after a losing first entry is blocked.
9. **Limits cannot be loosened at runtime.** Risk limits are read at start; a mid-run change requires a restart and is journaled.
10. **Kill switch.** The file `trader/KILL` blocks all entries and triggers flatten.
11. **Stale or missing data blocks entry.**
12. **No credentials.** The repo contains no API keys; a scan test rejects key-like strings; broker credentials (future) come only from environment variables and are never logged.
13. **Network tripwire in tests.** Opening a socket during tests fails them.
14. **Labelled outputs.** Every printed result and report says PAPER, and says SIMULATED when fixture or model-priced.
15. **Upstream untouched.** The P0 checksum test runs in CI-equivalent form with every phase.

## 7. Journal and validation policy

- Fixture and model-priced runs validate logic only. Reports from them carry the line: "Simulation on synthetic or model-priced data. This is not evidence of profitability."
- Real paper results (P12) are kept in a separate journal stream. No performance claims are printed until a configured minimum number of completed real paper trades exist, and the report always shows sample size, net-of-cost results, and the largest loss.

## 8. Problems with ₹1,00,000 capital and 1% risk for a real one-lot NIFTY debit spread

Figures below are illustrative and must be re-checked against current lot size, premiums and charges (section 9).

**8.1 The risk budget is very small relative to one lot.** 1% of ₹1,00,000 is ₹1,000. Max loss of a debit spread is `net debit × lot size`. If the lot is 75 units, the largest allowed debit is about 13.3 points; if 65 units, about 15.4 points. A 50-point-wide ATM spread typically costs more than that on most days, and wider spreads cost more still. Therefore the risk gate will block many setups, and the system will often return NO TRADE. That is the intended behaviour, but you should expect few trades.

**8.2 The affordable spreads are the less attractive ones.** Within a ~13 to 15 point debit budget, you are pushed toward very short-dated, far-from-the-money, or narrow spreads. Those have low delta, high theta and lower probability of reaching the short strike. Expiry-day spreads are cheap because they are extremely gamma-sensitive; buying them to fit the budget means taking the highest-variance trades.

**8.3 Costs are a large share of the risk.** Round-trip brokerage on four option orders, STT on the sell side, exchange charges, GST and slippage can plausibly total a few hundred rupees on one lot. Against a ₹1,000 risk that is a noticeable fraction, and it directly lowers the real reward-to-risk. The plan therefore sizes the risk cap on `max_loss_with_costs`, not on the theoretical debit alone.

**8.4 Slippage and bid/ask.** NIFTY options are liquid, but a 0.5 to 1 point spread on each leg is worth roughly ₹30 to ₹75 per leg per lot. On a 15-point debit, that is a material percentage.

**8.5 What "1% risk" means is ambiguous.** The theoretical maximum loss (full debit lost) is stricter than a stop-based loss (for example, exit at 50% of the debit). V1 defaults to the theoretical maximum plus costs as the gate, because it is conservative and cannot be exceeded by a gap. The alternative is looser and allows pricier spreads. Choose deliberately.

**8.6 Daily loss versus trade count.** Two trades at the full ₹1,000 risk exactly equal the ₹2,000 daily limit, leaving no room for costs. With costs counted, the second trade could breach the limit. The plan blocks a second trade if its worst case, together with the first trade's realised loss and costs, would exceed the daily limit.

**8.7 Lot size and margin are not fixed.** NSE revises NIFTY lot sizes, and the correct current value must come from the exchange circular. A defined-risk spread needs margin that is the premium paid; it does not need the large short-option margin, but brokers may block or hold extra margin on expiry day or for hedge benefit rules. This only matters for real trading but is relevant to realism.

**8.8 The sample will be small.** With a low trade frequency, even months of paper trading yield a small number of trades. A small sample cannot show an edge or the absence of one. This is the main reason results must be reported with sample sizes.

**8.9 Possible adjustments to consider (for your decision, not applied):**
- Treat the risk as a cap and keep 1% only if you accept frequent NO TRADE.
- Raise paper capital to a figure that matches the intended real account, rather than a convenient number. At ₹2,50,000, 1% is ₹2,500, which admits more spreads.
- Allow a stop-based risk definition after reviewing how often gaps break it.
- Keep paper capital at ₹1,00,000 and treat a high NO TRADE rate as a finding about the account size.

## 9. Open questions that need external verification

None of these is assumed. Each is recorded in a data or config file with `source` and `verified_on`.

1. **Current NIFTY lot size**, and its effective date and any scheduled revision (NSE circulars). Earlier values were 75; a later revision to 65 has been reported. Verify before fixing defaults.
2. **Weekly expiry weekday for NIFTY** and any change history (reported as Tuesday since September 2025), and the monthly expiry rule.
3. **Which expiries exist** for NIFTY weekly and monthly contracts, and strike interval and tick size (assumed 50 and ₹0.05).
4. **NSE trading holidays** for the target year(s) and any special or Muhurat sessions with timings.
5. **Current Indian transaction costs:** STT on option sell premium and on exercise, NSE transaction charge rate, SEBI turnover fee, stamp duty, GST, and the chosen broker's brokerage per order.
6. **SEBI/NSE rules on expiry-day risk**, such as additional margins or restrictions that could affect spreads (relevant to realism).
7. **Margin treatment for defined-risk debit spreads** at the intended broker (premium only, or hedge-based margin rules).
8. **Market data source for P12:** which read-only provider, licensing terms for storing option-chain data, rate limits, historical option data availability, whether the data includes bid/ask, OI and IV, and its latency.
9. **Whether a chosen data provider's terms allow personal paper-trading use** and whether its API surface can be restricted to read-only (separate read-only keys).
10. **Index volume/VWAP:** NIFTY spot has no traded volume. Decide whether the regime engine uses NIFTY futures data for VWAP and volume when the real data source exists.
11. **Event calendar source:** where RBI policy dates, Budget, major results days and US macro events come from, and how they are entered (manual file in V1).
12. **Regime thresholds:** the numeric thresholds in the regime engine are initial assumptions to tune on fixtures, and must not be tuned on the same data later used for evaluation.
13. **Risk definition (section 8.5)** and **capital choice (section 8.9):** require your decision.
14. **Legal/tax or regulatory questions** about automated trading in India are outside this plan; confirm independently before any future live work.

## 10. Out of scope for Version 1

Live trading or any broker order interface; naked option selling; ratio or multi-leg structures beyond a two-leg debit spread; credit spreads; overnight or carry positions; BANKNIFTY and other underlyings; options on stocks; automated parameter optimisation; claims of profitability from model-priced data.

## 11. Approval gate

No code will be written until this plan is approved. On approval, work starts at P0 and proceeds one phase at a time, with the test suite and the safety suite green before moving to the next phase.
