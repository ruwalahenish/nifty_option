"""Credit structures through the whole execution stack: risk gate -> paper executor -> position manager.

The candidates are the selector's own picks on the fixture days (a real chain, real quotes), so every figure is one the
system would actually produce: the iron condor on the sideways day, the iron butterfly on a pinned day, the bull put and
bear call credit spreads on a steady trend with rich premium. Nothing here touches a network or a broker."""
import datetime
import tempfile
import types
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core import approval, legs as legs_mod
from nifty_core.costs.models import BUY, SELL
from nifty_core.data.fixture_provider import FixtureProvider, Scenario
from nifty_core.execution import paper as X
from nifty_core.market.ist import parse_ist
from nifty_core.positions import manager as M
from nifty_core.positions import params as PP
from nifty_core.regime import params as RP
from nifty_core.risk import engine as R
from nifty_core.selector import engine as E
from nifty_core.strategy import engine as strategy_engine
from tests import _scenarios as sc
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase
from tests.unit import test_risk as TR
from tests.unit.test_selector import HV, selection

ENGINE = TR.ENGINE
DAY = sc.scenario("sideways").day
T1230 = parse_ist("%s 12:30" % DAY)
T1430 = parse_ist("%s 14:30" % DAY)
SEC = datetime.timedelta(seconds=1)
RP_DEFAULT, PP_DEFAULT = RP.load_params(), PP.load_params()


def picks():
    """name -> the selector's Selection on the fixture day that makes it the pick."""
    return {
        "IRON_CONDOR": selection("sideways", "12:30"),
        "IRON_BUTTERFLY": H.decide("sideways", "12:30", H.pinned(5)),
        "BULL_PUT_CREDIT": H.decide("bullish_trend", "12:30", H.trending(4, 3), H.params(**HV)),
        "BEAR_CALL_CREDIT": H.decide("bullish_trend", "12:30", H.trending(-4, 3), H.params(**HV)),
    }


class Base(NoNetworkTestCase):
    @classmethod
    def setUpClass(cls):
        super(Base, cls).setUpClass()
        cls.picks = picks()

    def setUp(self):
        super(Base, self).setUp()
        self.cfg = sc.config()
        self.kill = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.kill, ignore_errors=True))

    def manager(self, cfg=None, day=DAY):
        return R.RiskManager(cfg or self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill, day)

    def candidate(self, kind="IRON_CONDOR"):
        sel = self.picks[kind]
        self.assertEqual(sel.outcome, E.TRADE, kind)
        return sel.candidate

    def executor(self, mgr, scenario="sideways"):
        return X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), mgr, sc.provider(scenario))


class SelectionCarriesATradableCandidateTests(Base):
    def test_every_strategy_comes_with_a_candidate_of_the_matching_kind(self):
        for kind, sel in self.picks.items():
            self.assertEqual(sel.candidate.strategy_type, kind)
            self.assertEqual(sel.setup["kind"], kind)

    def test_the_debit_spread_is_the_engines_own_candidate(self):
        sel = selection("bullish_trend", "12:30")
        self.assertEqual(sel.selected, E.DEBIT)
        self.assertTrue(hasattr(sel.candidate, "spread"))                      # unchanged path for the debit spread
        self.assertEqual(legs_mod.leg_plan(sel.candidate)[0][0], "long")       # and its leg ids are unchanged

    def test_a_credit_candidate_fixes_its_exit_plan_before_entry(self):
        for kind in ("IRON_CONDOR", "BULL_PUT_CREDIT"):
            plan = self.candidate(kind).exit_plan
            self.assertGreater(Dec(plan["target_gross_inr"]), 0)
            self.assertGreater(Dec(plan["stop_gross_inr"]), 0)
            self.assertLessEqual(Dec(plan["stop_gross_inr"]), Dec(plan["max_loss_inr"]) * Dec("0.6") + 1)   # never beyond 60% of the worst case
            self.assertIn(plan["invalidate"]["rule"], ("close_below", "close_above", "outside_range"))

    def test_no_selection_leaves_the_proposal_only_status(self):
        self.assertIsNone(selection("sideways", "09:45").candidate)            # NO_TRADE selections carry no candidate


class RiskGateTests(Base):
    def test_every_strategy_is_approved_at_exactly_the_selectors_worst_case(self):
        for kind, sel in self.picks.items():
            d = self.manager(day=sel.candidate.decision_time.date()).request_entry(sel.candidate, sel.candidate.decision_time)
            self.assertEqual(d.outcome, R.APPROVED, (kind, [r.text for r in d.reasons]))
            self.assertEqual(d.calculation["total_defined_risk_inr"], Dec(sel.setup["worst_case_with_costs_inr"]))
            self.assertLessEqual(d.calculation["total_defined_risk_inr"], self.cfg.risk_per_trade_inr)
            self.assertEqual(d.approval.strategy_type, kind)

    def test_the_condors_approval_names_every_leg(self):
        d = self.manager().request_entry(self.candidate(), T1230)
        self.assertEqual(len(d.approval.long_symbol.split("+")), 2)
        self.assertEqual(len(d.approval.short_symbol.split("+")), 2)

    def test_a_worst_case_above_the_per_trade_limit_is_blocked(self):
        cfg = self.cfg._replace(risk_per_trade_inr=Dec("100"))
        d = self.manager(cfg).request_entry(self.candidate(), T1230)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("PER_TRADE_RISK_EXCEEDED", [r.code for r in d.reasons])

    def test_a_candidate_whose_figures_disagree_with_its_structure_is_blocked(self):
        c = self.candidate()
        d = self.manager().request_entry(c._replace(net_credit_points=c.net_credit_points + 5), T1230)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("CANDIDATE_INCONSISTENT", [r.code for r in d.reasons])

    def test_a_candidate_without_a_validated_structure_is_blocked(self):
        d = self.manager().request_entry(self.candidate()._replace(structure=None), T1230)
        self.assertEqual((d.outcome, d.reasons[0].code), (R.BLOCKED, "CANDIDATE_INVALID"))

    def forged(self, drop):
        """A CreditStructure whose legs were tampered with AFTER it was validated (create() itself refuses to build one)."""
        c = self.candidate()
        s = c.structure
        legs = [l for i, l in enumerate(s.legs) if i != drop]
        f = object.__new__(type(s))
        for slot in type(s).__slots__:
            object.__setattr__(f, slot, getattr(s, slot))
        object.__setattr__(f, "legs", tuple(legs))
        return c._replace(structure=f)

    def test_a_structure_stripped_of_a_protective_wing_is_blocked_as_a_naked_short(self):
        c = self.candidate()
        wing = next(i for i, l in enumerate(c.structure.legs) if l.is_long)
        d = self.manager().request_entry(self.forged(wing), T1230)
        codes = [r.code for r in d.reasons]
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("NAKED_SHORT", codes)
        self.assertIn("STRUCTURE_INVALID", codes)                              # and it has the wrong number of legs

    def test_a_structure_with_legs_of_mixed_quantity_or_no_long_leg_is_blocked(self):
        c = self.candidate()
        longs = [i for i, l in enumerate(c.structure.legs) if l.is_long]
        stripped = self.forged(longs[0])
        stripped = stripped._replace(structure=self._with_legs(stripped.structure, [l for l in stripped.structure.legs if not l.is_long]))
        d = self.manager().request_entry(stripped, T1230)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("STRUCTURE_INVALID", [r.code for r in d.reasons])

    @staticmethod
    def _with_legs(structure, legs):
        f = object.__new__(type(structure))
        for slot in type(structure).__slots__:
            object.__setattr__(f, slot, getattr(structure, slot))
        object.__setattr__(f, "legs", tuple(legs))
        return f

    def test_a_stale_candidate_is_blocked(self):
        d = self.manager().request_entry(self.candidate(), T1230 + 90 * SEC)
        self.assertIn("CANDIDATE_STALE", [r.code for r in d.reasons])

    def test_the_same_condor_cannot_be_approved_twice_or_added_to(self):
        m, c = self.manager(), self.candidate()
        self.assertEqual(m.request_entry(c, T1230).outcome, R.APPROVED)
        again = m.request_entry(c._replace(decision_time=T1230 + SEC), T1230 + SEC)
        codes = [r.code for r in again.reasons]
        self.assertEqual(again.outcome, R.BLOCKED)
        self.assertIn("AVERAGING_BLOCKED", codes)                              # the open condor already holds these contracts
        self.assertIn("MAX_LOTS_EXCEEDED_COMBINED", codes)

    def test_the_fingerprint_changes_when_any_leg_price_changes(self):
        c = self.candidate()
        legs = list(c.structure.legs)
        moved = c._replace(structure=c.structure.__class__.create(
            c.structure.kind, [legs[0]._replace(entry_price=legs[0].entry_price + Dec("0.05"))] + legs[1:], sc.specs(), DAY, "fixture"))
        self.assertNotEqual(R.candidate_fingerprint(c), R.candidate_fingerprint(moved))

    def test_the_defined_risk_is_the_loss_cap_plus_slippage_and_costs(self):
        c = self.candidate()
        calc = R.calculate(c, self.cfg, ENGINE, DAY)
        s = c.structure
        self.assertEqual(calc["max_loss_inr"], (s.width_points - s.net_credit_points) * s.units)
        self.assertEqual(calc["total_defined_risk_inr"], calc["max_loss_inr"] + calc["entry_slippage_inr"]
                         + calc["entry_costs_inr"] + calc["estimated_exit_costs_inr"])
        self.assertEqual(calc["entry_risk_inr"], calc["total_defined_risk_inr"] - calc["estimated_exit_costs_inr"])


class ExecutorTests(Base):
    def enter(self, kind="IRON_CONDOR", scenario="sideways"):
        c = self.candidate(kind)
        mgr = self.manager(day=c.decision_time.date())
        d = mgr.request_entry(c, c.decision_time)
        ex = self.executor(mgr, scenario)
        return c, mgr, ex, d, ex.execute_entry(d.approval, c, c.lots, c.decision_time)

    def test_all_four_legs_fill_together_at_adverse_prices(self):
        c, mgr, ex, d, r = self.enter()
        self.assertEqual(r.status, X.FILLED)
        self.assertEqual([f.leg_id for f in r.legs], ["leg0", "leg1", "leg2", "leg3"])
        slip = self.cfg.slippage_points_per_leg
        for f in r.legs:
            self.assertEqual(f.fill_price, f.ask + slip if f.side == BUY else f.bid - slip)   # buy the ask, sell the bid
            self.assertEqual(f.units, 65)
        self.assertEqual(sorted(f.side for f in r.legs), [BUY, BUY, SELL, SELL])
        self.assertLess(Dec(r.actual["net_debit_paid_inr"]), 0)                # a credit: money comes in

    def test_each_strategy_can_be_entered_and_the_fills_match_the_candidate_legs(self):
        for kind in self.picks:
            scenario = "sideways" if kind in ("IRON_CONDOR", "IRON_BUTTERFLY") else "bullish_trend"
            c, mgr, ex, d, r = self.enter(kind, scenario)
            self.assertEqual(r.status, X.FILLED, (kind, [x.text for x in r.reasons]))
            self.assertEqual([f.contract for f in r.legs], [l.contract for l in c.structure.legs])
            self.assertLessEqual(Dec(r.actual["defined_risk_at_fill_inr"]), d.approval.defined_risk_inr)

    def test_one_dead_leg_means_no_fill_at_all(self):
        c = self.candidate()
        scen = sc.scenario("sideways")
        chain = scen.chains[2]                                                  # the 12:30 chain
        dead = c.structure.legs[2].contract
        chain = type(chain)(chain.underlying, chain.snapshot_time, chain.spot,
                            [q._replace(bid=Dec(0)) if q.contract == dead else q for q in chain.quotes],
                            chain.data_label, chain.source)
        provider = FixtureProvider(Scenario(scen.name, scen.day, scen.underlying, scen.interval_minutes, scen.data_label,
                                            scen.candles, [chain], scen.events, scen.meta))
        mgr = self.manager()
        d = mgr.request_entry(c, T1230)
        ex = X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), mgr, provider)
        r = ex.execute_entry(d.approval, c, c.lots, T1230)
        self.assertEqual(r.status, X.PENDING)
        self.assertIn("ONE_SIDED_QUOTE", [x.code for x in r.reasons])
        self.assertEqual(ex.open_positions(), ())                               # no single-leg position was left behind

    def test_the_exit_closes_every_leg_the_other_way_and_the_pnl_reconciles(self):
        c, mgr, ex, d, entry = self.enter()
        pid = entry.execution_id
        r = ex.execute_exit(pid, T1430, "MANUAL")
        self.assertEqual(r.status, X.FILLED, [x.text for x in r.reasons])
        by_leg = {f.leg_id: f for f in entry.legs}
        for f in r.legs:
            self.assertEqual(f.side, SELL if by_leg[f.leg_id].side == BUY else BUY)
        gross = sum(((f.fill_price - by_leg[f.leg_id].fill_price) if by_leg[f.leg_id].side == BUY
                     else (by_leg[f.leg_id].fill_price - f.fill_price)) * f.units for f in r.legs)
        self.assertEqual(Dec(r.actual["gross_pnl_inr"]), gross.quantize(Dec("0.01")))
        net = Dec(r.actual["gross_pnl_inr"]) - Dec(r.actual["actual_entry_costs_inr"]) - Dec(r.actual["actual_exit_costs"]["total_inr"])
        self.assertEqual(Dec(r.actual["net_pnl_inr"]), net)
        self.assertEqual(mgr.snapshot().realized_pnl_inr, net)                  # and the risk ledger got the same figure
        self.assertEqual(mgr.snapshot().open_positions, 0)

    def test_a_fill_that_would_exceed_the_approved_risk_is_refused(self):
        c, mgr, ex, d, entry = self.enter()
        tight = d.approval._replace(defined_risk_inr=d.approval.defined_risk_inr - Dec("500"))
        mgr2 = self.manager()
        d2 = mgr2.request_entry(c, T1230)
        ex2 = self.executor(mgr2)
        with mock.patch.object(R, "approval_is_authentic", return_value=True):
            r = ex2.execute_entry(d2.approval._replace(defined_risk_inr=d2.approval.defined_risk_inr - Dec("500")), c, c.lots, T1230)
        self.assertEqual(r.status, X.PENDING)
        self.assertIn("FILL_EXCEEDS_APPROVED_RISK", [x.code for x in r.reasons])


class ManagerTests(Base):
    def open_position(self, kind="IRON_CONDOR", setup_exit_fn=None, label="SIDEWAYS"):
        c = self.candidate(kind)
        mgr = self.manager()
        d = mgr.request_entry(c, T1230)
        ex = self.executor(mgr)
        pm = M.PositionManager(self.cfg, PP_DEFAULT, ex, mgr, sc.provider("sideways"), sc.calendar(), RP_DEFAULT, ENGINE,
                               regime_fn=lambda now: types.SimpleNamespace(label=label), setup_exit_fn=setup_exit_fn)
        result, pos = pm.enter(d.approval, c, c.lots, T1230)
        self.assertEqual(result.status, X.FILLED)
        return c, pm, pos.position_id

    @staticmethod
    def unrealized(gross):
        g = Dec(gross)
        return {"label": "x", "gross_pnl_inr": g, "actual_entry_costs_inr": Dec(40), "estimated_exit_costs_inr": Dec(40),
                "net_pnl_inr": g - 80}

    def test_a_fresh_condor_is_held(self):
        c, pm, pid = self.open_position()
        self.assertEqual(pm.evaluate(pid, T1230 + 5 * SEC).action, M.HOLD)

    def test_the_plans_gross_target_closes_the_position(self):
        c, pm, pid = self.open_position()
        target = Dec(c.exit_plan["target_gross_inr"])
        with mock.patch.object(M.PositionManager, "_unrealized", return_value=self.unrealized(target)):
            d = pm.evaluate(pid, T1230 + 5 * SEC)
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.PROFIT_TARGET))

    def test_just_below_the_target_and_just_above_the_stop_are_held(self):
        c, pm, pid = self.open_position()
        target, stop = Dec(c.exit_plan["target_gross_inr"]), Dec(c.exit_plan["stop_gross_inr"])
        for gross in (target - 1, Dec(0), -(stop - 1)):
            with mock.patch.object(M.PositionManager, "_unrealized", return_value=self.unrealized(gross)):
                self.assertEqual(pm.evaluate(pid, T1230 + 5 * SEC).action, M.HOLD, gross)

    def test_the_plans_gross_stop_closes_the_position(self):
        c, pm, pid = self.open_position()
        stop = Dec(c.exit_plan["stop_gross_inr"])
        with mock.patch.object(M.PositionManager, "_unrealized", return_value=self.unrealized(-stop)):
            d = pm.evaluate(pid, T1230 + 5 * SEC)
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.STOP_LOSS))

    def test_a_credit_structure_does_not_use_the_debit_percentages(self):
        """Net P&L of 962 would already take profit under the debit rule (60% of the defined risk is 924), but the condor's
        own plan wants a gross 1,043: the plan, not the debit rule, decides."""
        c, pm, pid = self.open_position()
        defined = pm._ex.position(pid).approval.defined_risk_inr
        debit_rule_target = defined * PP_DEFAULT.profit_target_pct_of_defined_risk / 100
        gross = Dec(c.exit_plan["target_gross_inr"]) - 1
        self.assertGreaterEqual(gross - 80, debit_rule_target)
        with mock.patch.object(M.PositionManager, "_unrealized", return_value=self.unrealized(gross)):
            self.assertEqual(pm.evaluate(pid, T1230 + 5 * SEC).action, M.HOLD)

    def test_the_setup_exit_function_receives_the_position_candidate(self):
        seen = []
        c, pm, pid = self.open_position(setup_exit_fn=lambda now, label, cand: seen.append((label, cand)) or "range broke")
        d = pm.evaluate(pid, T1230 + 5 * SEC)
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.SETUP_INVALIDATED))
        self.assertEqual(seen[0][0], "NEUTRAL")
        self.assertIs(seen[0][1], c)

    def test_the_forced_flat_by_time_closes_a_condor_like_any_position(self):
        c, pm, pid = self.open_position()
        d = pm.evaluate(pid, parse_ist("%s 15:15" % DAY))
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.FORCED_FLAT_BY))

    def test_abnormal_volatility_invalidates_every_structure(self):
        c, pm, pid = self.open_position(label="HIGH_VOLATILITY")
        d = pm.evaluate(pid, T1230 + 5 * SEC)
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.REGIME_INVALIDATION))

    def test_a_second_structure_on_the_same_contracts_is_refused_by_the_manager_too(self):
        c, pm, pid = self.open_position()
        self.assertEqual(len(pm._entry_conflicts(c, 1)), 2)                    # duplicate contracts + the lot ceiling

    def test_the_directional_credit_spread_enters_as_bullish_and_the_condor_as_neutral(self):
        self.assertEqual(self.candidate("BULL_PUT_CREDIT").regime_at_entry["label"], "BULLISH")
        self.assertEqual(self.candidate("BEAR_CALL_CREDIT").regime_at_entry["label"], "BEARISH")
        self.assertEqual(self.candidate("IRON_CONDOR").regime_at_entry["label"], "NEUTRAL")


class TicketTests(Base):
    def ticket(self, kind="IRON_CONDOR"):
        c = self.candidate(kind)
        d = self.manager().request_entry(c, T1230)
        return c, approval.make_ticket(c, d)

    def test_the_ticket_lists_every_leg_and_the_risk(self):
        c, t = self.ticket()
        self.assertEqual(t.text.count("BUY "), 2)
        self.assertEqual(t.text.count("SELL"), 2)
        self.assertIn("Iron Condor", t.text)
        self.assertIn("net credit", t.text)
        self.assertIn("Total defined risk", t.text)
        for leg in c.structure.legs:
            self.assertIn(leg.contract.symbol, t.text)

    def test_a_clean_yes_for_this_ticket_counts_and_a_wrong_number_does_not(self):
        c, t = self.ticket()
        self.assertTrue(approval.parse_yes("yes", t)[0])
        self.assertTrue(approval.parse_yes("yes %s" % t.fingerprint, t)[0])
        strike = c.structure.legs[0].strike
        self.assertTrue(approval.parse_yes("yes %s" % strike, t)[0])           # a figure on the ticket
        self.assertFalse(approval.parse_yes("yes 99999", t)[0])                # not a figure on this order

    def test_each_kind_has_a_title(self):
        for kind in ("BULL_PUT_CREDIT", "BEAR_CALL_CREDIT", "IRON_BUTTERFLY"):
            c, t = self.ticket(kind)
            self.assertIn(approval.KIND_TITLES[kind], t.text)


class LegHelperTests(Base):
    def test_debit_leg_ids_and_symbol_fields_are_unchanged(self):
        sel = selection("bullish_trend", "12:30")
        self.assertEqual([p[0] for p in legs_mod.leg_plan(sel.candidate)], ["long", "short"])
        long_s, short_s = legs_mod.symbol_fields(sel.candidate)
        self.assertNotIn("+", long_s + short_s)

    def test_split_symbols_handles_single_and_joined_fields(self):
        self.assertEqual(legs_mod.split_symbols("A", "B"), {"A", "B"})
        self.assertEqual(legs_mod.split_symbols("A+B", "C+D"), {"A", "B", "C", "D"})
        self.assertEqual(legs_mod.split_symbols("", None), set())

    def test_condor_symbols_are_all_four_contracts(self):
        c = self.candidate()
        self.assertEqual(len(legs_mod.symbols(c)), 4)
        self.assertTrue(legs_mod.is_structure_candidate(c))
        self.assertTrue(legs_mod.is_candidate(c))
        self.assertFalse(legs_mod.is_candidate(object()))

    def test_gross_pnl_is_signed_by_the_entry_side(self):
        c = self.candidate()
        mgr = self.manager()
        d = mgr.request_entry(c, T1230)
        ex = self.executor(mgr)
        entry = ex.execute_entry(d.approval, c, c.lots, T1230)
        same = ex.preview_exit(entry.execution_id, T1230 + 5 * SEC)[0]
        # closing at the same quotes (adverse slippage both ways) can only lose: gross is the round-trip slippage
        gross = legs_mod.gross_pnl(c, ex.position(entry.execution_id).entry_fills, same)
        self.assertLess(gross, 0)


if __name__ == "__main__":
    unittest.main()
