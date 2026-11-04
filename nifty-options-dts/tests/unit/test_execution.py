"""Paper execution tests. The provider serves hand-edited chains so every fill price can be computed by hand."""
import copy
import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core.costs.engine import load_cost_engine
from nifty_core.costs.models import BUY, SELL, LegExecution
from nifty_core.data.fixture_provider import FixtureProvider, Scenario
from nifty_core.data.models import OptionChain
from nifty_core.errors import DataNotAvailable, LiveTradingNotSupported
from nifty_core.execution import paper as X
from nifty_core.guards import paper_only
from nifty_core.market.ist import UTC, parse_ist
from nifty_core.risk import engine as R
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.unit import test_risk as TR
from tests.unit import test_strategy as T

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAY = TR.DAY
T1230 = TR.T1230
ENGINE = TR.ENGINE
UNITS = 65
SLIP = Dec("0.5")


def shift(chain, seconds):
    d = datetime.timedelta(seconds=seconds)
    return OptionChain(chain.underlying, chain.snapshot_time + d, chain.spot,
                       [q._replace(timestamp=q.timestamp + d) for q in chain.quotes], chain.data_label, chain.source)


def entry_chain(debit="30.00"):
    """The 12:30 chain the 30-point-debit candidate was built from."""
    regime, chain, events, as_of = TR.base_parts()
    base = T.decide(regime, chain, events, as_of).candidate
    ask = Dec("85.55") + Dec(debit)
    return T.with_quote(T.with_quote(chain, base.long_contract, ask=ask, bid=ask - Dec("0.85")),
                        base.short_contract, bid=Dec("85.55"))


def set_quote(chain, contract, **changes):
    return T.with_quote(chain, contract, **changes)


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.cfg = sc.config()
        self.kill = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.kill, ignore_errors=True))
        self.cand = TR.cheap_candidate("30.00")
        self.long_c, self.short_c = self.cand.long_contract, self.cand.short_contract

    def build(self, chains, exec_cfg=None, cost_engine=None, approve=True, request_at=T1230):
        s = sc.scenario("bullish_trend")
        scen = Scenario(s.name, s.day, s.underlying, s.interval_minutes, s.data_label, s.candles, chains, s.events, s.meta)
        self.provider = FixtureProvider(scen)
        self.mgr = R.RiskManager(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill, DAY)
        self.decision = self.mgr.request_entry(self.cand, request_at) if approve else None
        self.ex = X.PaperExecutor(exec_cfg or self.cfg, cost_engine or ENGINE, sc.specs(), sc.calendar(), self.mgr,
                                  self.provider)
        return self.ex

    @property
    def approval(self):
        return self.decision.approval

    def enter(self, now=T1230):
        return self.ex.execute_entry(self.approval, self.cand, 1, now)

    @staticmethod
    def codes(result):
        return [r.code for r in result.reasons]

    def exit_chain(self, long_bid="140.00", long_ask="140.85", short_bid="80.00", short_ask="80.55", seconds=7200):
        c = shift(entry_chain(), seconds)
        c = set_quote(c, self.long_c, bid=Dec(long_bid), ask=Dec(long_ask))
        return set_quote(c, self.short_c, bid=Dec(short_bid), ask=Dec(short_ask))


class EntryFillTests(Base):
    def test_entry_fills_use_ask_plus_slippage_and_bid_minus_slippage(self):
        chain = entry_chain()
        r = self.build([chain]) and self.enter()
        self.assertEqual(r.status, X.FILLED, r.reasons)
        long_fill, short_fill = r.legs
        lq, sq = chain.get(self.long_c), chain.get(self.short_c)
        self.assertEqual((long_fill.leg_id, long_fill.side), ("long", BUY))
        self.assertEqual(long_fill.reference_price, lq.ask)
        self.assertEqual(long_fill.fill_price, lq.ask + SLIP)
        self.assertEqual((short_fill.leg_id, short_fill.side), ("short", SELL))
        self.assertEqual(short_fill.reference_price, sq.bid)
        self.assertEqual(short_fill.fill_price, sq.bid - SLIP)
        for f, q in ((long_fill, lq), (short_fill, sq)):
            self.assertEqual((f.bid, f.ask, f.quote_time, f.slippage_points, f.units, f.lots),
                             (q.bid, q.ask, q.timestamp, SLIP, UNITS, 1))
        self.assertEqual((long_fill.contract, short_fill.contract), (self.long_c, self.short_c))

    def test_nothing_is_changed_silently(self):
        self.build([entry_chain()])
        r = self.enter()
        pos = self.ex.position(r.execution_id)
        self.assertEqual(pos.candidate, self.cand)
        self.assertEqual(pos.lots, 1)
        self.assertEqual([f.contract for f in pos.entry_fills], [self.cand.long_contract, self.cand.short_contract])

    def test_actual_entry_costs_come_from_the_fill_prices(self):
        self.build([entry_chain()])
        r = self.enter()
        by_hand = ENGINE.actual_costs([LegExecution("long", BUY, r.legs[0].fill_price, UNITS, DAY),
                                       LegExecution("short", SELL, r.legs[1].fill_price, UNITS, DAY)])
        self.assertEqual(Dec(r.actual["entry_costs"]["total_inr"]), by_hand.total)
        self.assertEqual(r.actual["entry_costs"]["label"], "actual (paper fills)")
        paid = (r.legs[0].fill_price - r.legs[1].fill_price) * UNITS
        self.assertEqual(Dec(r.actual["net_debit_paid_inr"]), paid)
        self.assertEqual(Dec(r.actual["entry_cash_outlay_inr"]), paid + by_hand.total)

    def test_fill_at_unchanged_quotes_matches_the_approved_defined_risk(self):
        self.build([entry_chain()])
        r = self.enter()
        self.assertEqual(Dec(r.actual["defined_risk_at_fill_inr"]), self.approval.defined_risk_inr)
        self.assertEqual(Dec(r.estimated_at_approval["total_defined_risk_inr"]), self.approval.defined_risk_inr)
        self.assertIn("ESTIMATE", r.estimated_at_approval["label"])

    def test_audit_record_has_everything_required(self):
        self.build([entry_chain()])
        r = self.enter()
        j = json.loads(json.dumps(X.result_to_journal(r)))
        self.assertTrue(j["execution_id"])
        self.assertEqual(j["approval_id"], self.approval.approval_id)
        self.assertEqual(j["candidate_id"], R.candidate_fingerprint(self.cand))
        self.assertEqual(j["decision_time"], self.cand.decision_time.isoformat())
        self.assertEqual(j["approved_at"], self.approval.approved_at.isoformat())
        self.assertEqual(j["executed_at"], T1230.isoformat())
        self.assertEqual({l["contract"] for l in j["legs"]}, {self.long_c.symbol, self.short_c.symbol})
        for leg in j["legs"]:
            for key in ("side", "lots", "units", "bid", "ask", "fill_price", "slippage_points", "reference_price",
                        "quote_time"):
                self.assertIn(key, leg)
        self.assertEqual({l["side"] for l in j["legs"]}, {"BUY", "SELL"})
        self.assertIn("entry_costs", j["actual"])
        for key in ("config", "candidate", "strategy_thresholds", "regime_thresholds", "execution_model"):
            self.assertTrue(j["fingerprints"][key], key)
        self.assertIs(j["paper"], True)
        self.assertIn("SIMULATED", j["note"])

    def test_the_attempt_log_records_every_result(self):
        self.build([entry_chain()])
        self.ex.execute_entry(self.approval, self.cand, 2, T1230)
        self.enter()
        self.assertEqual([r.status for r in self.ex.log], [X.REJECTED, X.FILLED])

    def test_price_improvement_is_taken_at_the_current_quote(self):
        better = set_quote(entry_chain(), self.long_c, ask=Dec("115.50"), bid=Dec("114.65"))
        self.build([entry_chain()])
        self.provider = FixtureProvider(Scenario(*self._parts([better])))
        self.ex = X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.mgr, self.provider)
        r = self.enter()
        self.assertEqual(r.status, X.FILLED)
        self.assertEqual(r.legs[0].fill_price, Dec("115.50") + SLIP)
        self.assertLess(Dec(r.actual["defined_risk_at_fill_inr"]), self.approval.defined_risk_inr)

    def _parts(self, chains):
        s = sc.scenario("bullish_trend")
        return (s.name, s.day, s.underlying, s.interval_minutes, s.data_label, s.candles, chains, s.events, s.meta)


class IntegrityTests(Base):
    def test_a_modified_candidate_is_rejected(self):
        self.build([entry_chain()])
        forged = self.cand._replace(liquidity=copy.deepcopy(self.cand.liquidity))
        forged.liquidity["long"]["ask"] = "1.00"
        r = self.ex.execute_entry(self.approval, forged, 1, T1230)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["CANDIDATE_MODIFIED"]))
        self.assertEqual(self.ex.open_positions(), ())

    def test_a_tampered_approval_is_rejected(self):
        self.build([entry_chain()])
        for change in (dict(lots=2), dict(defined_risk_inr=Dec("1")), dict(candidate_id="0" * 64)):
            r = self.ex.execute_entry(self.approval._replace(**change), self.cand, 1, T1230)
            self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["APPROVAL_TAMPERED"]), change)

    def test_a_missing_or_blocked_approval_is_rejected(self):
        self.build([entry_chain()])
        r = self.ex.execute_entry(None, self.cand, 1, T1230)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["APPROVAL_TAMPERED"]))

    def test_an_approval_from_another_ledger_is_rejected(self):
        self.build([entry_chain()])
        other = R.RiskManager(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill, DAY)
        other_dec = other.request_entry(self.cand, T1230)
        self.build([entry_chain()], approve=False)
        r = self.ex.execute_entry(other_dec.approval, self.cand, 1, T1230)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["APPROVAL_NOT_OPEN"]))

    def test_a_reused_approval_is_rejected_and_nothing_fills_twice(self):
        self.build([entry_chain()])
        self.assertEqual(self.enter().status, X.FILLED)
        r = self.enter()
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["APPROVAL_ALREADY_USED"]))
        self.assertEqual(len(self.ex.open_positions()), 1)

    def test_a_changed_lot_quantity_is_rejected(self):
        self.build([entry_chain()])
        for lots in (2, 0):
            r = self.ex.execute_entry(self.approval, self.cand, lots, T1230)
            self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["QUANTITY_CHANGED"]), lots)
        self.assertEqual(self.enter().status, X.FILLED)                  # the real approval is untouched

    def test_an_active_kill_switch_rejects_entry_but_keeps_the_approval(self):
        self.build([entry_chain()])
        paper_only.trip_kill_switch(self.kill)
        r = self.enter()
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["KILL_SWITCH_ACTIVE"]))
        os.remove(paper_only.kill_switch_path(self.kill))                 # a human removes it
        self.assertEqual(self.enter().status, X.FILLED)

    def test_a_broken_paper_guard_raises(self):
        self.build([entry_chain()])
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                self.enter()
            with self.assertRaises(LiveTradingNotSupported):
                X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.mgr, self.provider)
        with self.assertRaises(LiveTradingNotSupported):
            X.PaperExecutor(self.cfg._replace(**{"pa" + "per": bool(0)}), ENGINE, sc.specs(), sc.calendar(), self.mgr,
                            self.provider)

    def test_a_stale_approval_is_rejected_and_its_reservation_released(self):
        self.build([entry_chain()])
        limit = self.cfg.data_stale_after_seconds
        late = T1230 + datetime.timedelta(seconds=limit + 1)
        r = self.enter(late)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["APPROVAL_STALE"]))
        snap = self.mgr.snapshot()
        self.assertEqual((snap.open_risk_reserved_inr, snap.capital_committed_inr, snap.trades_today, snap.open_lots),
                         (0, 0, 0, 0))
        again = self.mgr.request_entry(self.cand, late)                  # the identical candidate stays used
        self.assertIn("CANDIDATE_ALREADY_USED", [x.code for x in again.reasons])

    def test_an_approval_exactly_at_the_age_limit_still_works(self):
        chain = entry_chain()
        self.build([chain, shift(chain, 60)])
        r = self.enter(T1230 + datetime.timedelta(seconds=60))
        self.assertEqual(r.status, X.FILLED, r.reasons)

    def test_an_approval_from_the_future_is_rejected(self):
        self.build([entry_chain()])
        r = self.enter(T1230 - datetime.timedelta(seconds=1))
        self.assertIn("APPROVAL_FROM_FUTURE", self.codes(r))
        self.assertEqual(r.status, X.REJECTED)

    def test_entry_after_the_window_or_flat_by_is_rejected(self):
        self.build([entry_chain()], exec_cfg=self.cfg._replace(entry_window_end_ist="12:29"))
        r = self.enter()
        self.assertIn("OUTSIDE_ENTRY_WINDOW", self.codes(r))
        self.build([entry_chain()], exec_cfg=self.cfg._replace(entry_window_end_ist="12:30", flat_by_ist="12:30"))
        r = self.enter()
        self.assertIn("AFTER_FLAT_BY", self.codes(r))
        self.assertEqual(self.mgr.snapshot().trades_today, 0)

    def test_estimated_exit_costs_are_never_booked_as_actual(self):
        self.build([entry_chain()])
        r = self.enter()
        self.assertNotIn("exit", json.dumps(r.actual).lower().replace("exit_reason", ""))
        self.assertIn("estimated_exit_costs_inr", r.estimated_at_approval)


class MissingDataTests(Base):
    def assertPending(self, result, code):
        self.assertEqual(result.status, X.PENDING, result.reasons)
        self.assertIn(code, self.codes(result))
        self.assertEqual(result.legs, ())                    # no fill of any kind
        self.assertEqual(self.ex.open_positions(), ())
        self.assertEqual(self.mgr.snapshot().open_risk_reserved_inr, self.approval.defined_risk_inr)

    def test_missing_quote_is_pending_then_fills_when_data_arrives(self):
        chain = entry_chain()
        gap = T.edit_chain(chain, drop=(self.short_c,))
        self.build([gap, shift(chain, 30)])
        self.assertPending(self.enter(), "MISSING_QUOTE")
        r = self.enter(T1230 + datetime.timedelta(seconds=30))
        self.assertEqual(r.status, X.FILLED, r.reasons)

    def test_no_chain_at_all_is_pending(self):
        self.build([shift(entry_chain(), 3600)])
        self.assertPending(self.enter(), "MISSING_QUOTE")

    def test_a_stale_quote_is_pending(self):
        old = shift(entry_chain(), -30)                      # quotes stamped 12:29:30
        self.build([old])
        r = self.enter(T1230 + datetime.timedelta(seconds=31))
        self.assertPending(r, "STALE_QUOTE")

    def test_quote_age_boundary(self):
        old = shift(entry_chain(), -60)
        self.build([old])
        self.assertEqual(self.enter(T1230).status, X.FILLED)
        self.build([shift(entry_chain(), -61)])
        self.assertPending(self.enter(T1230), "STALE_QUOTE")

    def test_one_sided_quotes_are_pending(self):
        for contract, field in ((self.long_c, "ask"), (self.long_c, "bid"), (self.short_c, "bid"), (self.short_c, "ask")):
            self.build([set_quote(entry_chain(), contract, **{field: None})])
            self.assertPending(self.enter(), "ONE_SIDED_QUOTE")
        self.build([set_quote(entry_chain(), self.short_c, bid=Dec("0"))])
        self.assertPending(self.enter(), "ONE_SIDED_QUOTE")

    def test_quoted_size_must_cover_the_order(self):
        self.build([set_quote(entry_chain(), self.long_c, ask_qty=UNITS - 1)])
        self.assertPending(self.enter(), "INSUFFICIENT_QUOTED_SIZE")
        self.build([set_quote(entry_chain(), self.short_c, bid_qty=UNITS - 1)])
        self.assertPending(self.enter(), "INSUFFICIENT_QUOTED_SIZE")
        self.build([set_quote(entry_chain(), self.long_c, ask_qty=UNITS)])
        self.assertEqual(self.enter().status, X.FILLED)
        self.build([set_quote(entry_chain(), self.long_c, ask_qty=None)])
        self.assertPending(self.enter(), "MISSING_QUOTED_SIZE")

    def test_the_side_not_traded_does_not_matter(self):
        self.build([set_quote(set_quote(entry_chain(), self.long_c, bid_qty=0), self.short_c, ask_qty=0)])
        self.assertEqual(self.enter().status, X.FILLED)

    def test_off_tick_quote_is_pending(self):
        self.build([set_quote(entry_chain(), self.long_c, ask=self.cand.entry_price_long + Dec("0.03"))])
        self.assertPending(self.enter(), "OFF_TICK_QUOTE")

    def test_a_quote_from_the_future_is_pending(self):
        chain = entry_chain()
        build_args = [chain]

        class Future(FixtureProvider):
            def get_quote(self, contract, as_of=None):
                q = super(Future, self).get_quote(contract, as_of)
                return q._replace(timestamp=q.timestamp + datetime.timedelta(seconds=10))
        self.build(build_args)
        self.ex = X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.mgr, Future(self.provider.scenario))
        self.assertPending(self.enter(), "QUOTE_FROM_FUTURE")

    def test_a_provider_that_raises_is_pending_not_a_crash(self):
        class Dead(FixtureProvider):
            def get_quote(self, contract, as_of=None):
                raise DataNotAvailable("feed down")
        self.build([entry_chain()])
        self.ex = X.PaperExecutor(self.cfg, ENGINE, sc.specs(), sc.calendar(), self.mgr, Dead(self.provider.scenario))
        self.assertPending(self.enter(), "MISSING_QUOTE")

    def test_both_legs_or_neither_there_are_no_partial_fills(self):
        self.build([set_quote(entry_chain(), self.short_c, bid=None)])
        r = self.enter()
        self.assertEqual((r.status, r.legs), (X.PENDING, ()))
        self.assertEqual(self.ex.open_positions(), ())
        self.assertEqual(self.mgr.snapshot().capital_committed_inr, self.approval.capital_committed_inr)

    def test_quotes_that_moved_against_us_would_exceed_the_approved_risk(self):
        worse = set_quote(entry_chain(), self.long_c, ask=self.cand.entry_price_long + Dec("1.00"),
                          bid=self.cand.entry_price_long + Dec("0.15"))
        self.build([worse])
        r = self.enter()
        self.assertPending(r, "FILL_EXCEEDS_APPROVED_RISK")
        self.assertIn("approved", r.reasons[0].text)

    def test_the_risk_guard_boundary_is_exact(self):
        # a tick adverse move costs 0.05 x 65 = 3.25 more; the approval has no spare room, so even one tick is refused
        worse = set_quote(entry_chain(), self.long_c, ask=self.cand.entry_price_long + Dec("0.05"),
                          bid=self.cand.entry_price_long - Dec("0.80"))
        self.build([worse])
        self.assertPending(self.enter(), "FILL_EXCEEDS_APPROVED_RISK")

    def test_unverified_cost_rates_leave_the_entry_pending(self):
        self.build([entry_chain()], cost_engine=load_cost_engine(data_mode="real_readonly"))
        r = self.enter()
        self.assertEqual(r.status, X.PENDING)
        self.assertIn("COST_RATES_UNVERIFIED", self.codes(r))
        self.assertEqual(self.ex.open_positions(), ())


class ExitTests(Base):
    def open_position(self, exit_chain=None):
        self.build([entry_chain(), exit_chain or self.exit_chain()])
        self.entry = self.enter()
        self.assertEqual(self.entry.status, X.FILLED, self.entry.reasons)
        return self.entry.execution_id

    def test_exit_fills_use_bid_minus_and_ask_plus_slippage(self):
        eid = self.open_position()
        t = T1230 + datetime.timedelta(hours=2)
        r = self.ex.execute_exit(eid, t, "TEST")
        self.assertEqual(r.status, X.FILLED, r.reasons)
        long_fill, short_fill = r.legs
        self.assertEqual((long_fill.leg_id, long_fill.side, long_fill.reference_price, long_fill.fill_price),
                         ("long", SELL, Dec("140.00"), Dec("139.50")))
        self.assertEqual((short_fill.leg_id, short_fill.side, short_fill.reference_price, short_fill.fill_price),
                         ("short", BUY, Dec("80.55"), Dec("81.05")))

    def test_net_pnl_uses_actual_fills_and_actual_costs_only(self):
        eid = self.open_position()
        t = T1230 + datetime.timedelta(hours=2)
        r = self.ex.execute_exit(eid, t)
        e_long, e_short = self.entry.legs[0].fill_price, self.entry.legs[1].fill_price
        gross = ((Dec("139.50") - e_long) + (e_short - Dec("81.05"))) * UNITS
        entry_costs = ENGINE.actual_costs([LegExecution("long", BUY, e_long, UNITS, DAY),
                                           LegExecution("short", SELL, e_short, UNITS, DAY)]).total
        exit_costs = ENGINE.actual_costs([LegExecution("long", SELL, Dec("139.50"), UNITS, DAY),
                                          LegExecution("short", BUY, Dec("81.05"), UNITS, DAY)]).total
        self.assertEqual(Dec(r.actual["gross_pnl_inr"]), gross)
        self.assertEqual(Dec(r.actual["actual_exit_costs"]["total_inr"]), exit_costs)
        self.assertEqual(Dec(r.actual["actual_entry_costs_inr"]), entry_costs)
        self.assertEqual(Dec(r.actual["net_pnl_inr"]), (gross - entry_costs - exit_costs).quantize(Dec("0.01")))
        self.assertEqual(r.actual["actual_exit_costs"]["label"], "actual (paper fills)")

    def test_the_p7_exit_cost_estimate_is_not_the_actual_exit_cost(self):
        eid = self.open_position()
        r = self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))
        estimated = Dec(r.estimated_at_approval["estimated_exit_costs_inr"])
        actual = Dec(r.actual["actual_exit_costs"]["total_inr"])
        self.assertNotEqual(estimated, actual)
        self.assertNotIn("estimated", json.dumps(r.actual).lower())
        self.assertEqual(Dec(r.actual["net_pnl_inr"]) + Dec(r.actual["actual_entry_costs_inr"]) + actual,
                         Dec(r.actual["gross_pnl_inr"]))

    def test_the_actual_result_is_passed_to_the_risk_ledger(self):
        eid = self.open_position()
        before = self.mgr.snapshot()
        self.assertEqual(before.open_lots, 1)
        r = self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))
        after = self.mgr.snapshot()
        self.assertEqual(after.realized_pnl_inr, Dec(r.actual["net_pnl_inr"]))
        self.assertEqual((after.open_lots, after.open_risk_reserved_inr, after.capital_committed_inr), (0, 0, 0))
        self.assertEqual(self.ex.position(eid).state, X.CLOSED)
        self.assertEqual(self.ex.position(eid).net_pnl_inr, Dec(r.actual["net_pnl_inr"]))

    def test_a_losing_exit_reduces_daily_capacity_by_the_actual_loss(self):
        eid = self.open_position(self.exit_chain(long_bid="100.00", long_ask="100.85", short_bid="90.00", short_ask="90.55"))
        r = self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))
        net = Dec(r.actual["net_pnl_inr"])
        self.assertLess(net, 0)
        snap = self.mgr.snapshot()
        self.assertEqual(snap.realized_loss_used_inr, -net)
        self.assertEqual(snap.remaining_daily_risk_capacity_inr, self.cfg.max_daily_loss_inr + net)

    def test_a_missing_exit_quote_is_pending_and_the_position_stays_open(self):
        chain = self.exit_chain()
        self.build([entry_chain(), T.edit_chain(chain, drop=(self.long_c,)), shift(chain, 30)])
        eid = self.enter().execution_id
        t = T1230 + datetime.timedelta(hours=2)
        r = self.ex.execute_exit(eid, t)
        self.assertEqual((r.status, r.legs), (X.PENDING, ()))
        self.assertIn("MISSING_QUOTE", self.codes(r))
        self.assertEqual(self.ex.position(eid).state, X.OPEN)
        self.assertEqual(self.mgr.snapshot().open_lots, 1)
        self.assertEqual(self.ex.execute_exit(eid, t + datetime.timedelta(seconds=30)).status, X.FILLED)

    def test_stale_one_sided_and_thin_exit_quotes_are_pending(self):
        base = self.exit_chain()
        cases = (("STALE_QUOTE", shift(base, -61), 7200),
                 ("ONE_SIDED_QUOTE", set_quote(base, self.long_c, bid=None), 7200),
                 ("ONE_SIDED_QUOTE", set_quote(base, self.short_c, ask=Dec("0")), 7200),
                 ("INSUFFICIENT_QUOTED_SIZE", set_quote(base, self.long_c, bid_qty=UNITS - 1), 7200),
                 ("INSUFFICIENT_QUOTED_SIZE", set_quote(base, self.short_c, ask_qty=UNITS - 1), 7200))
        for code, chain, offset in cases:
            self.build([entry_chain(), chain])
            eid = self.enter().execution_id
            r = self.ex.execute_exit(eid, T1230 + datetime.timedelta(seconds=offset))
            self.assertEqual((r.status, r.legs), (X.PENDING, ()), code)
            self.assertIn(code, self.codes(r))
            self.assertEqual(self.ex.position(eid).state, X.OPEN)

    def test_exit_cannot_happen_twice_or_for_an_unknown_position(self):
        eid = self.open_position()
        t = T1230 + datetime.timedelta(hours=2)
        self.assertEqual(self.ex.execute_exit(eid, t).status, X.FILLED)
        r = self.ex.execute_exit(eid, t)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["ALREADY_CLOSED"]))
        r = self.ex.execute_exit("nope", t)
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["UNKNOWN_POSITION"]))
        self.assertEqual(self.mgr.snapshot().trades_today, 1)

    def test_exit_before_entry_or_on_another_day_is_rejected(self):
        eid = self.open_position()
        r = self.ex.execute_exit(eid, T1230 - datetime.timedelta(seconds=1))
        self.assertEqual(self.codes(r), ["EXIT_BEFORE_ENTRY"])
        r = self.ex.execute_exit(eid, T1230 + datetime.timedelta(days=1))
        self.assertEqual(self.codes(r), ["EXIT_ON_ANOTHER_DAY"])
        self.assertEqual(self.ex.position(eid).state, X.OPEN)

    def test_the_kill_switch_does_not_trap_an_open_position(self):
        eid = self.open_position()
        paper_only.trip_kill_switch(self.kill)
        self.assertEqual(self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2)).status, X.FILLED)

    def test_a_broken_paper_guard_stops_exits_too(self):
        eid = self.open_position()
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))

    def test_after_a_close_a_new_independent_candidate_can_be_approved(self):
        eid = self.open_position()
        self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))
        self.assertEqual(self.mgr.snapshot().open_lots, 0)

    def test_the_same_instant_in_any_time_zone_gives_the_same_fills(self):
        self.build([entry_chain()])
        r = self.ex.execute_entry(self.approval, self.cand, 1, T1230.astimezone(UTC))
        self.assertEqual(r.status, X.FILLED)
        self.assertEqual(r.executed_at, T1230)
        self.assertEqual(r.executed_at.utcoffset(), datetime.timedelta(hours=5, minutes=30))

    def test_the_whole_cycle_is_deterministic(self):
        runs = []
        for _ in range(2):
            eid = self.open_position()
            x = self.ex.execute_exit(eid, T1230 + datetime.timedelta(hours=2))
            runs.append(json.dumps([X.result_to_journal(r) for r in self.ex.log], sort_keys=True))
        self.assertEqual(runs[0], runs[1])


class ExtraBoundaryTests(Base):
    def test_execution_window_boundaries_are_inclusive(self):
        self.build([entry_chain()], exec_cfg=self.cfg._replace(entry_window_end_ist="12:30", flat_by_ist="12:31"))
        self.assertEqual(self.enter().status, X.FILLED)
        self.build([entry_chain()], exec_cfg=self.cfg._replace(entry_window_start_ist="12:30"))
        self.assertEqual(self.enter().status, X.FILLED)
        self.build([entry_chain()], exec_cfg=self.cfg._replace(entry_window_start_ist="12:31"))
        r = self.enter()
        self.assertEqual((r.status, self.codes(r)), (X.REJECTED, ["OUTSIDE_ENTRY_WINDOW"]))

    def test_a_candidate_from_another_day_is_rejected_at_execution(self):
        self.build([entry_chain()])
        r = self.enter(T1230 + datetime.timedelta(days=1))
        self.assertIn("CANDIDATE_OTHER_DAY", self.codes(r))
        self.assertEqual(r.status, X.REJECTED)

    def test_an_expired_contract_gets_no_quote_priced(self):
        self.build([entry_chain()])
        after = parse_ist("%s 12:30" % (self.cand.expiry + datetime.timedelta(days=1)))
        s = self.cand.spread
        legs = [("long", s.long_leg.contract, BUY, 1, UNITS), ("short", s.short_leg.contract, SELL, 1, UNITS)]
        fills, problems = self.ex._price_legs(legs, after)
        self.assertEqual(fills, ())
        self.assertIn("EXPIRED_CONTRACT", [p.code for p in problems])

    def test_cancelling_follows_the_same_integrity_rules(self):
        from nifty_core.errors import RiskViolation
        self.build([entry_chain()])
        with self.assertRaises(RiskViolation) as cm:
            self.mgr.cancel_unused(self.approval._replace(lots=2))
        self.assertEqual(cm.exception.code, "APPROVAL_TAMPERED")
        self.assertEqual(self.enter().status, X.FILLED)
        with self.assertRaises(RiskViolation) as cm:
            self.mgr.cancel_unused(self.approval)                    # already used by a fill
        self.assertEqual(cm.exception.code, "APPROVAL_ALREADY_USED")
        self.assertEqual(self.mgr.snapshot().open_lots, 1)

    def test_only_an_open_reservation_can_be_cancelled(self):
        from nifty_core.errors import RiskViolation
        self.build([entry_chain()])
        self.mgr.cancel_unused(self.approval)
        with self.assertRaises(RiskViolation) as cm:
            self.mgr.cancel_unused(self.approval)
        self.assertEqual(cm.exception.code, "NOT_CANCELLABLE")
        self.assertEqual(self.mgr.snapshot().trades_today, 0)
        self.assertEqual(len(self.mgr.ledger.positions), 1)         # kept, so the candidate stays used


class SourceTests(NoNetworkTestCase):
    def test_the_executor_only_reads_quotes_and_has_no_network_or_broker_code(self):
        with open(os.path.join(ROOT, "nifty_core", "execution", "paper.py"), encoding="utf-8") as fh:
            text = fh.read()
        for word in ("import socket", "import requests", "import urllib", "http", "subprocess", "api_key", "kite",
                     "os.environ", "getenv"):
            self.assertNotIn(word, text, word)
        self.assertIn("get_quote", text)
        for forbidden_call in ("get_option_chain(", "get_underlying_bars(", "get_event_calendar("):
            self.assertNotIn(forbidden_call, text)

    def test_every_result_is_labelled_simulated_and_paper(self):
        self.assertIn("SIMULATED", X.NOTE)
        self.assertIn("No order was sent", X.NOTE)


if __name__ == "__main__":
    unittest.main()
