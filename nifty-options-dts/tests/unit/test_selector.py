"""Selector tests: thresholds, market analysis, credit structures, and the choice among the four strategies."""
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import cli
from nifty_core.data.models import OptionContract
from nifty_core.errors import ConfigError, SpreadError
from nifty_core.instruments.leg import BUY, SELL
from nifty_core.selector import analysis as A
from nifty_core.selector import engine as E
from nifty_core.selector import params as SP
from nifty_core.selector import report as R
from nifty_core.selector import structures as S
from tests import _scenarios as sc
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase

HV = dict(vol_high_stdev_pct=Dec("0.06"), vol_low_stdev_pct=Dec("0.02"))        # makes ordinary volatility count as HIGH


def values(**override):
    v = {f: getattr(H.PARAMS, f) for f in SP.FIELDS}
    v.update(override)
    return v


def selection(name, hhmm, p=H.PARAMS, **kw):
    return E.select_at(sc.provider(name), H.ctx(), p, H.at(name, hhmm), data_label="FIXTURE_SYNTHETIC", **kw)


class ParamsTests(NoNetworkTestCase):
    def test_default_file_loads_with_a_stable_fingerprint(self):
        self.assertEqual(SP.load_params().fingerprint, SP.load_params().fingerprint)
        self.assertEqual(len(H.PARAMS.fingerprint), 16)

    def test_changing_a_value_changes_the_fingerprint(self):
        self.assertNotEqual(SP.build_params(values(min_confidence=70)).fingerprint, H.PARAMS.fingerprint)

    def test_unknown_missing_and_bad_values_are_refused(self):
        bad = [
            dict(values(), surprise=1),
            {k: v for k, v in values().items() if k != "min_confidence"},
            values(min_confidence=101), values(min_confidence=True), values(momentum_window=0),
            values(room_min_pct=0), values(room_min_pct=True), values(credit_width_options=[]),
            values(credit_width_options=[50, 50]), values(fly_width_options=[0]),
            values(latest_entry_credit_ist="1:30pm"), values(strong_net_move_pct=Dec("0.1")),
            values(range_efficiency_max=Dec("0.5")), values(momentum_flat_max_pct=Dec("0.5")),
            values(vol_low_stdev_pct=Dec("0.5")), values(min_credit_to_width_fly=1),
            values(range_mid_low=Dec("0.8")),
        ]
        for v in bad:
            with self.assertRaises(ConfigError, msg=str(sorted(v.items())[:0])):
                SP.build_params(v)


class AnalysisTests(NoNetworkTestCase):
    def a(self, name, hhmm):
        cs = A.completed_candles(sc.provider(name), sc.calendar(), H.ctx().regime_params, H.at(name, hhmm))
        return A.analyse(cs, H.at(name, hhmm), H.PARAMS)

    def test_trend_day_is_trending_bullish_with_levels(self):
        a = self.a("bullish_trend", "12:30")
        self.assertEqual((a.structure, a.strength, a.momentum), (A.TRENDING_BULLISH, "STRONG", A.MOM_UP))
        self.assertGreater(a.net_move_pct, Dec("0.35"))
        self.assertIsNotNone(a.support)
        self.assertLess(a.support, a.spot)

    def test_quiet_day_is_range_bound(self):
        a = self.a("sideways", "12:30")
        self.assertEqual(a.structure, A.RANGE_BOUND)

    def test_too_few_candles_gives_no_reading(self):
        cs = A.completed_candles(sc.provider("sideways"), sc.calendar(), H.ctx().regime_params, H.at("sideways", "09:45"))
        self.assertIsNone(A.analyse(cs, H.at("sideways", "09:30"), H.PARAMS))

    def test_candles_still_forming_are_ignored(self):
        day = sc.scenario("sideways").day
        cs = H.candles_from(day, [Dec(25000) + i for i in range(20)])
        full = A.analyse(cs, H.at("sideways", "12:30"), H.PARAMS)
        early = A.analyse(cs, H.at("sideways", "10:30"), H.PARAMS)      # 10:30 -> only the first 15 are complete
        self.assertEqual(full.candles_used, 20)
        self.assertEqual(early.candles_used, 15)

    def test_a_pinned_day_has_tiny_efficiency_and_flat_momentum(self):
        day = sc.scenario("sideways").day
        cs = H.candles_from(day, [Dec(25000) + (5 if i % 2 else -5) for i in range(30)])
        a = A.analyse(cs, H.at("sideways", "12:30"), H.PARAMS)
        self.assertEqual((a.structure, a.strength, a.momentum), (A.RANGE_BOUND, "TIGHT", A.MOM_FLAT))
        self.assertLess(a.efficiency, Dec("0.08"))


class StructureTests(NoNetworkTestCase):
    def chain(self):
        return sc.provider("sideways").get_option_chain(as_of=H.at("sideways", "12:30"))

    def contract(self, chain, strike, opt):
        return OptionContract("NIFTY", chain.expiries()[0], Dec(strike), opt)

    def build(self, kind, legs):
        chain = self.chain()
        built = [S.priced_leg(chain, self.contract(chain, k, o), side, 1, sc.specs(), "fixture") for k, o, side in legs]
        return S.CreditStructure.create(kind, built, sc.specs(), chain.snapshot_time.date(), "fixture")

    def atm(self):
        chain = self.chain()
        return int((chain.spot / 50).to_integral_value()) * 50

    def test_bull_put_spread_figures(self):
        k = self.atm() - 100
        s = self.build(S.KIND_BULL_PUT, [(k, "PE", SELL), (k - 100, "PE", BUY)])
        self.assertEqual(s.width_points, 100)
        self.assertGreater(s.net_credit_points, 0)
        self.assertEqual(s.gross_max_profit_inr, s.net_credit_points * s.units)
        self.assertEqual(s.gross_max_loss_inr, (100 - s.net_credit_points) * s.units)
        self.assertEqual(s.lower_breakeven, k - s.net_credit_points)
        self.assertIsNone(s.upper_breakeven)

    def test_bear_call_spread_and_iron_condor_and_fly(self):
        a = self.atm()
        bc = self.build(S.KIND_BEAR_CALL, [(a + 100, "CE", SELL), (a + 200, "CE", BUY)])
        self.assertEqual(bc.upper_breakeven, a + 100 + bc.net_credit_points)
        ic = self.build(S.KIND_IRON_CONDOR, [(a + 150, "CE", SELL), (a + 250, "CE", BUY), (a - 150, "PE", SELL), (a - 250, "PE", BUY)])
        self.assertEqual(len(ic.legs), 4)
        self.assertLess(ic.lower_breakeven, a - 150)
        self.assertGreater(ic.upper_breakeven, a + 150)
        fly = self.build(S.KIND_IRON_FLY, [(a, "CE", SELL), (a + 100, "CE", BUY), (a, "PE", SELL), (a - 100, "PE", BUY)])
        self.assertGreater(fly.net_credit_points, ic.net_credit_points)        # the ATM straddle collects more
        self.assertEqual(fly.gross_max_loss_inr, (100 - fly.net_credit_points) * fly.units)

    def test_every_short_needs_a_wing(self):
        a = self.atm()
        with self.assertRaises(SpreadError) as cm:
            self.build(S.KIND_BULL_PUT, [(a - 100, "PE", SELL), (a - 50, "PE", BUY)])          # the long is on the wrong side
        self.assertTrue({c for c, _ in cm.exception.problems} & {"NAKED_SHORT", "WRONG_STRUCTURE"})
        chain = self.chain()
        lone = [S.priced_leg(chain, self.contract(chain, a - 100, "PE"), SELL, 1, sc.specs(), "fixture")]
        self.assertEqual(len(S.uncovered_by_wings(lone)), 1)

    def test_a_condor_with_unequal_wings_or_a_fly_with_split_shorts_is_refused(self):
        a = self.atm()
        with self.assertRaises(SpreadError) as cm:
            self.build(S.KIND_IRON_CONDOR, [(a + 150, "CE", SELL), (a + 250, "CE", BUY), (a - 150, "PE", SELL), (a - 200, "PE", BUY)])
        self.assertIn("UNEQUAL_WINGS", [c for c, _ in cm.exception.problems])
        with self.assertRaises(SpreadError) as cm:
            self.build(S.KIND_IRON_FLY, [(a, "CE", SELL), (a + 100, "CE", BUY), (a - 50, "PE", SELL), (a - 150, "PE", BUY)])
        self.assertIn("WRONG_STRUCTURE", [c for c, _ in cm.exception.problems])

    def test_a_debit_is_not_a_credit_structure(self):
        a = self.atm()
        with self.assertRaises(SpreadError) as cm:
            self.build(S.KIND_BEAR_CALL, [(a + 200, "CE", SELL), (a + 100, "CE", BUY)])
        self.assertTrue({c for c, _ in cm.exception.problems} & {"WRONG_STRUCTURE", "NAKED_SHORT"})

    def test_off_grid_strikes_and_wrong_leg_count_are_refused(self):
        chain, a = self.chain(), self.atm()
        legs = [S.priced_leg(chain, self.contract(chain, a - 100, "PE"), SELL, 1, sc.specs(), "fixture")]
        with self.assertRaises(SpreadError) as cm:
            S.CreditStructure.create(S.KIND_IRON_CONDOR, legs, sc.specs(), chain.snapshot_time.date(), "fixture")
        self.assertIn("WRONG_LEG_COUNT", [c for c, _ in cm.exception.problems])
        with self.assertRaises(SpreadError):
            S.CreditStructure.create("RATIO_SPREAD", legs, sc.specs(), chain.snapshot_time.date(), "fixture")

    def test_immutable_and_pnl_from_actual_exit_prices(self):
        a = self.atm()
        s = self.build(S.KIND_BEAR_CALL, [(a + 100, "CE", SELL), (a + 200, "CE", BUY)])
        with self.assertRaises(AttributeError):
            s.kind = "X"
        with self.assertRaises(TypeError):
            S.CreditStructure()
        sell, buy = s.legs
        flat = s.gross_pnl_from_exit_prices([sell.entry_price, buy.entry_price])
        self.assertEqual(flat, 0)
        win = s.gross_pnl_from_exit_prices([sell.entry_price - 5, buy.entry_price])
        self.assertEqual(win, 5 * s.units)
        with self.assertRaises(SpreadError):
            s.gross_pnl_from_exit_prices([1])

    def test_defined_risk_includes_loss_costs_and_slippage(self):
        a = self.atm()
        s = self.build(S.KIND_BEAR_CALL, [(a + 100, "CE", SELL), (a + 200, "CE", BUY)])
        total, parts = s.defined_risk_inr(H.ctx().cost_engine, Dec("0.5"))
        self.assertGreater(total, s.gross_max_loss_inr)
        self.assertEqual(total, sum(parts.values(), Dec(0)))
        self.assertEqual(parts["slippage_inr"], Dec("0.5") * 2 * s.units)


class SelectionTests(NoNetworkTestCase):
    def test_strong_trend_selects_the_debit_spread_with_every_output_section(self):
        s = selection("bullish_trend", "12:30")
        self.assertEqual((s.outcome, s.selected), (E.TRADE, E.DEBIT))
        self.assertEqual(s.analysis.structure, A.TRENDING_BULLISH)
        self.assertGreaterEqual(s.confidence, H.PARAMS.min_confidence)
        self.assertIn(s.confidence_label, ("MODERATE", "HIGH"))
        self.assertTrue(s.why and s.trigger)
        self.assertEqual(s.setup["kind"], "BULL_CALL_DEBIT")
        for key in ("legs", "max_profit_inr", "max_loss_inr", "reward_to_risk", "worst_case_with_costs_inr", "breakevens_at_expiry"):
            self.assertIn(key, s.setup)
        self.assertLessEqual(Dec(s.setup["worst_case_with_costs_inr"]), sc.config().risk_per_trade_inr)
        self.assertGreaterEqual(Dec(s.setup["reward_to_risk"]), H.PARAMS.min_rr_debit)
        self.assertEqual(set(s.exit_rules), {"target", "stop_loss", "time_exit", "early_exit"})
        self.assertIn("15:15", s.exit_rules["time_exit"])
        self.assertIn("overnight", s.exit_rules["time_exit"])

    def test_range_day_selects_the_iron_condor(self):
        s = selection("sideways", "12:30")
        self.assertEqual((s.outcome, s.selected), (E.TRADE, E.CONDOR))
        self.assertEqual(len(s.setup["legs"]), 4)
        self.assertEqual(sorted(l["action"] for l in s.setup["legs"]), ["BUY", "BUY", "SELL", "SELL"])
        a = s.analysis
        shorts = {l["type"]: Dec(l["strike"]) for l in s.setup["legs"] if l["action"] == "SELL"}
        self.assertGreater(shorts["CE"], a.day_high)            # short strikes sit OUTSIDE the day's range
        self.assertLess(shorts["PE"], a.day_low)
        self.assertIn("net_credit_points", s.setup)

    def test_credit_stop_is_capped_below_the_worst_case(self):
        s = selection("sideways", "12:30")
        credit, max_loss = Dec(s.setup["net_credit_points"]), Dec(s.setup["max_loss_inr"])
        units = s.setup["units"]
        cost = Dec(s.exit_rules["stop_loss"].split("costs ")[1].split(" points")[0])
        stop_loss_inr = (cost - credit) * units
        self.assertLessEqual(stop_loss_inr, max_loss * H.PARAMS.credit_stop_max_pct_of_max_loss / 100 + 1)
        self.assertLess(stop_loss_inr, max_loss)

    def test_a_pinned_low_volatility_day_selects_the_iron_butterfly(self):
        s = H.decide("sideways", "12:30", H.pinned(5))
        self.assertEqual((s.outcome, s.selected), (E.TRADE, E.FLY))
        strikes = {l["type"]: l["strike"] for l in s.setup["legs"] if l["action"] == "SELL"}
        self.assertEqual(strikes["CE"], strikes["PE"])           # a straddle at one strike
        self.assertEqual(s.analysis.volatility, A.VOL_LOW)

    def test_credit_spread_when_premium_is_rich_and_the_trend_is_steady(self):
        for slope, kind, bull in ((4, "BULL_PUT_CREDIT", True), (-4, "BEAR_CALL_CREDIT", False)):
            s = H.decide("bullish_trend", "12:30", H.trending(slope, 3), H.params(**HV))
            self.assertEqual((s.outcome, s.selected, s.setup["kind"]), (E.TRADE, E.CREDIT, kind), slope)
            sold = next(l for l in s.setup["legs"] if l["action"] == "SELL")
            spot = s.analysis.spot
            self.assertTrue(Dec(sold["strike"]) < spot if bull else Dec(sold["strike"]) > spot)      # short strike out of the money
            self.assertEqual(sold["type"], "PE" if bull else "CE")

    def test_slow_drift_in_cheap_premium_is_no_trade(self):
        s = H.decide("bullish_trend", "12:30", H.trending(4, 3))
        self.assertEqual(s.outcome, E.NO_TRADE)
        names = {e.strategy: [c.name for c in e.blockers] for e in s.evaluations}
        self.assertIn("premium worth selling", names[E.CREDIT])          # credit: premium too thin
        self.assertTrue(names[E.DEBIT])                                  # debit: momentum / room not good enough

    def test_unclear_market_is_no_trade_with_a_reason_for_every_strategy(self):
        s = selection("sideways", "10:30")
        self.assertEqual(s.outcome, E.NO_TRADE)
        self.assertEqual(s.reasons[0].code, "NO_STRATEGY_QUALIFIES")
        self.assertEqual(len(s.reasons), 5)
        self.assertIsNone(s.selected)
        self.assertIsNone(s.setup)

    def test_abnormal_volatility_event_and_bad_data_are_no_trade(self):
        s = selection("high_volatility", "12:30")
        codes = {r.code for r in s.reasons}
        self.assertEqual(s.outcome, E.NO_TRADE)
        self.assertTrue({"ABNORMAL_VOLATILITY", "HIGH_IMPACT_EVENT"} <= codes)
        s = selection("missing_invalid_data", "12:30")
        self.assertEqual(s.outcome, E.NO_TRADE)

    def test_stale_chain_and_outside_the_entry_window_are_no_trade(self):
        s = E.select_at(sc.provider("bullish_trend"), H.ctx(), H.PARAMS, H.at("bullish_trend", "11:30"), data_label="FIXTURE_SYNTHETIC")
        self.assertIn("CHAIN_STALE", {r.code for r in s.reasons})
        s = selection("bullish_trend", "09:45")
        self.assertIn("OUTSIDE_ENTRY_WINDOW", {r.code for r in s.reasons})

    def test_late_entry_rules_per_strategy(self):
        s = selection("sideways", "14:30")                       # past the credit cut-off
        self.assertEqual(s.outcome, E.NO_TRADE)
        condor = next(e for e in s.evaluations if e.strategy == E.CONDOR)
        self.assertIn("time window", [c.name for c in condor.blockers])

    def test_only_one_trade_at_a_time_and_the_kill_switch(self):
        s = selection("bullish_trend", "12:30", position_open=True)
        self.assertEqual((s.outcome, s.reasons[0].code), (E.NO_TRADE, "ONE_TRADE_AT_A_TIME"))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(selection("bullish_trend", "12:30", kill_dir=d).outcome, E.TRADE)
            open(os.path.join(d, "KILL"), "w").close()
            s = selection("bullish_trend", "12:30", kill_dir=d)
            self.assertEqual((s.outcome, s.reasons[0].code), (E.NO_TRADE, "KILL_SWITCH"))

    def test_a_trade_that_does_not_fit_the_risk_limit_is_never_selected(self):
        tiny = H.ctx()._replace(cfg=sc.config()._replace(risk_per_trade_inr=Dec(50)))
        s = H.decide("bullish_trend", "12:30", context=tiny)
        self.assertEqual(s.outcome, E.NO_TRADE)
        s = H.decide("sideways", "12:30", context=tiny)
        self.assertEqual(s.outcome, E.NO_TRADE)

    def test_illiquid_options_are_never_selected(self):
        s = selection("illiquid_options", "12:30")
        self.assertEqual(s.outcome, E.NO_TRADE)

    def test_confidence_below_the_minimum_means_no_trade(self):
        s = selection("bullish_trend", "12:30", H.params(min_confidence=99))
        self.assertEqual(s.outcome, E.NO_TRADE)
        self.assertEqual(s.reasons[0].code, "NO_STRATEGY_QUALIFIES")
        self.assertTrue(any("below the minimum" in r.text for r in s.reasons))

    def test_highest_confidence_wins_when_two_strategies_qualify(self):
        s = selection("bullish_trend", "12:30")
        scores = {e.strategy: e.confidence for e in s.evaluations if e.eligible}
        self.assertGreaterEqual(len(scores), 2)                 # the credit spread also qualified ...
        self.assertEqual(s.selected, max(scores, key=scores.get))   # ... and the better fit was chosen
        self.assertTrue(any(w.startswith("Not Directional Credit Spread") for w in s.why))

    def test_invariants_over_every_fixture_and_snapshot(self):
        limit = sc.config().risk_per_trade_inr
        for name in sc.NAMES:
            for hhmm in ("09:45", "10:30", "12:30", "14:30"):
                s = selection(name, hhmm)
                self.assertIn(s.outcome, (E.TRADE, E.NO_TRADE))
                if s.outcome == E.NO_TRADE:
                    self.assertTrue(s.reasons, (name, hhmm))
                    self.assertIsNone(s.selected)
                    continue
                chosen = next(e for e in s.evaluations if e.strategy == s.selected)
                self.assertTrue(all(c.passed for c in chosen.conditions if c.required), (name, hhmm))
                self.assertGreaterEqual(s.confidence, H.PARAMS.min_confidence)
                self.assertLessEqual(Dec(s.setup["worst_case_with_costs_inr"]), limit)
                self.assertEqual(len([e for e in s.evaluations if e.strategy == s.selected]), 1)
                actions = [l["action"] for l in s.setup["legs"]]
                self.assertEqual(actions.count("BUY") >= 1, True)          # a short is never alone
                if "SELL" in actions and s.selected != E.DEBIT:
                    self.assertEqual(actions.count("BUY"), actions.count("SELL"))

    def test_same_inputs_give_the_same_decision(self):
        a, b = selection("sideways", "12:30"), selection("sideways", "12:30")
        self.assertEqual(R.to_json(a), R.to_json(b))


class RecheckTests(NoNetworkTestCase):
    def test_unchanged_market_confirms(self):
        a = selection("bullish_trend", "12:30")
        status, _ = E.recheck(a, selection("bullish_trend", "12:30"), H.PARAMS)
        self.assertEqual(status, "CONFIRMED")

    def test_a_big_move_drops_the_original_trade(self):
        a = selection("bullish_trend", "12:30")
        b = H.decide("bullish_trend", "12:30", H.trending(4, 3), H.params(**HV))
        status, text = E.recheck(a, b, H.PARAMS)
        self.assertEqual(status, "RE_EVALUATED")
        self.assertIn("dropped", text)

    def test_spot_move_alone_forces_re_evaluation(self):
        a = selection("bullish_trend", "12:30")
        moved = a._replace(analysis=a.analysis._replace(spot=a.analysis.spot * Dec("1.01")))
        status, text = E.recheck(a, moved, H.PARAMS)
        self.assertEqual(status, "RE_EVALUATED")
        self.assertIn("spot moved", text)

    def test_conditions_turning_to_no_trade_cancels(self):
        a = selection("bullish_trend", "12:30")
        status, _ = E.recheck(a, selection("bullish_trend", "12:30", position_open=True), H.PARAMS)
        self.assertEqual(status, "CANCELLED")

    def test_nothing_to_confirm_after_no_trade(self):
        a = selection("sideways", "10:30")
        self.assertEqual(E.recheck(a, a, H.PARAMS)[0], "NOT_APPLICABLE")


class ReportTests(NoNetworkTestCase):
    def test_trade_report_has_every_required_section(self):
        text = R.render(selection("bullish_trend", "12:30"))
        for heading in ("MARKET CONDITION", "SELECTED STRATEGY", "WHY THIS STRATEGY", "TRADE TRIGGER", "TRADE SETUP",
                        "EXIT RULES", "CONFIDENCE", "target", "stop-loss", "time exit", "early exit", "max profit",
                        "max loss", "reward-to-risk", "Not financial advice"):
            self.assertIn(heading, text)

    def test_no_trade_report_says_so_plainly(self):
        text = R.render(selection("sideways", "10:30"))
        self.assertIn("NO TRADE", text)
        self.assertIn("WHY NO TRADE", text)
        self.assertNotIn("TRADE SETUP", text)

    def test_json_form_is_serialisable(self):
        for name, hhmm in (("bullish_trend", "12:30"), ("sideways", "10:30")):
            data = json.loads(json.dumps(R.to_json(selection(name, hhmm)), default=str))
            self.assertEqual(data["thresholds_fingerprint"], H.PARAMS.fingerprint)


class CliTests(NoNetworkTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        code = cli.main(["select-strategy"] + list(argv), out)
        return code, out.getvalue()

    def test_text_and_json_and_recheck(self):
        code, text = self.run_cli("--scenario", "bullish_trend", "--at", "12:30", "--profile", "intraday")
        self.assertEqual(code, 0)
        self.assertIn("Directional Debit Spread", text)
        self.assertIn("PAPER TRADING ONLY", text)
        code, text = self.run_cli("--scenario", "sideways", "--at", "12:30", "--profile", "intraday", "--json")
        self.assertEqual(json.loads(text)["selected_strategy"], E.CONDOR)
        code, text = self.run_cli("--scenario", "bullish_trend", "--at", "12:30", "--profile", "intraday",
                                  "--recheck-at", "12:30", "--brief")
        self.assertIn("RE-EVALUATION AT ENTRY TIME 12:30: CONFIRMED", text)
        self.assertNotIn("HOW EACH STRATEGY", text)

    def test_position_open_flag_and_no_trade(self):
        code, text = self.run_cli("--scenario", "bullish_trend", "--at", "12:30", "--profile", "intraday",
                                  "--position-open", "--kill-dir", tempfile.gettempdir())
        self.assertEqual(code, 0)
        self.assertIn("NO TRADE", text)
        self.assertIn("one strategy may be active", text)


if __name__ == "__main__":
    unittest.main()
