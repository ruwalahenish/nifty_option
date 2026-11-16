"""Trade readiness: every status, the checklist, the entry, the next action and the decision history are derived from the
selector's real evaluation. These tests build readiness from real fixture-day decisions and check it against them."""
import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core.selector import analysis as A
from nifty_core.selector import engine as E
from nifty_core.selector import readiness as RD
from tests import _scenarios as sc
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase
from tests.unit.test_selector import selection

CFG = sc.config()
P = H.PARAMS


def build(sel, confirm=None):
    return RD.build(sel, P, CFG, confirm)


def row(ready, name):
    return next(r for r in ready["checklist"] if r["name"].startswith(name))


def far_expiry_ctx(days=30):
    ctx = H.ctx()
    return ctx._replace(strategy_params=ctx.strategy_params._replace(min_days_to_expiry=days))


class SelectorReadsAllFourEvenWhenBlockedTests(NoNetworkTestCase):
    """The market-side conditions must still be visible when something other than the market blocks trading."""

    def test_an_unusable_expiry_still_evaluates_the_four_strategies(self):
        s = H.decide("sideways", "12:30", context=far_expiry_ctx())
        self.assertEqual((s.outcome, s.reasons[0].code), (E.NO_TRADE, "NO_ELIGIBLE_EXPIRY"))
        self.assertEqual(len(s.evaluations), 4)
        self.assertTrue(all(e.proposal is None for e in s.evaluations))          # a blocked read builds no structure
        condor = next(e for e in s.evaluations if e.strategy == E.CONDOR)
        self.assertEqual(condor.blockers[0].name, "option chain usable")          # all its market conditions pass; the chain does not
        self.assertIn("at least 30 days away", condor.blockers[0].detail)

    def test_the_listed_expiries_are_named_in_the_reason(self):
        s = H.decide("sideways", "12:30", context=far_expiry_ctx())
        self.assertIn("listed:", s.reasons[0].text)

    def test_a_stale_chain_and_a_scheduled_event_also_keep_the_evaluations(self):
        s = E.select_at(sc.provider("bullish_trend"), H.ctx(), H.PARAMS, H.at("bullish_trend", "11:30"), data_label="FIXTURE_SYNTHETIC")
        self.assertIn("CHAIN_STALE", {r.code for r in s.reasons})
        self.assertEqual(len(s.evaluations), 4)
        s = selection("high_volatility", "12:30")
        self.assertEqual(len(s.evaluations), 4)
        self.assertIn("HIGH_IMPACT_EVENT", {r.code for r in s.reasons})

    def test_a_gate_other_than_the_chain_is_called_trading_permitted_now(self):
        s = selection("bullish_trend", "12:30", position_open=True)
        self.assertEqual(s.reasons[0].code, "ONE_TRADE_AT_A_TIME")
        names = {b.name for e in s.evaluations for b in e.blockers}
        self.assertIn("trading permitted now", names)

    def test_nothing_changes_when_nothing_blocks(self):
        s = selection("bullish_trend", "12:30")
        self.assertEqual((s.outcome, s.selected), (E.TRADE, E.DEBIT))
        self.assertTrue(s.narratives and E.DEBIT in s.narratives)


class StatusTests(NoNetworkTestCase):
    def test_a_pick_that_has_not_yet_held_is_waiting_for_confirmation(self):
        r = build(selection("bullish_trend", "12:30"), {"have": 1})
        self.assertEqual(r["status"], RD.WAITING_CONFIRMATION)
        self.assertEqual(row(r, "confirmation")["status"], RD.PENDING)
        self.assertIn("1 of 2 slots", row(r, "confirmation")["detail"])
        self.assertEqual(row(r, "risk gate")["status"], RD.PENDING)
        self.assertIn("1 so far", r["next_action"])

    def test_a_confirmed_pick_is_ready_for_risk_validation_never_ready_to_execute(self):
        r = build(selection("bullish_trend", "12:30"), {"have": 2})
        self.assertEqual(r["status"], RD.READY_RISK)
        self.assertEqual(row(r, "confirmation")["status"], RD.PASSED)
        self.assertEqual(row(r, "risk gate")["status"], RD.PENDING)              # not passed until the gate has actually run
        self.assertNotEqual(build(selection("bullish_trend", "12:30"))["status"], RD.READY_EXECUTE)

    def test_ready_to_execute_needs_the_risk_gate_to_have_approved(self):
        r = build(selection("bullish_trend", "12:30"), {"have": 2})
        RD.finalize(r, "AWAITING_APPROVAL", {})
        self.assertEqual(r["status"], RD.READY_EXECUTE)
        self.assertEqual(row(r, "risk gate")["status"], RD.PASSED)
        self.assertIn("'yes'", r["next_action"])

    def test_an_entered_trade_is_trade_executed(self):
        r = build(selection("bullish_trend", "12:30"), {"have": 2})
        RD.finalize(r, "ENTERED", {})
        self.assertEqual(r["status"], RD.EXECUTED)
        self.assertEqual(row(r, "risk gate")["status"], RD.PASSED)
        self.assertEqual(r["counts"][RD.PENDING], 0)
        self.assertIn("paper trade was entered", r["next_action"])

    def test_a_risk_block_is_execution_blocked_and_says_why(self):
        r = build(selection("bullish_trend", "12:30"), {"have": 2})
        RD.finalize(r, "BLOCKED", {"risk": {"reasons": [{"code": "DAILY_LOSS_LIMIT_REACHED", "text": "realized loss 2000 has reached the daily limit"}]}})
        self.assertEqual(r["status"], RD.BLOCKED)
        self.assertEqual(row(r, "risk gate")["status"], RD.FAILED)
        self.assertIn("Signal confirmed", r["next_action"])
        self.assertIn("risk validation failed", r["next_action"])
        self.assertIn("daily limit", r["next_action"])

    def test_a_refused_fill_a_declined_ticket_and_an_expired_ticket_are_blocked_not_executed(self):
        for status in ("ENTRY_NOT_FILLED", "DECLINED", "TICKET_EXPIRED"):
            r = build(selection("bullish_trend", "12:30"), {"have": 2})
            RD.finalize(r, status, {"reasons_not_filled": [{"code": "STALE_QUOTE", "text": "quote is old"}]})
            self.assertEqual(r["status"], RD.BLOCKED, status)
            self.assertNotEqual(r["status"], RD.EXECUTED)

    def test_a_market_that_suits_no_strategy_is_no_valid_strategy(self):
        s = selection("sideways", "10:30")
        r = build(s)
        self.assertEqual(s.analysis.structure, A.UNCLEAR)
        self.assertEqual(r["status"], RD.NO_VALID)
        self.assertIn("unsuitable for all four strategies", r["next_action"])

    def test_a_trending_market_below_the_bar_is_waiting_for_a_signal_and_names_what_it_waits_for(self):
        s = H.decide("bullish_trend", "12:30", H.trending(4, 3))
        r = build(s)
        self.assertEqual((s.outcome, r["status"]), (E.NO_TRADE, RD.WAITING_SIGNAL))
        self.assertIn("Waiting for:", r["next_action"])
        self.assertIn("Closest to qualifying: %s" % r["evaluated_strategy"], r["next_action"])
        self.assertFalse(r["evaluated_selected"])

    def test_a_technical_block_is_execution_blocked_with_the_gate_row_in_error(self):
        r = build(H.decide("sideways", "12:30", context=far_expiry_ctx()))
        self.assertEqual(r["status"], RD.BLOCKED)
        self.assertEqual(r["technical"][0]["kind"], "EXPIRY")
        self.assertEqual(row(r, "data, expiry")["status"], RD.ERROR)
        self.assertEqual(row(r, "option structure")["status"], RD.ERROR)
        self.assertEqual(r["counts"][RD.ERROR], 2)
        self.assertIn("non-market condition", r["next_action"])
        states = {s["strategy"]: s["state"] for s in r["strategies"]}
        self.assertEqual(states["Iron Condor"], "BLOCKED (technical)")            # its market conditions pass; the expiry does not

    def test_data_and_safety_gates_are_blocking_and_timing_is_not(self):
        stale = E.select_at(sc.provider("bullish_trend"), H.ctx(), H.PARAMS, H.at("bullish_trend", "11:30"), data_label="FIXTURE_SYNTHETIC")
        self.assertEqual(build(stale)["status"], RD.BLOCKED)
        self.assertEqual(build(stale)["technical"][0]["kind"], "DATA")
        event = selection("high_volatility", "12:30")
        self.assertEqual(build(event)["status"], RD.BLOCKED)
        self.assertTrue({"SAFETY"} <= {i["kind"] for i in build(event)["technical"]})
        early = selection("bullish_trend", "09:45")                                   # outside the entry window
        r = build(early)
        self.assertEqual(r["status"], RD.WAITING_SIGNAL)
        self.assertEqual(row(r, "data, expiry")["status"], RD.FAILED)

    def test_the_kill_switch_is_a_safety_block(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "KILL"), "w").close()
            r = build(selection("bullish_trend", "12:30", kill_dir=d))
        self.assertEqual((r["status"], r["technical"][0]["kind"]), (RD.BLOCKED, "SAFETY"))


class EntryTests(NoNetworkTestCase):
    def test_a_real_structure_shows_strikes_premiums_stop_target_and_reward_to_risk(self):
        s = selection("bullish_trend", "12:30")
        e = build(s, {"have": 1})["entry"]
        self.assertTrue(e["available"])
        self.assertEqual(e["underlying"], "{:,.2f}".format(s.analysis.spot))
        self.assertEqual([(l["action"], l["type"]) for l in e["legs"]], [("BUY", "CE"), ("SELL", "CE")])
        self.assertEqual([l["price"] for l in e["legs"]], [l["price"] for l in s.setup["legs"]])      # the selector's own quotes
        self.assertEqual(e["net"]["kind"], "debit")
        self.assertEqual(e["reward_to_risk"], s.setup["reward_to_risk"])
        self.assertIn("-50%", e["stop_loss"])
        self.assertIn("+60%", e["target"])
        self.assertLessEqual(Dec(e["worst_case_inr"]), Dec(e["risk_limit_inr"]))
        self.assertTrue(e["priced_at"].startswith(str(sc.scenario("bullish_trend").day)))       # says when it was priced

    def test_a_credit_structure_shows_all_four_legs_and_a_credit(self):
        e = build(selection("sideways", "12:30"), {"have": 1})["entry"]
        self.assertEqual(sorted(l["action"] for l in e["legs"]), ["BUY", "BUY", "SELL", "SELL"])
        self.assertEqual(e["net"]["kind"], "credit")

    def test_the_trigger_lists_only_conditions_the_live_logic_enforces(self):
        e = build(selection("bullish_trend", "12:30"), {"have": 1})["entry"]
        text = " ".join(e["trigger"])
        self.assertIn("consecutive 5-minute slots", text)
        self.assertNotIn("Entry candle", text)                                          # not enforced live
        self.assertNotIn("still within 0.15%", text)                                    # recheck is not run live
        self.assertIn("fresh", text)

    def test_no_price_is_shown_when_no_structure_exists_and_the_reason_is_given(self):
        r = build(selection("sideways", "10:30"))
        e = r["entry"]
        self.assertFalse(e["available"])
        self.assertEqual(e["label"], "ENTRY NOT AVAILABLE")
        self.assertTrue(e["reason"])
        for key in ("legs", "underlying", "net", "stop_loss", "target"):
            self.assertNotIn(key, e)
        self.assertTrue(e["watch"])

    def test_price_zones_come_from_the_thresholds_and_the_analysis(self):
        s = selection("sideways", "10:30")
        a = s.analysis
        r = build(s)
        condor_zones = RD._zones(next(e for e in s.evaluations if e.strategy == E.CONDOR), a, P)
        band = next(z for z in condor_zones if z["label"] == "Mid-range band")
        span = a.day_high - a.day_low
        self.assertEqual(band["low"], "{:,.2f}".format(a.day_low + P.range_mid_low * span))
        self.assertEqual(band["high"], "{:,.2f}".format(a.day_low + P.range_mid_high * span))
        debit = RD._zones(next(e for e in s.evaluations if e.strategy == E.DEBIT), a, P)
        up = next(z for z in debit if z["label"] == "Bullish trend level")
        self.assertEqual(up["price"], "{:,.2f}".format(a.day_open * (1 + P.trend_net_move_pct / 100)))
        fly = RD._zones(next(e for e in s.evaluations if e.strategy == E.FLY), a, P)
        self.assertEqual(fly[0]["label"], "Pin band")
        self.assertIsNotNone(r)


class DirectionAndChecklistTests(NoNetworkTestCase):
    def test_directions(self):
        self.assertEqual(build(selection("bullish_trend", "12:30"), {"have": 1})["direction"], "BULLISH")
        self.assertEqual(build(selection("sideways", "12:30"), {"have": 1})["direction"], "NEUTRAL")
        bear = H.decide("bullish_trend", "12:30", H.trending(-4, 3), H.params(vol_high_stdev_pct=Dec("0.06"), vol_low_stdev_pct=Dec("0.02")))
        self.assertEqual(build(bear, {"have": 1})["direction"], "BEARISH")

    def test_a_directional_strategy_with_no_trend_has_no_direction_yet(self):
        s = selection("sideways", "10:30")                                              # an unclear market: no trend
        debit = next(e for e in s.evaluations if e.strategy == E.DEBIT)
        self.assertEqual(RD._direction(debit, s.analysis), "NOT DETERMINED (no trend yet)")
        condor = next(e for e in s.evaluations if e.strategy == E.CONDOR)
        self.assertEqual(RD._direction(condor, s.analysis), "NEUTRAL")

    def test_the_checklist_is_the_strategys_own_conditions_in_its_own_words(self):
        s = selection("bullish_trend", "12:30")
        r = build(s, {"have": 1})
        debit = next(e for e in s.evaluations if e.strategy == E.DEBIT)
        required = [c for c in debit.conditions if c.required]
        for c in required:
            self.assertEqual((row(r, c.name)["status"], row(r, c.name)["detail"]), (RD.PASSED, c.detail))
        self.assertEqual(sum(1 for x in r["checklist"] if x["required"] and x["name"] in [c.name for c in required]), len(required))

    def test_a_failed_condition_is_failed_with_its_real_detail_and_an_unmet_extra_is_not_confirmed(self):
        s = selection("sideways", "10:30")
        r = build(s)
        failed = [x for x in r["checklist"] if x["status"] == RD.FAILED]
        self.assertTrue(failed)
        self.assertTrue(all(x["detail"] and x["required"] for x in failed))
        for x in r["checklist"]:
            if not x["required"] and x["status"] not in (RD.PASSED, RD.NA):
                self.assertEqual(x["status"], RD.NOT_CONFIRMED)                          # an optional extra never reads FAILED

    def test_volume_is_marked_not_applicable_because_the_index_has_no_volume(self):
        v = row(build(selection("bullish_trend", "12:30"), {"have": 1}), "volume confirmation")
        self.assertEqual(v["status"], RD.NA)
        self.assertIn("no traded volume", v["detail"])

    def test_pending_rows_come_after_the_market_conditions_they_wait_for(self):
        r = build(selection("sideways", "10:30"))
        self.assertEqual(row(r, "option structure")["status"], RD.PENDING)
        self.assertEqual(row(r, "confirmation")["status"], RD.PENDING)
        self.assertEqual(row(r, "risk gate")["status"], RD.PENDING)

    def test_a_structure_level_failure_is_failed_not_pending(self):
        tiny = H.ctx()._replace(cfg=sc.config()._replace(risk_per_trade_inr=Dec(50)))
        s = H.decide("bullish_trend", "12:30", context=tiny)
        r = build(s)
        self.assertEqual(s.outcome, E.NO_TRADE)
        struct = row(r, "option structure")
        self.assertEqual(r["evaluated_strategy"], "Directional Debit Spread")          # every market condition passes for it
        self.assertEqual(struct["status"], RD.FAILED)                                    # so the failure is the structure itself
        self.assertIn("no debit spread passed", struct["detail"])
        self.assertIn("OVER_RISK_LIMIT", struct["detail"])
        self.assertEqual(row(r, "worst case")["status"], RD.PENDING)


class InvariantsOverEveryFixtureTests(NoNetworkTestCase):
    def test_readiness_is_consistent_for_every_fixture_day_and_snapshot(self):
        names = ["bullish_trend", "bearish_trend", "sideways", "high_volatility", "illiquid_options", "missing_invalid_data"]
        seen = set()
        for name in names:
            for chain in sc.scenario(name).chains:
                hhmm = chain.snapshot_time.strftime("%H:%M")
                s = E.select_at(sc.provider(name), H.ctx(), P, H.at(name, hhmm), data_label="FIXTURE_SYNTHETIC")
                for confirm in (None, {"have": 1}, {"have": 2}):
                    r = build(s, confirm)
                    json.dumps(r)                                                        # JSON-safe
                    seen.add(r["status"])
                    self.assertIn(r["status"], RD.STATUSES, (name, hhmm))
                    self.assertNotIn(r["status"], (RD.READY_EXECUTE, RD.EXECUTED))     # only the pipeline can set these
                    if r["entry"]["available"]:
                        self.assertTrue(any(e.proposal for e in s.evaluations))
                        focus = next(e for e in s.evaluations if E.TITLES[e.strategy] == r["evaluated_strategy"])
                        self.assertEqual([l["strike"] for l in r["entry"]["legs"]], [str(l.strike) for l in focus.proposal.legs])
                    else:
                        self.assertEqual(r["entry"]["label"], "ENTRY NOT AVAILABLE")
                        self.assertTrue(r["entry"]["reason"])
                    for st in r["strategies"]:
                        self.assertLessEqual(st["required_passed"], st["required_total"])
                    counts = r["counts"]
                    self.assertEqual(sum(counts.values()), sum(1 for x in r["checklist"] if x["required"]))
                    self.assertIn("no time or price is promised", r["next_action"])
        self.assertTrue({RD.WAITING_CONFIRMATION, RD.BLOCKED} <= seen, seen)


class HistoryTests(NoNetworkTestCase):
    @staticmethod
    def record(at, strategy, qualifying, status="NO_TRADE", spot="24500.00"):
        return {"decision_time": at, "status": status, "strategy": {"reasons": [{"code": "X", "text": "not yet"}]},
                "readiness": {"status": "WAITING FOR SIGNAL", "evaluated_strategy": strategy, "direction": "NEUTRAL",
                              "regime": {"structure": "RANGE_BOUND", "spot": spot},
                              "entry": {"watch": [{"label": "Pin band"}]},
                              "counts": {"PASSED": 3}, "checklist": [
                                  {"name": "pin", "required": True, "status": "FAILED"},
                                  {"name": "risk gate", "required": True, "status": "PENDING"}],
                              "strategies": [{"strategy": q, "eligible": True} for q in qualifying]}}

    def test_a_row_carries_what_was_evaluated_the_price_what_failed_and_why(self):
        h = RD.history_entry(self.record("2026-11-05T10:30:00+05:30", "Iron Butterfly", []))
        self.assertEqual((h["strategy"], h["price"], h["regime"], h["traded"]), ("Iron Butterfly", "24500.00", "RANGE_BOUND", False))
        self.assertEqual((h["failed_names"], h["pending_count"], h["reason"]), (["pin"], 1, "not yet"))
        self.assertEqual(h["expected"], ["Pin band"])

    def test_a_rejected_opportunity_is_marked_when_it_later_became_valid(self):
        hist = []
        RD.record_in_history(hist, RD.history_entry(self.record("2026-11-05T10:30:00+05:30", "Iron Butterfly", [])))
        RD.record_in_history(hist, RD.history_entry(self.record("2026-11-05T10:35:00+05:30", "Iron Butterfly", [])))
        self.assertEqual([h["later_valid_at"] for h in hist], [None, None])
        RD.record_in_history(hist, RD.history_entry(self.record("2026-11-05T10:40:00+05:30", "Iron Condor", ["Iron Butterfly"])))
        self.assertEqual(hist[0]["later_valid_at"], "2026-11-05T10:40:00+05:30")
        self.assertEqual(hist[1]["later_valid_at"], "2026-11-05T10:40:00+05:30")
        self.assertIsNone(hist[2]["later_valid_at"])

    def test_an_executed_trade_is_flagged_and_a_decision_without_readiness_is_ignored(self):
        h = RD.history_entry(self.record("2026-11-05T10:30:00+05:30", "Iron Condor", ["Iron Condor"], status="ENTERED"))
        self.assertTrue(h["traded"])
        self.assertIsNone(RD.history_entry({"decision_time": "x"}))
        self.assertEqual(RD.record_in_history([], None), [])

    def test_the_history_is_bounded(self):
        hist = []
        for i in range(250):
            RD.record_in_history(hist, RD.history_entry(self.record("2026-11-05T10:%02d:%02d+05:30" % (i // 60, i % 60), "Iron Condor", [])), keep=200)
        self.assertEqual(len(hist), 200)

    def test_it_is_rebuilt_from_journal_records_skipping_follow_ups(self):
        recs = [{"kind": "DECISION", "payload": self.record("2026-11-05T10:30:00+05:30", "Iron Butterfly", [])},
                {"kind": "DECISION", "payload": dict(self.record("2026-11-05T10:31:00+05:30", "x", []), follow_up=True)},
                {"kind": "TICKET", "payload": {}},
                {"kind": "DECISION", "payload": self.record("2026-11-05T10:35:00+05:30", "Iron Condor", ["Iron Butterfly"])}]
        hist = RD.history_from_records(recs)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["later_valid_at"], "2026-11-05T10:35:00+05:30")


if __name__ == "__main__":
    unittest.main()
