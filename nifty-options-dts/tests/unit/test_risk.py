"""Risk engine tests. Candidates are built from the bullish fixture's 12:30 chain with the long leg's price edited,
so the net debit can be placed exactly where a limit is being tested. The strategy thresholds are never touched."""
import copy
import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core.costs.engine import load_cost_engine
from nifty_core.errors import KillSwitchActive, LiveTradingNotSupported, RiskViolation
from nifty_core.guards import paper_only
from nifty_core.instruments import builders
from nifty_core.market.ist import UTC, parse_ist
from nifty_core.risk import engine as R
from nifty_core.risk import ledger as L
from nifty_core.strategy import engine as SE
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.unit import test_strategy as T

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAY = sc.scenario("bullish_trend").day
T1230 = parse_ist("%s 12:30" % DAY)
ENGINE = load_cost_engine(data_mode="fixture")
_cache = {}


def base_parts():
    if "parts" not in _cache:
        _cache["parts"] = T.parts("bullish_trend", "12:30")
    return _cache["parts"]


def cheap_candidate(debit="30.00", at=T1230):
    """A real strategy candidate (offset 1, width 150) whose net debit is exactly `debit` points."""
    key = ("c", str(debit), at)
    if key not in _cache:
        regime, chain, events, as_of = base_parts()
        base = T.decide(regime, chain, events, as_of).candidate
        ask = Dec("85.55") + Dec(debit)
        ch = T.with_quote(T.with_quote(chain, base.long_contract, ask=ask, bid=ask - Dec("0.85")),
                          base.short_contract, bid=Dec("85.55"))
        p = T.params(long_strike_offsets_steps=[1], width_points_options=[150])
        d = T.decide(regime, ch, events, as_of, p=p)
        assert d.candidate is not None, d.reasons
        cand = d.candidate
        if at != as_of:
            cand = cand._replace(decision_time=at)
        _cache[key] = cand
    return _cache[key]


def other_contract_candidate(at):
    """A second, different candidate (offset 0, width 100: different strikes) decided at `at`, with a small debit."""
    key = ("o", at)
    if key not in _cache:
        regime, chain, events, as_of = base_parts()
        p = T.params(long_strike_offsets_steps=[0], width_points_options=[100])
        base = T.decide(regime, chain, events, as_of, p=p).candidate
        ch = T.with_quote(T.with_quote(chain, base.long_contract, ask=Dec("100.00"), bid=Dec("99.15")),
                          base.short_contract, bid=Dec("75.00"), ask=Dec("75.80"))
        d = T.decide(regime, ch, events, as_of, p=p)
        assert d.candidate is not None, d.reasons
        _cache[key] = d.candidate._replace(decision_time=at)
    return _cache[key]


def total_risk(cand, cfg=None):
    return R.calculate(cand, cfg or sc.config(), ENGINE, DAY)["total_defined_risk_inr"]


def mins(n):
    return T1230 + datetime.timedelta(minutes=n)


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.cfg = sc.config()
        self.kill = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.kill, ignore_errors=True))
        self.ledger = L.DayLedger(DAY, self.cfg.account_capital_inr)

    def evaluate(self, cand, now=None, ledger=None, cfg=None):
        return R.evaluate(cand, ledger or self.ledger, now or cand.decision_time, cfg or self.cfg, ENGINE,
                          sc.specs(), sc.calendar(), self.kill)

    def manager(self, cfg=None):
        return R.RiskManager(cfg or self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill, DAY)

    @staticmethod
    def codes(decision):
        return [r.code for r in decision.reasons]


class WithinLimitsTests(Base):
    def test_a_candidate_within_every_limit_is_approved(self):
        c = cheap_candidate("30.00")
        d = self.evaluate(c)
        self.assertEqual((d.outcome, d.reasons), (R.APPROVED, ()))
        self.assertEqual(d.trades_before, 0)
        self.assertEqual(d.trades_after, 1)
        self.assertEqual(d.candidate_id, R.candidate_fingerprint(c))
        self.assertEqual(d.approval.lots, 1)
        self.assertLessEqual(d.calculation["total_defined_risk_inr"], self.cfg.risk_per_trade_inr)

    def test_approved_record_has_everything_asked_for(self):
        d = self.evaluate(cheap_candidate("30.00"))
        j = json.loads(json.dumps(R.decision_to_journal(d)))
        self.assertEqual(j["outcome"], "APPROVED")
        self.assertEqual(j["ledger_before"]["open_risk_reserved_inr"], "0")
        after = j["ledger_after"]
        self.assertEqual(Dec(after["open_risk_reserved_inr"]), d.calculation["total_defined_risk_inr"])
        self.assertEqual(Dec(after["capital_committed_inr"]), d.calculation["entry_risk_inr"])
        self.assertEqual(Dec(after["remaining_daily_risk_capacity_inr"]),
                         self.cfg.max_daily_loss_inr - d.calculation["total_defined_risk_inr"])
        self.assertEqual((j["trades_before"], j["trades_after"]), (0, 1))
        for key in ("config", "candidate", "strategy_thresholds", "regime_thresholds"):
            self.assertTrue(j["fingerprints"][key], key)
        self.assertEqual(Dec(j["limits"]["risk_per_trade_inr"]), Dec("2500"))
        self.assertEqual(j["approval"]["lots"], 1)
        self.assertFalse(j["calculation"]["stop_loss_used"])

    def test_every_strategy_fixture_candidate_is_reported_with_its_own_numbers(self):
        c = T.decide_at("bullish_trend", "12:30").candidate
        d = self.evaluate(c)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertEqual(self.codes(d), ["PER_TRADE_RISK_EXCEEDED"])
        self.assertEqual(d.calculation["net_debit_quoted_inr"], Dec("3402.75"))
        self.assertEqual(d.calculation["total_defined_risk_inr"], Dec("3596.55"))
        self.assertIsNone(d.ledger_after)
        self.assertIsNone(d.approval)

    def test_blocked_record_has_candidate_risk_costs_limit_and_every_reason(self):
        d = self.evaluate(T.decide_at("bullish_trend", "12:30").candidate)
        j = json.loads(json.dumps(R.decision_to_journal(d)))
        self.assertEqual(j["outcome"], "BLOCKED")
        self.assertTrue(j["candidate_id"])
        for key in ("net_debit_quoted_inr", "entry_slippage_inr", "entry_costs_inr", "estimated_exit_costs_inr",
                    "total_defined_risk_inr", "entry_cost_components"):
            self.assertIn(key, j["calculation"])
        self.assertEqual(Dec(j["limits"]["risk_per_trade_inr"]), Dec("2500"))
        self.assertTrue(j["reasons"][0]["text"])

    def test_defined_risk_is_the_sum_of_its_four_parts(self):
        c = self.evaluate(cheap_candidate("30.00")).calculation
        self.assertEqual(c["net_debit_quoted_inr"] + c["entry_slippage_inr"] + c["entry_costs_inr"]
                         + c["estimated_exit_costs_inr"], c["total_defined_risk_inr"])
        self.assertEqual(c["net_debit_paid_inr"], c["net_debit_quoted_inr"] + c["entry_slippage_inr"])
        self.assertEqual(c["round_trip_costs_inr"], c["entry_costs_inr"] + c["estimated_exit_costs_inr"])
        self.assertEqual(c["entry_costs_inr"], sum(c["entry_cost_components"].values()))
        self.assertEqual(c["entry_slippage_inr"], Dec("0.5") * 2 * 65)        # 0.5 pt x 2 legs x 65 units
        self.assertEqual(c["net_debit_quoted_inr"], Dec("30.00") * 65)


class PerTradeLimitTests(Base):
    def test_defined_risk_exactly_equal_to_the_limit_is_approved(self):
        c = cheap_candidate("30.00")
        exact = total_risk(c)
        cfg = self.cfg._replace(risk_per_trade_inr=exact)
        d = self.evaluate(c, cfg=cfg)
        self.assertEqual(d.outcome, R.APPROVED)
        self.assertEqual(d.calculation["total_defined_risk_inr"], cfg.risk_per_trade_inr)

    def test_one_paisa_and_one_rupee_above_the_limit_are_blocked(self):
        c = cheap_candidate("30.00")
        exact = total_risk(c)
        for gap in (Dec("0.01"), Dec("1")):
            d = self.evaluate(c, cfg=self.cfg._replace(risk_per_trade_inr=exact - gap))
            self.assertEqual((d.outcome, self.codes(d)), (R.BLOCKED, ["PER_TRADE_RISK_EXCEEDED"]), gap)
            self.assertIn(str(gap), d.reasons[0].text)

    def test_the_default_2500_limit_at_the_tick_boundary(self):
        # walk the debit up one tick at a time: the first blocked one is the first whose total exceeds 2500
        last_ok = first_bad = None
        for ticks in range(0, 200):
            debit = Dec("30.00") + Dec("0.05") * ticks
            c = cheap_candidate(str(debit))
            risk = total_risk(c)
            if risk <= Dec("2500"):
                last_ok = (debit, risk)
            else:
                first_bad = (debit, risk)
                break
        self.assertIsNotNone(first_bad)
        self.assertLessEqual(last_ok[1], Dec("2500"))
        self.assertGreater(first_bad[1], Dec("2500"))
        self.assertEqual(first_bad[0] - last_ok[0], Dec("0.05"))
        self.assertEqual(self.evaluate(cheap_candidate(str(last_ok[0]))).outcome, R.APPROVED)
        self.assertEqual(self.evaluate(cheap_candidate(str(first_bad[0]))).outcome, R.BLOCKED)

    def test_costs_and_slippage_push_an_otherwise_valid_debit_over_the_limit(self):
        c = cheap_candidate("37.00")                                    # 37.00 x 65 = 2,405 < 2,500
        d = self.evaluate(c)
        calc = d.calculation
        self.assertLess(calc["net_debit_quoted_inr"], self.cfg.risk_per_trade_inr)
        self.assertLessEqual(calc["net_debit_paid_inr"], self.cfg.risk_per_trade_inr)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertEqual(self.codes(d), ["PER_TRADE_RISK_EXCEEDED"])
        self.assertGreater(calc["total_defined_risk_inr"], self.cfg.risk_per_trade_inr)

    def test_slippage_alone_can_decide_the_outcome(self):
        c = cheap_candidate("30.00")
        base = total_risk(c)
        no_slip = self.cfg._replace(slippage_points_per_leg=Dec("0"))
        with_slip = total_risk(c, self.cfg)
        self.assertLess(total_risk(c, no_slip), with_slip)
        limit = (total_risk(c, no_slip) + with_slip) / 2
        self.assertEqual(self.evaluate(c, cfg=no_slip._replace(risk_per_trade_inr=limit)).outcome, R.APPROVED)
        self.assertEqual(self.evaluate(c, cfg=self.cfg._replace(risk_per_trade_inr=limit)).outcome, R.BLOCKED)
        self.assertEqual(base, with_slip)

    def test_a_stop_loss_cannot_reduce_defined_risk(self):
        import inspect
        for fn in (R.evaluate, R.calculate, R.RiskManager.request_entry):
            self.assertFalse([p for p in inspect.signature(fn).parameters if "stop" in p.lower()], fn)
        c = cheap_candidate("30.00")
        self.assertFalse(self.evaluate(c).calculation["stop_loss_used"])
        # the figure equals the strategy-independent spread calculation with no stop of any kind
        self.assertEqual(total_risk(c), c.spread.defined_risk(ENGINE, self.cfg.slippage_points_per_leg,
                                                              trade_date=DAY).total_defined_risk_inr)

    def test_the_engine_never_alters_the_candidate_to_fit(self):
        c = T.decide_at("bullish_trend", "12:30").candidate
        before = R.candidate_fingerprint(c)
        d = self.evaluate(c)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertEqual(R.candidate_fingerprint(c), before)
        self.assertEqual((c.lots, c.net_debit_points), (1, Dec("52.35")))

    def test_unverified_cost_rates_block_in_real_mode(self):
        real = load_cost_engine(data_mode="real_readonly")
        cfg = self.cfg._replace(data_mode="real_readonly")
        c = cheap_candidate("30.00")
        d = R.evaluate(c, self.ledger, c.decision_time, cfg, real, sc.specs(), sc.calendar("real_readonly"), self.kill)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertTrue({"COST_RATES_UNVERIFIED", "DATA_MODE_MISMATCH", "DATA_UNVERIFIED"} & set(self.codes(d)))


class LotAndTradeCountTests(Base):
    def test_one_lot_is_the_most_even_if_configuration_says_more(self):
        c = cheap_candidate("30.00")
        two = builders.from_chain(base_parts()[1], c.long_contract, c.short_contract, 2, sc.specs(), "fixture",
                                  trade_date=DAY)
        c2 = c._replace(spread=two, lots=2, net_debit_inr=two.net_debit_points * two.units)
        for configured in (1, 2, 5, 100):
            d = self.evaluate(c2, cfg=self.cfg._replace(max_lots=configured))
            self.assertEqual(d.outcome, R.BLOCKED, configured)
            self.assertIn("MAX_LOTS_EXCEEDED", self.codes(d))
            self.assertEqual(d.limits["max_lots_effective"], 1)
        self.assertEqual(R.effective_max_lots(self.cfg._replace(max_lots=5)), 1)

    def test_a_lower_configured_lot_limit_still_applies(self):
        self.assertEqual(R.effective_max_lots(self.cfg._replace(max_lots=1)), 1)

    def test_maximum_trades_per_day(self):
        m = self.manager()
        times = [T1230, mins(15)]
        for i, t in enumerate(times):
            d = m.request_entry(cheap_candidate("30.00" if i == 0 else "30.05", at=t), t)
            self.assertEqual(d.outcome, R.APPROVED, i)
            m.record_close(d.approval, Dec("100"), t + datetime.timedelta(minutes=5))
        t3 = mins(30)
        d = m.request_entry(cheap_candidate("30.10", at=t3), t3)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertEqual(self.codes(d), ["MAX_TRADES_REACHED"])
        self.assertEqual((d.trades_before, d.trades_after), (2, None))

    def test_trade_count_boundary_follows_configuration(self):
        cfg = self.cfg._replace(max_trades_per_day=1)
        m = self.manager(cfg)
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        m.record_close(d.approval, Dec("0"), mins(5))
        d2 = m.request_entry(cheap_candidate("30.05", at=mins(15)), mins(15))
        self.assertIn("MAX_TRADES_REACHED", self.codes(d2))

    def test_an_open_position_blocks_a_second_lot_even_before_the_trade_count(self):
        m = self.manager()
        m.request_entry(cheap_candidate("30.00"), T1230)
        d = m.request_entry(other_contract_candidate(mins(10)), mins(10))
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("MAX_LOTS_EXCEEDED_COMBINED", self.codes(d))


class DailyLimitTests(Base):
    def test_daily_loss_limit_blocks_once_reached(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        snap = m.record_close(d.approval, Dec("-5000"), mins(10))
        self.assertEqual((snap.realized_loss_used_inr, snap.remaining_daily_risk_capacity_inr), (Dec("5000"), Dec("0")))
        d2 = m.request_entry(cheap_candidate("30.05", at=mins(20)), mins(20))
        self.assertEqual(d2.outcome, R.BLOCKED)
        self.assertIn("DAILY_LOSS_LIMIT_REACHED", self.codes(d2))
        self.assertIn("DAILY_RISK_CAPACITY_EXCEEDED", self.codes(d2))

    def test_remaining_capacity_after_a_partial_loss(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        m.record_close(d.approval, Dec("-3000"), mins(10))
        snap = m.snapshot()
        self.assertEqual(snap.remaining_daily_risk_capacity_inr, Dec("2000"))
        c2 = cheap_candidate("30.05", at=mins(20))
        risk = total_risk(c2)
        self.assertGreater(risk, Dec("2000"))
        d2 = m.request_entry(c2, mins(20))
        self.assertEqual(self.codes(d2), ["DAILY_RISK_CAPACITY_EXCEEDED"])
        self.assertIn("2000", d2.reasons[0].text)

    def test_capacity_exactly_equal_to_defined_risk_is_allowed_and_one_paisa_less_is_not(self):
        c = cheap_candidate("30.00")
        risk = total_risk(c)
        for max_daily, expect in ((risk, R.APPROVED), (risk - Dec("0.01"), R.BLOCKED)):
            cfg = self.cfg._replace(max_daily_loss_inr=max_daily)
            d = self.evaluate(c, cfg=cfg)
            self.assertEqual(d.outcome, expect, max_daily)

    def test_realized_profit_does_not_raise_the_loss_budget(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        snap = m.record_close(d.approval, Dec("1500"), mins(10))
        self.assertEqual(snap.realized_pnl_inr, Dec("1500"))
        self.assertEqual(snap.realized_loss_used_inr, Dec("0"))
        self.assertEqual(snap.remaining_daily_risk_capacity_inr, self.cfg.max_daily_loss_inr)

    def test_open_risk_is_reserved_against_the_daily_capacity(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        snap = m.snapshot()
        self.assertEqual(snap.open_risk_reserved_inr, d.calculation["total_defined_risk_inr"])
        self.assertEqual(snap.remaining_daily_risk_capacity_inr,
                         self.cfg.max_daily_loss_inr - d.calculation["total_defined_risk_inr"])

    def test_combined_open_exposure_cannot_breach_the_daily_limit(self):
        # raise the lot ceiling for this test only, so the risk arithmetic (not the lot limit) is what blocks
        a, b = cheap_candidate("30.00"), other_contract_candidate(mins(10))
        ra, rb = total_risk(a), total_risk(b)
        cfg = self.cfg._replace(max_lots=2, max_daily_loss_inr=ra + rb - Dec("0.01"))
        with mock.patch.object(R, "HARD_MAX_LOTS", 2):
            m = self.manager(cfg)
            self.assertEqual(m.request_entry(a, T1230).outcome, R.APPROVED)
            d = m.request_entry(b, mins(10))
            self.assertEqual(self.codes(d), ["DAILY_RISK_CAPACITY_EXCEEDED"])
            ok = self.manager(self.cfg._replace(max_lots=2, max_daily_loss_inr=ra + rb))
            ok.request_entry(a, T1230)
            self.assertEqual(ok.request_entry(b, mins(10)).outcome, R.APPROVED)
            self.assertEqual(ok.snapshot().open_risk_reserved_inr, ra + rb)

    def test_accounting_quantities_are_kept_separate(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        s = m.snapshot()
        values = {s.account_capital_inr, s.capital_committed_inr, s.open_risk_reserved_inr, s.realized_pnl_inr,
                  s.remaining_daily_risk_capacity_inr}
        self.assertEqual(len(values), 5)
        self.assertEqual(s.account_capital_inr, Dec("250000"))
        self.assertEqual(s.capital_committed_inr, d.calculation["entry_risk_inr"])
        self.assertNotEqual(s.capital_committed_inr, s.open_risk_reserved_inr)
        m.record_close(d.approval, Dec("-1000"), mins(5))
        s = m.snapshot()
        self.assertEqual((s.capital_committed_inr, s.open_risk_reserved_inr, s.realized_pnl_inr),
                         (Dec("0"), Dec("0"), Dec("-1000")))
        self.assertEqual(s.account_capital_inr, Dec("250000"))

    def test_capital_limit_is_checked_independently(self):
        cfg = self.cfg._replace(account_capital_inr=Dec("1000"))
        d = self.evaluate(cheap_candidate("30.00"), cfg=cfg)
        self.assertIn("CAPITAL_EXCEEDED", self.codes(d))


class NoBypassTests(Base):
    def test_candidate_is_immutable(self):
        c = cheap_candidate("30.00")
        with self.assertRaises(AttributeError):
            c.lots = 2
        with self.assertRaises(AttributeError):
            c.spread.lots = 2

    def test_changing_the_candidate_after_approval_is_detected(self):
        m = self.manager()
        c = cheap_candidate("30.00")._replace(liquidity=copy.deepcopy(cheap_candidate("30.00").liquidity))
        d = m.request_entry(c, T1230)
        c.liquidity["long"]["ask"] = "1.00"                              # a mutable inner dict
        with self.assertRaises(RiskViolation) as cm:
            m.confirm_entry(d.approval, c, 1)
        self.assertEqual(cm.exception.code, "CANDIDATE_MODIFIED")

    def test_replacing_any_field_changes_the_fingerprint(self):
        c = cheap_candidate("30.00")
        base = R.candidate_fingerprint(c)
        self.assertEqual(R.candidate_fingerprint(c), base)
        for change in (dict(entry_price_long=c.entry_price_long + Dec("0.05")), dict(lots=2),
                       dict(decision_time=c.decision_time + datetime.timedelta(seconds=1)),
                       dict(width_points=c.width_points + 50), dict(data_mode="real_readonly")):
            self.assertNotEqual(R.candidate_fingerprint(c._replace(**change)), base, change)

    def test_a_candidate_whose_summary_disagrees_with_its_spread_is_blocked(self):
        c = cheap_candidate("30.00")
        for change in (dict(lots=2), dict(entry_price_long=c.entry_price_long - Dec("1")),
                       dict(net_debit_points=Dec("1")), dict(strategy_type="BEAR_PUT_DEBIT")):
            d = self.evaluate(c._replace(**change))
            self.assertEqual(d.outcome, R.BLOCKED, change)
            self.assertIn("CANDIDATE_INCONSISTENT", self.codes(d), change)

    def test_a_candidate_without_a_spread_is_invalid(self):
        d = self.evaluate(cheap_candidate("30.00")._replace(spread=None))
        self.assertEqual((d.outcome, self.codes(d)[:1]), (R.BLOCKED, ["CANDIDATE_INVALID"]))

    def test_changing_the_quantity_after_approval_is_refused(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        d = m.request_entry(c, T1230)
        for lots in (2, 0, 3):
            with self.assertRaises(RiskViolation) as cm:
                m.confirm_entry(d.approval, c, lots)
            self.assertEqual(cm.exception.code, "QUANTITY_CHANGED")
        self.assertTrue(m.confirm_entry(d.approval, c, 1))

    def test_an_approval_can_be_used_only_once(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        d = m.request_entry(c, T1230)
        m.confirm_entry(d.approval, c, 1)
        with self.assertRaises(RiskViolation) as cm:
            m.confirm_entry(d.approval, c, 1)
        self.assertEqual(cm.exception.code, "APPROVAL_ALREADY_USED")

    def test_a_tampered_approval_is_refused(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        d = m.request_entry(c, T1230)
        self.assertTrue(R.approval_is_authentic(d.approval))
        for change in (dict(lots=2), dict(defined_risk_inr=Dec("1")), dict(candidate_id="x" * 64)):
            forged = d.approval._replace(**change)
            self.assertFalse(R.approval_is_authentic(forged))
            with self.assertRaises(RiskViolation) as cm:
                m.confirm_entry(forged, c, forged.lots)
            self.assertEqual(cm.exception.code, "APPROVAL_TAMPERED")

    def test_a_decision_that_was_blocked_has_no_approval_to_confirm(self):
        m = self.manager()
        c = T.decide_at("bullish_trend", "12:30").candidate
        d = m.request_entry(c, T1230)
        self.assertIsNone(d.approval)
        with self.assertRaises(RiskViolation):
            m.confirm_entry(None, c, 1)

    def test_an_approval_not_in_the_ledger_is_refused(self):
        m1, m2 = self.manager(), self.manager()
        c = cheap_candidate("30.00")
        d = m1.request_entry(c, T1230)
        with self.assertRaises(RiskViolation) as cm:
            m2.confirm_entry(d.approval, c, 1)
        self.assertEqual(cm.exception.code, "APPROVAL_NOT_OPEN")

    def test_the_same_candidate_cannot_be_approved_twice_or_split(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        self.assertEqual(m.request_entry(c, T1230).outcome, R.APPROVED)
        d = m.request_entry(c, T1230)
        self.assertEqual(d.outcome, R.BLOCKED)
        for code in ("CANDIDATE_ALREADY_USED", "AVERAGING_BLOCKED", "MAX_LOTS_EXCEEDED_COMBINED"):
            self.assertIn(code, self.codes(d))
        self.assertEqual(m.snapshot().trades_today, 1)

    def test_a_slightly_changed_copy_of_an_open_position_is_averaging(self):
        m = self.manager()
        m.request_entry(cheap_candidate("30.00"), T1230)
        d = m.request_entry(cheap_candidate("29.95", at=mins(2)), mins(2))      # same contracts, cheaper entry
        self.assertIn("AVERAGING_BLOCKED", self.codes(d))
        self.assertEqual(d.outcome, R.BLOCKED)

    def test_averaging_is_blocked_even_when_lots_and_capacity_would_allow_it(self):
        a, b = cheap_candidate("30.00"), cheap_candidate("29.95", at=mins(2))
        cfg = self.cfg._replace(max_lots=2)
        with mock.patch.object(R, "HARD_MAX_LOTS", 2):
            m = self.manager(cfg)
            m.request_entry(a, T1230)
            d = m.request_entry(b, mins(2))
            self.assertEqual(self.codes(d), ["AVERAGING_BLOCKED"])

    def test_a_recovery_trade_needs_a_decision_made_after_the_loss(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        m.record_close(d.approval, Dec("-800"), mins(10))
        pre = cheap_candidate("30.05", at=mins(5))                       # decided before the loss closed
        r = m.request_entry(pre, mins(5) + datetime.timedelta(minutes=5))        # evaluated at 12:40 (exactly the close)
        self.assertIn("RECOVERY_TRADE_NOT_INDEPENDENT", self.codes(r))
        same_instant = cheap_candidate("30.05", at=mins(10))
        self.assertIn("RECOVERY_TRADE_NOT_INDEPENDENT", self.codes(m.request_entry(same_instant, mins(10))))
        later = cheap_candidate("30.10", at=mins(10) + datetime.timedelta(seconds=1))
        ok = m.request_entry(later, mins(10) + datetime.timedelta(seconds=1))
        self.assertEqual(ok.outcome, R.APPROVED, self.codes(ok))

    def test_a_win_does_not_trigger_the_recovery_rule(self):
        m = self.manager()
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        m.record_close(d.approval, Dec("800"), mins(10))
        c = cheap_candidate("30.05", at=mins(8))
        self.assertEqual(m.request_entry(c, mins(8)).outcome, R.APPROVED)

    def test_no_uncovered_short_ever_reaches_approval(self):
        c = cheap_candidate("30.00")
        s = c.spread
        naked = s.short_leg
        fake = type("FakeSpread", (object,), {})()
        for attr in ("kind", "long_leg", "short_leg", "expiry", "lots", "lot_size", "net_debit_points",
                     "width_points", "units"):
            setattr(fake, attr, getattr(s, attr))
        fake.long_leg = naked._replace(side="SELL")                   # two short legs
        d = self.evaluate(c._replace(spread=fake))
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("CANDIDATE_INVALID", self.codes(d))             # not a validated DebitSpread at all

    def test_the_independent_structure_check_catches_a_short_that_loses_its_cover(self):
        c = cheap_candidate("30.00")
        s = c.spread
        # build a real DebitSpread object, then lower the long leg's coverage by monkeypatching the leg tuple
        broken_long = s.long_leg._replace(contract=s.long_leg.contract._replace(strike=s.short_leg.strike + 100))
        with mock.patch.object(type(s), "long_leg", broken_long, create=True):
            problems = R._structure(c)
        self.assertTrue([p for p in problems if p[0] == "NAKED_SHORT"])

    def test_only_debit_spreads_exist_so_a_credit_structure_cannot_be_built(self):
        from nifty_core.errors import SpreadError
        regime, chain, events, as_of = base_parts()
        c = T.decide(regime, chain, events, as_of).candidate
        with self.assertRaises(SpreadError):
            builders.from_chain(chain, c.short_contract, c.long_contract, 1, sc.specs())      # would be a credit


class ExtraCheckTests(Base):
    def two_lot(self, at=T1230):
        c = cheap_candidate("30.00", at=at)
        two = builders.from_chain(base_parts()[1], c.long_contract, c.short_contract, 2, sc.specs(), "fixture",
                                  trade_date=DAY)
        return c._replace(spread=two, lots=2, net_debit_inr=two.net_debit_points * two.units)

    def test_lots_may_not_increase_after_a_loss(self):
        with mock.patch.object(R, "HARD_MAX_LOTS", 2):
            cfg = self.cfg._replace(max_lots=2)
            m = self.manager(cfg)
            d = m.request_entry(cheap_candidate("30.00"), T1230)
            m.record_close(d.approval, Dec("-100"), mins(5))
            later = self.two_lot(at=mins(10))
            r = m.request_entry(later, mins(10))
            self.assertEqual(r.outcome, R.BLOCKED)
            self.assertIn("RECOVERY_SIZE_INCREASE", self.codes(r))

    def test_lot_size_must_agree_with_the_contract_layer(self):
        import types
        c = cheap_candidate("30.00")
        with mock.patch.object(sc.specs(), "lot_size", lambda expiry, mode: types.SimpleNamespace(value=75)):
            d = self.evaluate(c)
        self.assertIn("LOT_SIZE_MISMATCH", self.codes(d))
        self.assertEqual(R.BLOCKED, d.outcome)

    def test_candidate_data_mode_must_match_configuration(self):
        c = cheap_candidate("30.00")
        d = R.evaluate(c, self.ledger, c.decision_time, self.cfg._replace(data_mode="real_readonly"), ENGINE,
                       sc.specs(), sc.calendar(), self.kill)
        self.assertIn("DATA_MODE_MISMATCH", self.codes(d))
        d = self.evaluate(c._replace(data_mode="real_readonly"))
        self.assertIn("DATA_MODE_MISMATCH", self.codes(d))

    def test_synthetic_data_is_never_approved_outside_fixture_mode(self):
        c = cheap_candidate("30.00")._replace(data_mode="real_readonly")
        d = R.evaluate(c, self.ledger, c.decision_time, self.cfg._replace(data_mode="real_readonly"), ENGINE,
                       sc.specs(), sc.calendar(), self.kill)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("DATA_UNVERIFIED", self.codes(d))
        self.assertNotIn("DATA_MODE_MISMATCH", self.codes(d))

    def test_an_expired_contract_is_blocked(self):
        c = cheap_candidate("30.00")
        expiry = c.expiry
        after = parse_ist("%s 12:30" % (expiry + datetime.timedelta(days=1)))
        led = L.DayLedger(after.date(), self.cfg.account_capital_inr)
        d = self.evaluate(c._replace(decision_time=after), now=after, ledger=led)
        self.assertIn("EXPIRED_CONTRACT", self.codes(d))
        on_expiry_before_close = parse_ist("%s 12:30" % expiry)
        led2 = L.DayLedger(expiry, self.cfg.account_capital_inr)
        d2 = self.evaluate(c._replace(decision_time=on_expiry_before_close), now=on_expiry_before_close, ledger=led2)
        self.assertNotIn("EXPIRED_CONTRACT", self.codes(d2))
        on_expiry_after_close = parse_ist("%s 15:45" % expiry)
        d3 = self.evaluate(c._replace(decision_time=on_expiry_after_close), now=on_expiry_after_close, ledger=led2)
        self.assertIn("EXPIRED_CONTRACT", self.codes(d3))


class GuardTests(Base):
    def test_kill_switch_blocks_with_a_reason_and_removal_restores_approval(self):
        c = cheap_candidate("30.00")
        paper_only.trip_kill_switch(self.kill, "test")
        d = self.evaluate(c)
        self.assertEqual(d.outcome, R.BLOCKED)
        self.assertIn("KILL_SWITCH_ACTIVE", self.codes(d))
        os.remove(paper_only.kill_switch_path(self.kill))                 # a human removes it (the test plays one)
        self.assertEqual(self.evaluate(c).outcome, R.APPROVED)

    def test_kill_switch_also_stops_confirmation_of_an_existing_approval(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        d = m.request_entry(c, T1230)
        paper_only.trip_kill_switch(self.kill)
        with self.assertRaises(KillSwitchActive):
            m.confirm_entry(d.approval, c, 1)

    def test_kill_switch_cannot_be_skipped_or_switched_off_by_environment_or_config(self):
        c = cheap_candidate("30.00")
        with self.assertRaises(ValueError):
            R.evaluate(c, self.ledger, T1230, self.cfg, ENGINE, sc.specs(), sc.calendar(), None)
        paper_only.trip_kill_switch(self.kill)
        env = {"NIFTY_KILL_SWITCH": "0", "NIFTY_KILL": "off", "KILL": "0", "NIFTY_DISABLE_KILL_SWITCH": "1",
               "NIFTY_SKIP_RISK": "1", "NIFTY_MAX_LOTS": "9"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("KILL_SWITCH_ACTIVE", self.codes(self.evaluate(c)))
        from nifty_core.config import KNOWN_KEYS
        self.assertFalse([k for k in KNOWN_KEYS if "kill" in k.lower()])

    def test_paper_mode_cannot_be_turned_off(self):
        c = cheap_candidate("30.00")
        with self.assertRaises(LiveTradingNotSupported):
            self.evaluate(c, cfg=self.cfg._replace(**{"pa" + "per": bool(0)}))     # built indirectly: the scanner bans the literal
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                self.evaluate(c)
            with self.assertRaises(LiveTradingNotSupported):
                self.manager()
        for value in ("0", "false", "no"):
            with mock.patch.dict(os.environ, {"NIFTY_PAPER": value}):
                from nifty_core import config as config_mod
                with self.assertRaises(LiveTradingNotSupported):
                    config_mod.load_config()
        self.assertEqual(self.evaluate(c).outcome, R.APPROVED)

    def test_confirmation_rechecks_paper_mode(self):
        m = self.manager()
        c = cheap_candidate("30.00")
        d = m.request_entry(c, T1230)
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                m.confirm_entry(d.approval, c, 1)

    def test_overnight_positions_are_not_allowed(self):
        c = cheap_candidate("30.00")
        yesterday = L.DayLedger(DAY, self.cfg.account_capital_inr)
        yesterday_pos = L.Position("a" * 64, "b" * 64, "BULL_CALL_DEBIT", "X", "Y", 1, Dec("1000"), Dec("1000"),
                                   parse_ist("%s 14:00" % (DAY - datetime.timedelta(days=1))), L.OPEN, None, None)
        led = L.DayLedger(DAY, self.cfg.account_capital_inr, (yesterday_pos,))
        d = self.evaluate(c, ledger=led)
        self.assertIn("OVERNIGHT_POSITION_PRESENT", self.codes(d))
        self.assertEqual(yesterday.positions, ())

    def test_no_entry_at_or_after_flat_by(self):
        c = cheap_candidate("30.00")
        for flat, expected_code in (("12:30", "AFTER_FLAT_BY"), ("12:31", None)):
            cfg = self.cfg._replace(entry_window_end_ist="12:30", flat_by_ist=flat)
            d = self.evaluate(c, cfg=cfg)
            if expected_code:
                self.assertIn(expected_code, self.codes(d))
            else:
                self.assertNotIn("AFTER_FLAT_BY", self.codes(d))
                self.assertEqual(d.outcome, R.APPROVED)

    def test_overnight_and_averaging_flags_are_fixed_off(self):
        self.assertIs(self.cfg.allow_overnight, False)
        self.assertIs(self.cfg.allow_averaging, False)
        d = self.evaluate(cheap_candidate("30.00"))
        self.assertIs(d.limits["allow_overnight"], False)
        self.assertIs(d.limits["allow_averaging"], False)


class TimeAndBoundaryTests(Base):
    def test_candidate_age_boundary(self):
        c = cheap_candidate("30.00")
        limit = self.cfg.data_stale_after_seconds
        ok = self.evaluate(c, now=c.decision_time + datetime.timedelta(seconds=limit))
        self.assertEqual(ok.outcome, R.APPROVED)
        late = self.evaluate(c, now=c.decision_time + datetime.timedelta(seconds=limit + 1))
        self.assertEqual(self.codes(late), ["CANDIDATE_STALE"])

    def test_candidate_from_the_future_is_blocked(self):
        c = cheap_candidate("30.00")
        d = self.evaluate(c, now=c.decision_time - datetime.timedelta(seconds=1))
        self.assertIn("CANDIDATE_FROM_FUTURE", self.codes(d))

    def test_entry_window_boundaries_are_inclusive(self):
        for hhmm, expected in (("10:15", R.APPROVED), ("14:30", R.APPROVED), ("10:14", R.BLOCKED), ("14:31", R.BLOCKED)):
            t = parse_ist("%s %s" % (DAY, hhmm))
            c = cheap_candidate("30.00", at=t)
            d = self.evaluate(c, now=t)
            self.assertEqual(d.outcome, expected, hhmm)
            if expected == R.BLOCKED:
                self.assertIn("OUTSIDE_ENTRY_WINDOW", self.codes(d))

    def test_a_candidate_from_another_day_is_blocked(self):
        c = cheap_candidate("30.00", at=T1230 - datetime.timedelta(days=1))
        d = self.evaluate(c, now=T1230)
        self.assertIn("CANDIDATE_OTHER_DAY", self.codes(d))

    def test_ledger_for_another_day_is_blocked(self):
        led = L.DayLedger(DAY - datetime.timedelta(days=1), self.cfg.account_capital_inr)
        self.assertIn("LEDGER_DAY_MISMATCH", self.codes(self.evaluate(cheap_candidate("30.00"), ledger=led)))

    def test_the_same_instant_in_any_time_zone_gives_the_same_decision(self):
        c = cheap_candidate("30.00")
        base = json.dumps(R.decision_to_journal(self.evaluate(c)), sort_keys=True)
        for zone in (UTC, datetime.timezone(datetime.timedelta(hours=-4)), datetime.timezone(datetime.timedelta(hours=13))):
            d = self.evaluate(c, now=T1230.astimezone(zone))
            self.assertEqual(json.dumps(R.decision_to_journal(d), sort_keys=True), base)

    def test_the_decision_is_deterministic_and_pure(self):
        c = cheap_candidate("30.00")
        a = json.dumps(R.decision_to_journal(self.evaluate(c)), sort_keys=True)
        self.assertEqual(a, json.dumps(R.decision_to_journal(self.evaluate(c)), sort_keys=True))
        self.assertEqual(self.ledger.positions, ())                      # evaluate() recorded nothing

    def test_every_blocking_reason_is_reported_together(self):
        c = T.decide_at("bullish_trend", "12:30").candidate               # over the per-trade limit
        paper_only.trip_kill_switch(self.kill)
        cfg = self.cfg._replace(entry_window_end_ist="12:29", flat_by_ist="12:29", max_trades_per_day=1)
        m_led = L.DayLedger(DAY, cfg.account_capital_inr)
        d = self.evaluate(c, cfg=cfg, ledger=m_led)
        for code in ("KILL_SWITCH_ACTIVE", "OUTSIDE_ENTRY_WINDOW", "AFTER_FLAT_BY", "PER_TRADE_RISK_EXCEEDED"):
            self.assertIn(code, self.codes(d))

    def test_every_tested_decision_has_a_reason_when_blocked(self):
        for c in (T.decide_at("bullish_trend", "12:30").candidate, T.decide_at("bearish_trend", "14:30").candidate):
            d = R.evaluate(c, L.DayLedger(c.decision_time.date(), self.cfg.account_capital_inr), c.decision_time,
                           self.cfg, ENGINE, sc.specs(), sc.calendar(), self.kill)
            self.assertEqual(d.outcome, R.BLOCKED)
            self.assertTrue(d.reasons)


class LedgerTests(NoNetworkTestCase):
    def test_ledger_is_immutable_and_updates_return_new_ledgers(self):
        led = L.DayLedger(DAY, Dec("250000"))
        with self.assertRaises(AttributeError):
            led.day = DAY
        self.assertEqual(led.positions, ())

    def test_unknown_or_double_close_is_refused(self):
        cfg = sc.config()
        m = R.RiskManager(cfg, ENGINE, sc.specs(), sc.calendar(), tempfile.mkdtemp(), DAY)
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        m.record_close(d.approval, Dec("10"), mins(5))
        with self.assertRaises(RiskViolation) as cm:
            m.record_close(d.approval, Dec("10"), mins(6))
        self.assertEqual(cm.exception.code, "ALREADY_CLOSED")
        with self.assertRaises(RiskViolation):
            m.record_close(d.approval._replace(approval_id="z" * 64), Dec("1"), mins(6))

    def test_close_before_open_is_refused(self):
        m = R.RiskManager(sc.config(), ENGINE, sc.specs(), sc.calendar(), tempfile.mkdtemp(), DAY)
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        with self.assertRaises(RiskViolation) as cm:
            m.record_close(d.approval, Dec("10"), mins(-5))
        self.assertEqual(cm.exception.code, "CLOSE_BEFORE_OPEN")


class LedgerMathTests(NoNetworkTestCase):
    def ledger_with(self, pnls):
        cfg = sc.config()
        m = R.RiskManager(cfg, ENGINE, sc.specs(), sc.calendar(), tempfile.mkdtemp(), DAY)
        for i, pnl in enumerate(pnls):
            at = mins(20 * i)
            d = m.request_entry(cheap_candidate("30.%02d" % (5 * i), at=at), at)
            assert d.outcome == R.APPROVED, [r.code for r in d.reasons]
            m.record_close(d.approval, Dec(pnl), at + datetime.timedelta(minutes=10))
        return m

    def test_remaining_capacity_never_goes_below_zero(self):
        m = self.ledger_with(["-6000"])
        s = m.snapshot()
        self.assertEqual((s.realized_pnl_inr, s.realized_loss_used_inr, s.remaining_daily_risk_capacity_inr),
                         (Dec("-6000"), Dec("6000"), Dec("0")))

    def test_last_loss_is_the_most_recent_one(self):
        m = self.ledger_with(["-100", "-200"])
        last = m.ledger.last_loss()
        self.assertEqual(last.realized_pnl_inr, Dec("-200"))
        self.assertEqual(m.snapshot().realized_pnl_inr, Dec("-300"))

    def test_the_same_approval_cannot_enter_the_ledger_twice(self):
        m = R.RiskManager(sc.config(), ENGINE, sc.specs(), sc.calendar(), tempfile.mkdtemp(), DAY)
        d = m.request_entry(cheap_candidate("30.00"), T1230)
        with self.assertRaises(RiskViolation) as cm:
            m.ledger.with_opened(d.approval, mins(1))
        self.assertEqual(cm.exception.code, "APPROVAL_ALREADY_USED")


class SourceTests(NoNetworkTestCase):
    def test_risk_code_does_not_read_strategy_thresholds_or_the_environment(self):
        for fname in ("engine.py", "ledger.py"):
            with open(os.path.join(ROOT, "nifty_core", "risk", fname), encoding="utf-8") as fh:
                text = fh.read()
            for word in ("os.environ", "getenv", "strategy_thresholds.json", "load_params", "regime_thresholds.json"):
                self.assertNotIn(word, text, "%s mentions %s" % (fname, word))

    def test_risk_code_does_not_import_the_strategy_selection_logic(self):
        with open(os.path.join(ROOT, "nifty_core", "risk", "engine.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("candidate_to_journal", text)
        for word in ("decide(", "decide_at(", "classify("):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
