"""Position manager tests. Chains are hand-edited so every unrealized and realized figure can be computed by hand."""
import datetime
import inspect
import json
import os
import tempfile
import types
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core.costs.models import BUY, SELL, LegExecution
from nifty_core.data.fixture_provider import FixtureProvider, Scenario
from nifty_core.errors import LiveTradingNotSupported
from nifty_core.execution import paper as X
from nifty_core.guards import paper_only
from nifty_core.market.ist import UTC, parse_ist
from nifty_core.positions import manager as M
from nifty_core.positions import params as PP
from nifty_core.regime import params as RP
from nifty_core.risk import engine as R
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.unit import test_execution as TE
from tests.unit import test_risk as TR
from tests.unit import test_strategy as T

DAY, T1230, ENGINE = TR.DAY, TR.T1230, TR.ENGINE
RPARAMS = RP.load_params()
DEFAULT = PP.load_params()
UNITS = 65
SLIP = Dec("0.5")


def at(seconds):
    return T1230 + datetime.timedelta(seconds=seconds)


T1300, T1445, T1514, T1515, T1516, T1520, T1539, T1540 = (at(1800), at(8100), at(9840), at(9900), at(9960),
                                                          at(10200), at(11340), at(11400))


def pparams(**override):
    v = {f: getattr(DEFAULT, f) for f in PP.FIELDS}
    v.update(override)
    return PP.build_params(v)


def regime(label):
    return lambda now: types.SimpleNamespace(label=label)


class Base(TE.Base):
    """Opens a position at 12:30 and gives helpers to build the later chains."""

    def chain_at(self, seconds, lb="116.00", la="116.85", sb="85.55", sa="86.10"):
        c = TE.shift(TE.entry_chain(), seconds)
        c = TE.set_quote(c, self.long_c, bid=Dec(lb), ask=Dec(la))
        return TE.set_quote(c, self.short_c, bid=Dec(sb), ask=Dec(sa))

    def start(self, chains, label="BULLISH", params=None, exec_cfg=None, regime_fn="default"):
        """Build the world and open the position. `chains` are the later snapshots; the 12:30 one is added."""
        self.build([TE.entry_chain()] + list(chains), exec_cfg=exec_cfg)
        fn = regime(label) if regime_fn == "default" else regime_fn
        self.pm = M.PositionManager(exec_cfg or self.cfg, params or DEFAULT, self.ex, self.mgr, self.provider,
                                    sc.calendar(), RPARAMS, ENGINE, regime_fn=fn)
        result, self.pos = self.pm.enter(self.approval, self.cand, 1, T1230)
        self.assertEqual(result.status, X.FILLED, result.reasons)
        return self.pos.position_id

    def profit_chain(self, seconds):
        return self.chain_at(seconds, lb="140.00", la="140.85", sb="80.00", sa="80.55")

    def loss_chain(self, seconds):
        return self.chain_at(seconds, lb="100.00", la="100.85", sb="90.00", sa="90.55")

    def state(self, pid):
        return self.pm.position(pid).state


class LifecycleTests(Base):
    def test_successful_lifecycle_open_hold_close(self):
        pid = self.start([self.chain_at(1800), self.profit_chain(3600)])
        self.assertEqual(self.state(pid), M.OPEN)
        r = self.pm.manage(pid, T1300)                          # unchanged quotes: HOLD
        self.assertEqual((r.decision.action, r.execution, r.position.state), (M.HOLD, None, M.OPEN))
        r = self.pm.manage(pid, at(3600))                       # profit
        self.assertEqual((r.decision.action, r.decision.primary_reason), (M.CLOSE, M.PROFIT_TARGET))
        self.assertEqual((r.execution.status, r.position.state), (X.FILLED, M.CLOSED))
        self.assertEqual(self.pm.active(), ())
        self.assertEqual([e.to_state for e in self.pm.position(pid).history], [M.OPEN, M.CLOSED])

    def test_a_closed_position_is_terminal(self):
        pid = self.start([self.profit_chain(3600)])
        self.pm.manage(pid, at(3600))
        r = self.pm.manage(pid, at(3660))
        self.assertEqual((r.decision, r.execution), (None, None))
        self.assertEqual(self.state(pid), M.CLOSED)

    def test_entry_failure_creates_no_managed_position(self):
        self.build([TE.set_quote(TE.entry_chain(), self.short_c, bid=None)])
        pm = M.PositionManager(self.cfg, DEFAULT, self.ex, self.mgr, self.provider, sc.calendar(), RPARAMS, ENGINE,
                               regime_fn=regime("BULLISH"))
        result, pos = pm.enter(self.approval, self.cand, 1, T1230)
        self.assertEqual((result.status, pos, pm.positions()), (X.PENDING, None, ()))

    def test_duplicate_positions_are_prevented(self):
        pid = self.start([])
        result, pos = self.pm.enter(self.approval, self.cand, 1, T1230)               # same approval again
        self.assertEqual((result.status, pos), (X.REJECTED, None))
        self.assertEqual(len(self.pm.positions()), 1)
        other = R.RiskManager(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill, DAY)
        approval = other.request_entry(self.cand, T1230).approval            # a different approval, same contracts
        result, pos = self.pm.enter(approval, self.cand, 1, T1230)
        self.assertEqual((result.status, pos), (X.REJECTED, None))
        self.assertEqual([r.code for r in result.reasons][0], "DUPLICATE_POSITION")
        self.assertIn("POSITION_LOT_CEILING", [r.code for r in result.reasons])
        self.assertEqual(self.ex.log[-1], result)

    def test_once_closed_the_same_contracts_may_be_entered_again_by_a_new_decision(self):
        pid = self.start([self.profit_chain(3600)])
        self.assertTrue(self.pm._entry_conflicts(self.cand, 1))
        self.pm.manage(pid, at(3600))
        self.assertEqual(self.pm._entry_conflicts(self.cand, 1), [])

    def test_no_operation_exists_to_add_lots_average_roll_or_add_legs(self):
        public = {n for n, v in inspect.getmembers(M.PositionManager) if callable(v) and not n.startswith("_")}
        self.assertEqual(public, {"active", "close_manually", "end_of_session", "enter", "evaluate", "manage",
                                  "manage_all", "pnl_report", "position", "positions"})
        for word in ("add", "scale", "roll", "average", "recover", "rescue", "adjust", "leg"):
            self.assertFalse([n for n in public if word in n], word)
        for field in M.ManagedPosition._fields:
            self.assertNotIn("add", field)

    def test_the_exit_uses_exactly_the_entry_contracts_and_lots(self):
        pid = self.start([self.profit_chain(3600)])
        self.pm.manage(pid, at(3600))
        epos = self.ex.position(pid)
        self.assertEqual([f.contract for f in epos.exit_fills], [f.contract for f in epos.entry_fills])
        self.assertEqual([(f.lots, f.units) for f in epos.exit_fills], [(f.lots, f.units) for f in epos.entry_fills])
        self.assertEqual([f.side for f in epos.exit_fills], [SELL, BUY])
        self.assertEqual(len(epos.exit_fills), 2)

    def test_a_managed_position_is_immutable(self):
        self.start([])
        with self.assertRaises(AttributeError):
            self.pos.lots = 2

    def test_a_stale_approval_does_not_block_a_fresh_decision_later(self):
        # stale: the first approval is used 61 s late and rejected, its reservation is released
        chain_b = TE.shift(TE.entry_chain("30.05"), 120)
        self.build([TE.entry_chain(), chain_b])
        pm = M.PositionManager(self.cfg, DEFAULT, self.ex, self.mgr, self.provider, sc.calendar(), RPARAMS, ENGINE,
                               regime_fn=regime("BULLISH"))
        result, pos = pm.enter(self.approval, self.cand, 1, at(61))
        self.assertEqual(([r.code for r in result.reasons], pos), (["APPROVAL_STALE"], None))
        # fresh pipeline: a new candidate decided later, approved afresh, executed on fresh quotes
        fresh = TR.cheap_candidate("30.05", at=at(120))
        decision = self.mgr.request_entry(fresh, at(120))
        self.assertEqual(decision.outcome, R.APPROVED, [r.code for r in decision.reasons])
        result, pos = pm.enter(decision.approval, fresh, 1, at(120))
        self.assertEqual(result.status, X.FILLED, result.reasons)
        self.assertEqual(pos.state, M.OPEN)


class EntryRegimeTests(Base):
    def test_the_entry_regime_comes_from_the_candidate(self):
        for label in ("BULLISH", "BEARISH"):
            self.cand = TR.cheap_candidate("30.00")._replace(
                regime_at_entry=dict(TR.cheap_candidate("30.00").regime_at_entry, label=label))
            self.start([])
            self.assertEqual(self.pos.entry_regime, label)

    def test_preview_only_prices_open_positions(self):
        pid = self.start([self.profit_chain(3600)])
        fills, problems = self.ex.preview_exit(pid, at(3600))
        self.assertEqual((len(fills), problems), (2, []))
        self.pm.manage(pid, at(3600))
        fills, problems = self.ex.preview_exit(pid, at(3600))
        self.assertEqual((fills, [p.code for p in problems]), ((), ["NOT_OPEN"]))
        self.assertEqual([p.code for p in self.ex.preview_exit("nope", at(3600))[1]], ["NOT_OPEN"])


class ExitReasonTests(Base):
    def test_profit_target(self):
        pid = self.start([self.profit_chain(3600)])
        d = self.pm.evaluate(pid, at(3600))
        self.assertEqual((d.action, d.primary_reason, d.triggers), (M.CLOSE, M.PROFIT_TARGET, (M.PROFIT_TARGET,)))
        target = self.approval.defined_risk_inr * DEFAULT.profit_target_pct_of_defined_risk / 100
        self.assertGreaterEqual(d.unrealized["net_pnl_inr"], target)

    def test_stop_loss(self):
        pid = self.start([self.loss_chain(3600)])
        d = self.pm.evaluate(pid, at(3600))
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.STOP_LOSS))
        self.assertLessEqual(d.unrealized["net_pnl_inr"],
                             -(self.approval.defined_risk_inr * DEFAULT.stop_loss_pct_of_defined_risk / 100))
        r = self.pm.manage(pid, at(3600))
        self.assertEqual((r.position.state, r.position.exit_reason), (M.CLOSED, M.STOP_LOSS))
        self.assertLess(Dec(r.execution.actual["net_pnl_inr"]), 0)

    def test_profit_and_stop_thresholds_are_inclusive_at_the_exact_value(self):
        pid = self.start([self.chain_at(3600)])
        defined = self.approval.defined_risk_inr
        stop = defined * DEFAULT.stop_loss_pct_of_defined_risk / 100
        target = defined * DEFAULT.profit_target_pct_of_defined_risk / 100
        def fake(net):
            return lambda self_, pos, fills, now: {"label": "x", "gross_pnl_inr": net, "actual_entry_costs_inr": Dec(0),
                                                    "estimated_exit_costs_inr": Dec(0), "net_pnl_inr": net}
        for net, expect in ((-stop, M.STOP_LOSS), (-stop + Dec("0.01"), None), (target, M.PROFIT_TARGET),
                            (target - Dec("0.01"), None)):
            with mock.patch.object(M.PositionManager, "_unrealized", fake(net)):
                d = self.pm.evaluate(pid, at(3600))
            self.assertEqual(d.primary_reason, expect, net)

    def test_hold_between_the_thresholds(self):
        pid = self.start([self.chain_at(3600)])
        d = self.pm.evaluate(pid, at(3600))
        self.assertEqual((d.action, d.primary_reason, d.triggers), (M.HOLD, None, ()))
        self.assertLess(d.unrealized["net_pnl_inr"], 0)

    def test_regime_invalidation_opposite_direction(self):
        pid = self.start([self.chain_at(3600)], label="BULLISH", regime_fn=regime("BEARISH"))
        d = self.pm.evaluate(pid, at(3600))
        self.assertEqual((d.action, d.primary_reason, d.regime_label), (M.CLOSE, M.REGIME_INVALIDATION, "BEARISH"))
        pid = self.start([self.chain_at(3600)], regime_fn=regime("BULLISH"))
        self.pm._positions[pid] = self.pm.position(pid)._replace(entry_regime="BEARISH")      # a bearish-entry position
        self.assertEqual(self.pm.evaluate(pid, at(3600)).primary_reason, M.REGIME_INVALIDATION)
        self.assertEqual(self.pm.evaluate(pid, at(3600)).regime_label, "BULLISH")

    def test_regime_invalidation_high_volatility_and_its_switches(self):
        pid = self.start([self.chain_at(3600)], regime_fn=regime("HIGH_VOLATILITY"))
        self.assertEqual(self.pm.evaluate(pid, at(3600)).primary_reason, M.REGIME_INVALIDATION)
        pid = self.start([self.chain_at(3600)], regime_fn=regime("HIGH_VOLATILITY"),
                         params=pparams(invalidate_on_high_volatility=False))
        self.assertEqual(self.pm.evaluate(pid, at(3600)).action, M.HOLD)
        pid = self.start([self.chain_at(3600)], regime_fn=regime("BEARISH"),
                         params=pparams(invalidate_on_opposite_regime=False))
        self.assertEqual(self.pm.evaluate(pid, at(3600)).action, M.HOLD)

    def test_same_direction_sideways_and_no_trade_do_not_invalidate(self):
        for label in ("BULLISH", "SIDEWAYS", "NO_TRADE"):
            pid = self.start([self.chain_at(3600)], regime_fn=regime(label))
            self.assertEqual(self.pm.evaluate(pid, at(3600)).action, M.HOLD, label)

    def test_data_safety_after_consecutive_failed_checks_and_reset_by_a_good_check(self):
        gap = T.edit_chain(self.chain_at(60), drop=(self.long_c,))
        pid = self.start([gap, self.chain_at(120), T.edit_chain(self.chain_at(180), drop=(self.long_c,)),
                          T.edit_chain(self.chain_at(240), drop=(self.long_c,)),
                          T.edit_chain(self.chain_at(300), drop=(self.long_c,))])
        r = self.pm.manage(pid, at(60))
        self.assertEqual((r.decision.action, r.decision.bad_data_streak, r.position.bad_data_streak), (M.HOLD, 1, 1))
        self.assertIn("MISSING_QUOTE", [p.code for p in r.decision.data_problems])
        r = self.pm.manage(pid, at(120))                         # a good check resets the streak
        self.assertEqual(r.position.bad_data_streak, 0)
        for t, expected_streak in ((180, 1), (240, 2)):
            r = self.pm.manage(pid, at(t))
            self.assertEqual((r.decision.action, r.position.bad_data_streak), (M.HOLD, expected_streak))
        r = self.pm.manage(pid, at(300))                         # third in a row
        self.assertEqual((r.decision.action, r.decision.primary_reason), (M.CLOSE, M.DATA_SAFETY))
        self.assertEqual((r.execution.status, r.position.state), (X.PENDING, M.EXIT_PENDING))

    def test_regime_with_no_data_counts_as_a_data_safety_strike(self):
        pid = self.start([self.chain_at(60)], regime_fn=regime("NO_DATA"), params=pparams(data_safety_failed_checks=1))
        d = self.pm.evaluate(pid, at(60))
        self.assertEqual(d.primary_reason, M.DATA_SAFETY)
        self.assertIn("REGIME_NO_DATA", [p.code for p in d.data_problems])

    def test_forced_flat_by_boundary(self):
        pid = self.start([self.chain_at(9840), self.chain_at(9900)])
        self.assertEqual(self.pm.evaluate(pid, T1514).action, M.HOLD)
        d = self.pm.evaluate(pid, T1515)
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.FORCED_FLAT_BY))

    def test_forced_flat_by_follows_configuration(self):
        cfg = self.cfg._replace(flat_by_ist="13:00")
        pid = self.start([self.chain_at(1799), self.chain_at(1800)], exec_cfg=cfg)
        self.assertEqual(self.pm.evaluate(pid, at(1799)).action, M.HOLD)
        self.assertEqual(self.pm.evaluate(pid, at(1800)).primary_reason, M.FORCED_FLAT_BY)

    def test_manual_paper_exit(self):
        pid = self.start([self.chain_at(300)])
        self.assertEqual(self.pm.evaluate(pid, at(300)).action, M.HOLD)
        r = self.pm.close_manually(pid, at(300))
        self.assertEqual((r.decision.primary_reason, r.position.state, r.position.exit_reason),
                         (M.MANUAL_PAPER_EXIT, M.CLOSED, M.MANUAL_PAPER_EXIT))
        self.assertEqual(r.execution.actual["exit_reason"], M.MANUAL_PAPER_EXIT)

    def test_priority_when_several_reasons_apply(self):
        pid = self.start([self.loss_chain(9900)], regime_fn=regime("BEARISH"))
        d = self.pm.evaluate(pid, T1515, manual=True)
        self.assertEqual(d.triggers, (M.FORCED_FLAT_BY, M.MANUAL_PAPER_EXIT, M.STOP_LOSS, M.REGIME_INVALIDATION))
        self.assertEqual(d.primary_reason, M.FORCED_FLAT_BY)
        d = self.pm.evaluate(pid, at(9900))
        pid2 = self.start([self.profit_chain(3600)], regime_fn=regime("BEARISH"))
        d = self.pm.evaluate(pid2, at(3600))
        self.assertEqual(d.triggers, (M.REGIME_INVALIDATION, M.PROFIT_TARGET))
        self.assertEqual(d.primary_reason, M.REGIME_INVALIDATION)

    def test_a_decided_close_is_sticky(self):
        gap = T.edit_chain(self.chain_at(600), drop=(self.long_c,))
        pid = self.start([gap, self.chain_at(660)])
        r = self.pm.close_manually(pid, at(600))                    # the exit cannot fill: no long-leg quote
        self.assertEqual((r.position.state, r.position.exit_reason), (M.EXIT_PENDING, M.MANUAL_PAPER_EXIT))
        r = self.pm.manage(pid, at(660))                            # no trigger is active any more; the decision stands
        self.assertEqual((r.decision.primary_reason, r.position.state), (M.MANUAL_PAPER_EXIT, M.CLOSED))

    def test_the_default_regime_function_runs_on_the_fixture_candles(self):
        pid = self.start([self.chain_at(60)], regime_fn=None)
        d = self.pm.evaluate(pid, at(60))
        self.assertIn(d.regime_label, ("BULLISH", "BEARISH", "SIDEWAYS", "HIGH_VOLATILITY", "NO_DATA", "NO_TRADE"))


class ExitDataTests(Base):
    def test_two_legs_both_needed_each_missing_leg_blocks_the_exit(self):
        for contract in (self.long_c, self.short_c):
            gap = T.edit_chain(self.profit_chain(3600), drop=(contract,))
            pid = self.start([gap])
            r = self.pm.close_manually(pid, at(3600))
            self.assertEqual((r.execution.status, r.execution.legs), (X.PENDING, ()))
            self.assertEqual(r.position.state, M.EXIT_PENDING)
            self.assertEqual(self.ex.position(pid).state, X.OPEN)
            self.assertEqual(self.mgr.snapshot().open_lots, 1)

    def test_missing_stale_one_sided_and_thin_exit_data_leave_a_pending_exit(self):
        base = lambda: self.profit_chain(3600)
        cases = (("MISSING_QUOTE", T.edit_chain(base(), drop=(self.short_c,)), 3600),
                 ("STALE_QUOTE", TE.shift(base(), -61), 3600),
                 ("ONE_SIDED_QUOTE", TE.set_quote(base(), self.long_c, bid=None), 3600),
                 ("INSUFFICIENT_QUOTED_SIZE", TE.set_quote(base(), self.long_c, bid_qty=UNITS - 1), 3600),
                 ("MISSING_QUOTED_SIZE", TE.set_quote(base(), self.short_c, ask_qty=None), 3600))
        for code, chain, t in cases:
            pid = self.start([chain])
            r = self.pm.close_manually(pid, at(t))
            self.assertEqual((r.decision.action, r.execution.status, r.position.state), (M.CLOSE, X.PENDING, M.EXIT_PENDING), code)
            self.assertIn(code, [c for c, _ in r.position.last_pending_reasons])
            self.assertEqual(self.ex.position(pid).state, X.OPEN)           # never pretended to be closed
            self.assertIsNone(self.ex.position(pid).net_pnl_inr)
            self.assertEqual(self.mgr.snapshot().realized_pnl_inr, 0)

    def test_a_pending_exit_retries_with_fresh_data_and_then_closes(self):
        gap = T.edit_chain(self.profit_chain(3600), drop=(self.short_c,))
        pid = self.start([gap, self.profit_chain(3660)])
        r = self.pm.close_manually(pid, at(3600))
        self.assertEqual(r.position.state, M.EXIT_PENDING)
        self.assertEqual(r.position.exit_attempts, 1)
        r = self.pm.manage(pid, at(3660))
        self.assertEqual((r.position.state, r.execution.status, r.position.exit_attempts), (M.CLOSED, X.FILLED, 2))
        self.assertEqual([e.to_state for e in r.position.history], [M.OPEN, M.EXIT_PENDING, M.CLOSED])

    def test_profit_and_loss_triggers_cannot_fire_without_valid_exit_quotes(self):
        for chain in (T.edit_chain(self.loss_chain(600), drop=(self.short_c,)),
                      TE.set_quote(self.profit_chain(600), self.long_c, bid=None)):
            pid = self.start([chain])
            d = self.pm.evaluate(pid, at(600))
            self.assertEqual((d.action, d.unrealized), (M.HOLD, None))
            self.assertTrue(d.data_problems)

    def test_open_position_is_visible_in_the_ledger_until_really_closed(self):
        gap = T.edit_chain(self.loss_chain(600), drop=(self.short_c,))
        pid = self.start([gap])
        self.pm.close_manually(pid, at(600))
        snap = self.mgr.snapshot()
        self.assertEqual((snap.open_lots, snap.open_risk_reserved_inr), (1, self.approval.defined_risk_inr))


class ForcedExitTests(Base):
    def test_forced_exit_pending_then_filled_when_data_returns(self):
        gap = T.edit_chain(self.chain_at(9900), drop=(self.short_c,))
        thin = TE.set_quote(self.chain_at(9960), self.long_c, bid_qty=0)
        pid = self.start([gap, thin, self.chain_at(10020)])
        r = self.pm.manage(pid, T1515)
        self.assertEqual((r.position.state, r.position.forced, r.position.exit_reason),
                         (M.EXIT_PENDING, True, M.FORCED_FLAT_BY))
        self.assertIn("MISSING_QUOTE", [c for c, _ in r.position.last_pending_reasons])
        r = self.pm.manage(pid, T1516)
        self.assertEqual((r.position.state, r.position.forced), (M.EXIT_PENDING, True))
        self.assertIn("INSUFFICIENT_QUOTED_SIZE", [c for c, _ in r.position.last_pending_reasons])
        self.assertEqual(self.pm.critical_exceptions, ())
        r = self.pm.manage(pid, at(10020))
        self.assertEqual((r.position.state, r.execution.status, r.position.exit_attempts), (M.CLOSED, X.FILLED, 3))
        self.assertTrue(r.position.forced)

    def test_every_call_after_flat_by_attempts_an_exit(self):
        pid = self.start([T.edit_chain(self.chain_at(t), drop=(self.long_c,)) for t in (9900, 9960, 10020)])
        for t in (9900, 9960, 10020):
            self.pm.manage(pid, at(t))
        attempts = [r for r in self.ex.log if r.kind == X.EXIT]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.pm.position(pid).exit_attempts, 3)

    def test_manage_all_retries_every_active_position(self):
        pid = self.start([T.edit_chain(self.chain_at(9900), drop=(self.long_c,))])
        results = self.pm.manage_all(T1515)
        self.assertEqual([r.position.position_id for r in results], [pid])

    def test_session_end_with_an_open_position_is_a_critical_exception_never_closed(self):
        gap = lambda t: T.edit_chain(self.chain_at(t), drop=(self.short_c,))
        pid = self.start([gap(9900), gap(11340), self.chain_at(11400)])
        self.pm.manage(pid, T1515)
        self.pm.manage(pid, T1539)
        before = len(self.ex.log)
        r = self.pm.manage(pid, T1540)                             # 15:40: the session is over
        self.assertEqual(r.position.state, M.UNCLOSED_CRITICAL)
        self.assertIsNone(r.execution)
        self.assertEqual(len(self.ex.log), before)                 # no exit was attempted after the close
        self.assertEqual(len(self.pm.critical_exceptions), 1)
        self.assertIn("NOT closed", self.pm.critical_exceptions[0].text)
        self.assertIn("MISSING_QUOTE", self.pm.critical_exceptions[0].text)
        self.assertEqual(self.ex.position(pid).state, X.OPEN)
        snap = self.mgr.snapshot()
        self.assertEqual((snap.open_lots, snap.realized_pnl_inr), (1, 0))
        self.assertNotEqual(r.position.state, M.CLOSED)
        self.assertEqual(self.pm.active(), ())                      # terminal, but flagged critical
        self.assertEqual(r.position.history[-1].to_state, M.UNCLOSED_CRITICAL)

    def test_one_minute_before_the_close_the_exit_is_still_attempted(self):
        pid = self.start([self.chain_at(11340)])
        r = self.pm.manage(pid, T1539)
        self.assertEqual(r.position.state, M.CLOSED)

    def test_end_of_session_before_the_close_does_nothing(self):
        pid = self.start([])
        self.assertEqual(self.pm.end_of_session(T1539), ())
        self.assertEqual(self.state(pid), M.OPEN)

    def test_end_of_session_leaves_closed_positions_alone(self):
        pid = self.start([self.profit_chain(3600)])
        self.pm.manage(pid, at(3600))
        self.assertEqual(self.pm.end_of_session(T1540), ())
        self.assertEqual(self.state(pid), M.CLOSED)

    def test_a_pending_position_at_close_is_also_critical(self):
        pid = self.start([T.edit_chain(self.chain_at(9900), drop=(self.long_c,))])
        self.pm.manage(pid, T1515)
        raised = self.pm.end_of_session(T1540)
        self.assertEqual([e.position_id for e in raised], [pid])
        self.assertEqual(self.state(pid), M.UNCLOSED_CRITICAL)

    def test_kill_switch_allows_exits_but_blocks_entries(self):
        pid = self.start([self.profit_chain(3600)])
        paper_only.trip_kill_switch(self.kill)
        r = self.pm.manage(pid, at(3600))
        self.assertEqual((r.execution.status, r.position.state), (X.FILLED, M.CLOSED))
        fresh = TR.cheap_candidate("30.05", at=at(3700))
        blocked = self.mgr.request_entry(fresh, at(3700))
        self.assertIn("KILL_SWITCH_ACTIVE", [x.code for x in blocked.reasons])

    def test_kill_switch_still_requires_valid_exit_data(self):
        pid = self.start([T.edit_chain(self.profit_chain(3600), drop=(self.short_c,))])
        paper_only.trip_kill_switch(self.kill)
        r = self.pm.close_manually(pid, at(3600))
        self.assertEqual((r.execution.status, r.position.state), (X.PENDING, M.EXIT_PENDING))

    def test_a_broken_paper_guard_stops_the_manager(self):
        pid = self.start([])
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                self.pm.manage(pid, at(60))
            with self.assertRaises(LiveTradingNotSupported):
                self.pm.enter(self.approval, self.cand, 1, T1230)


class PnlTests(Base):
    def test_profitable_trade_figures_by_hand(self):
        pid = self.start([self.profit_chain(3600)])
        entry_long, entry_short = Dec("116.05"), Dec("85.05")                  # 115.55 ask / 85.55 bid, 0.5 slippage
        r = self.pm.manage(pid, at(3600))
        gross = ((Dec("139.50") - entry_long) + (entry_short - Dec("81.05"))) * UNITS
        self.assertEqual(gross, Dec("1784.25"))
        ec = ENGINE.actual_costs([LegExecution("long", BUY, entry_long, UNITS, DAY),
                                  LegExecution("short", SELL, entry_short, UNITS, DAY)]).total
        xc = ENGINE.actual_costs([LegExecution("long", SELL, Dec("139.50"), UNITS, DAY),
                                  LegExecution("short", BUY, Dec("81.05"), UNITS, DAY)]).total
        self.assertEqual(Dec(r.execution.actual["net_pnl_inr"]), (gross - ec - xc).quantize(Dec("0.01")))
        self.assertGreater(Dec(r.execution.actual["net_pnl_inr"]), 0)
        self.assertEqual(self.mgr.snapshot().realized_pnl_inr, Dec(r.execution.actual["net_pnl_inr"]))

    def test_losing_trade_figures_by_hand(self):
        pid = self.start([self.loss_chain(3600)])
        r = self.pm.manage(pid, at(3600))
        gross = ((Dec("99.50") - Dec("116.05")) + (Dec("85.05") - Dec("91.05"))) * UNITS
        self.assertEqual(Dec(r.execution.actual["gross_pnl_inr"]), gross)
        net = Dec(r.execution.actual["net_pnl_inr"])
        self.assertLess(net, gross)                                              # costs make a loss larger
        snap = self.mgr.snapshot()
        self.assertEqual((snap.realized_pnl_inr, snap.realized_loss_used_inr), (net, -net))

    def test_slippage_is_in_every_fill_and_changes_the_result(self):
        pid = self.start([self.profit_chain(3600)])
        self.pm.manage(pid, at(3600))
        epos = self.ex.position(pid)
        for f in list(epos.entry_fills) + list(epos.exit_fills):
            self.assertEqual(f.slippage_points, SLIP)
            adverse = (f.fill_price - f.reference_price) if f.side == BUY else (f.reference_price - f.fill_price)
            self.assertEqual(adverse, SLIP)
        self.assertEqual(len(epos.entry_fills) + len(epos.exit_fills), 4)

    def test_report_keeps_estimated_actual_and_unrealized_apart(self):
        pid = self.start([self.chain_at(900), self.profit_chain(3600)])
        open_report = self.pm.pnl_report(pid, at(900))
        self.assertEqual(open_report["state"], M.OPEN)
        self.assertIsNone(open_report["actual_exit_fills"])
        self.assertIsNone(open_report["actual_exit_costs_inr"])
        self.assertIsNone(open_report["gross_pnl_inr"])
        self.assertIsNone(open_report["net_realized_pnl_inr"])                   # nothing is realized yet
        self.assertIn("UNREALIZED", open_report["unrealized"]["label"])
        self.assertIn("ESTIMATE", open_report["estimated_at_approval"]["label"])
        self.assertEqual(open_report["estimated_at_approval"]["approved_total_defined_risk_inr"],
                         str(self.approval.defined_risk_inr))
        self.pm.manage(pid, at(3600))
        done = self.pm.pnl_report(pid, at(3600))
        self.assertIsNone(done["unrealized"])
        net = Dec(done["net_realized_pnl_inr"])
        self.assertEqual(net, Dec(done["gross_pnl_inr"]) - Dec(done["actual_entry_costs_inr"]) - Dec(done["actual_exit_costs_inr"]))
        self.assertEqual(len(done["actual_entry_fills"]), 2)
        self.assertEqual(len(done["actual_exit_fills"]), 2)
        json.dumps(done)

    def test_unrealized_is_never_booked_as_realized(self):
        gap = T.edit_chain(self.loss_chain(600), drop=(self.short_c,))
        pid = self.start([self.loss_chain(300), gap])
        d = self.pm.evaluate(pid, at(300))
        self.assertIsNotNone(d.unrealized)
        self.assertEqual(self.mgr.snapshot().realized_pnl_inr, 0)
        self.pm.manage(pid, at(300))                                             # stop-loss on good data: really closes
        self.assertEqual(self.mgr.snapshot().realized_pnl_inr, self.pm.pnl_report(pid)["net_realized_pnl_inr"] and
                         Dec(self.pm.pnl_report(pid)["net_realized_pnl_inr"]))                  # realized comes from the fills

    def test_estimated_exit_costs_differ_from_actual_ones_in_the_final_report(self):
        pid = self.start([self.profit_chain(3600)])
        r = self.pm.manage(pid, at(3600))
        est = Dec(r.execution.estimated_at_approval["estimated_exit_costs_inr"])
        act = Dec(r.execution.actual["actual_exit_costs"]["total_inr"])
        self.assertNotEqual(est, act)
        self.assertEqual(Dec(r.execution.actual["net_pnl_inr"]),
                         (Dec(r.execution.actual["gross_pnl_inr"]) - Dec(r.execution.actual["actual_entry_costs_inr"]) - act))

    def test_unrealized_uses_actual_entry_costs_and_estimated_exit_costs(self):
        pid = self.start([self.profit_chain(3600)])
        u = self.pm.evaluate(pid, at(3600)).unrealized
        epos = self.ex.position(pid)
        self.assertEqual(u["actual_entry_costs_inr"], epos.actual_entry_costs.total.quantize(Dec("0.01")))
        fills, _ = self.ex.preview_exit(pid, at(3600))
        est = ENGINE.exit_costs([LegExecution(f.leg_id, f.side, f.fill_price, f.units, DAY) for f in fills]).total
        self.assertEqual(u["estimated_exit_costs_inr"], est.quantize(Dec("0.01")))
        self.assertEqual(u["net_pnl_inr"], (u["gross_pnl_inr"] - u["actual_entry_costs_inr"] - u["estimated_exit_costs_inr"]))

    def test_same_instant_in_any_time_zone_gives_the_same_decision(self):
        pid = self.start([self.profit_chain(3600)])
        a = self.pm.evaluate(pid, at(3600))
        b = self.pm.evaluate(pid, at(3600).astimezone(UTC))
        self.assertEqual((a.action, a.primary_reason, a.unrealized), (b.action, b.primary_reason, b.unrealized))

    def test_the_whole_lifecycle_is_deterministic(self):
        outs = []
        for _ in range(2):
            pid = self.start([self.chain_at(900), self.loss_chain(3600)])
            self.pm.manage(pid, at(900))
            self.pm.manage(pid, at(3600))
            outs.append(json.dumps([X.result_to_journal(r) for r in self.ex.log] + [self.pm.pnl_report(pid)], sort_keys=True))
        self.assertEqual(outs[0], outs[1])


class ParamsTests(NoNetworkTestCase):
    def test_defaults_and_file_kind(self):
        p = PP.load_params()
        self.assertEqual((p.profit_target_pct_of_defined_risk, p.stop_loss_pct_of_defined_risk), (Dec("60"), Dec("50")))
        self.assertEqual((p.invalidate_on_opposite_regime, p.invalidate_on_high_volatility, p.data_safety_failed_checks),
                         (True, True, 3))
        from nifty_core.versioned import load_json
        doc = load_json(PP.DEFAULT_PATH)
        self.assertEqual(doc["kind"], "STRATEGY_ASSUMPTIONS")
        self.assertIn("NOT validated", doc["description"])
        self.assertIn("NOT tuned", doc["description"])
        text = json.dumps(doc, default=str).lower()
        for word in ("lot_size", "tick", "stt", "brokerage", "capital", "risk_per_trade", "max_daily_loss", "flat_by_ist\":"):
            self.assertNotIn(word, text)

    def test_bad_values_and_fingerprint(self):
        from nifty_core.errors import ConfigError
        base = {f: getattr(PP.load_params(), f) for f in PP.FIELDS}
        for bad in (dict(profit_target_pct_of_defined_risk=0), dict(stop_loss_pct_of_defined_risk=-1),
                    dict(stop_loss_pct_of_defined_risk=101), dict(invalidate_on_high_volatility="yes"),
                    dict(data_safety_failed_checks=0), dict(data_safety_failed_checks=True)):
            with self.assertRaises(ConfigError, msg=str(bad)):
                PP.build_params(dict(base, **bad))
        self.assertNotEqual(PP.build_params(dict(base, stop_loss_pct_of_defined_risk=40)).fingerprint,
                            PP.load_params().fingerprint)
        self.assertEqual(PP.build_params(dict(base, stop_loss_pct_of_defined_risk=100)).stop_loss_pct_of_defined_risk, 100)

    def test_the_risk_engine_never_sees_the_stop_loss(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for fname in ("engine.py", "ledger.py"):
            with open(os.path.join(root, "nifty_core", "risk", fname), encoding="utf-8") as fh:
                text = fh.read()
            for word in ("nifty_core.positions", "..positions", "PositionManager", "position_thresholds", "profit_target"):
                self.assertNotIn(word, text, fname)
            self.assertNotIn("stop_loss", text.replace("stop_loss_used", ""), fname)    # only the flag that says none was used


if __name__ == "__main__":
    unittest.main()
