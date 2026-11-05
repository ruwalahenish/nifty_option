"""The "human" entry profile: reads a trend sooner and sizes the spread to the budget, but keeps every safety rule."""
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import cli
from nifty_core import pipeline as P
from nifty_core.data.fixture_provider import FixtureProvider
from nifty_core.errors import ConfigError
from nifty_core.journal import journal as J
from nifty_core.regime import params as regime_params
from nifty_core.strategy import params as strategy_params
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase


class _Sink(object):
    def write(self, text):
        pass


class ProfileFilesTests(NoNetworkTestCase):
    def test_the_default_is_unchanged_and_the_human_profile_is_a_different_fingerprinted_set(self):
        strict = P.load_context(sc.config())
        human = P.load_context(sc.config(), "fixture", "human")
        self.assertNotEqual(strict.regime_params.fingerprint, human.regime_params.fingerprint)
        self.assertNotEqual(strict.strategy_params.fingerprint, human.strategy_params.fingerprint)
        self.assertEqual(strict.regime_params, P.load_context(sc.config(), "fixture", "default").regime_params)
        self.assertEqual(strict.position_params, human.position_params)          # exit rules are not part of a profile

    def test_human_is_never_stricter_than_the_reference_on_any_entry_rule(self):
        s, h = regime_params.load_params(), P.load_context(sc.config(), "fixture", "human").regime_params
        self.assertLessEqual(h.min_bars, s.min_bars)
        self.assertLess(h.net_move_pct, s.net_move_pct)
        self.assertLessEqual(h.range_position_high, s.range_position_high)
        self.assertGreaterEqual(h.range_position_low, s.range_position_low)
        self.assertGreater(h.abnormal_bar_range_pct, s.abnormal_bar_range_pct)
        self.assertGreater(h.high_vol_stdev_pct, s.high_vol_stdev_pct)
        self.assertGreaterEqual(h.votes_required * 2, 5)                          # still a strict majority of 4 items
        a, b = strategy_params.load_params(), P.load_context(sc.config(), "fixture", "human").strategy_params
        self.assertTrue(set(a.width_points_options) < set(b.width_points_options))
        self.assertIn(50, b.width_points_options)
        self.assertGreaterEqual(b.max_leg_spread_pct, a.max_leg_spread_pct)
        self.assertLessEqual(b.min_leg_volume, a.min_leg_volume)
        self.assertEqual(b.max_debit_to_width, a.max_debit_to_width)               # debit-to-width limit is kept
        self.assertTrue(b.block_on_high_impact_event)                              # the event block is kept
        self.assertEqual(b.min_days_to_expiry, a.min_days_to_expiry)

    def test_bad_profile_names_are_refused(self):
        for bad in ("../config", "Human", "a/b", "", "no_such_profile"):
            with self.assertRaises(ConfigError, msg=bad):
                P.load_context(sc.config(), "fixture", bad)

    def test_the_entry_window_can_open_when_the_human_regime_first_classifies(self):
        ctx = P.load_context(sc.config()._replace(entry_window_start_ist="10:00"), "fixture", "human")
        P.tick_times(ctx, sc.scenario("bullish_trend").day)
        from nifty_core.strategy import engine as SE
        SE.check_alignment(ctx.cfg, ctx.regime_params, ctx.calendar.session(sc.scenario("bullish_trend").day))
        strict = P.load_context(sc.config()._replace(entry_window_start_ist="10:00"))
        with self.assertRaises(ConfigError):                                       # strict needs 60 minutes of candles
            SE.check_alignment(strict.cfg, strict.regime_params, strict.calendar.session(sc.scenario("bullish_trend").day))


class HumanProfileDayTests(NoNetworkTestCase):
    def setUp(self):
        super(HumanProfileDayTests, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def run_day(self, name, profile):
        scenario = sc.scenario(name)
        ctx = P.load_context(sc.config(), "fixture", profile)          # the DEFAULT 250,000 capital: 2,500 limit
        path = os.path.join(self.dir, "%s_%s.jsonl" % (name, profile))
        jr = J.Journal(path, "fixture", "r")
        summary = P.run_day(FixtureProvider(scenario), scenario.day, ctx, os.path.join(self.dir, "trader"), jr, {})
        return summary, J.read_records(path)

    def test_a_trending_day_now_reaches_a_ticket_inside_the_unchanged_risk_limit(self):
        strict, _ = self.run_day("bullish_trend", None)
        human, records = self.run_day("bullish_trend", "human")
        self.assertEqual(strict.status_counts.get("AWAITING_APPROVAL"), None)      # strict: blocked by the 2,500 limit
        self.assertGreaterEqual(human.status_counts["AWAITING_APPROVAL"], 1)
        self.assertEqual(human.entries, 0)                                         # a ticket is not an entry
        for r in records:
            if r["kind"] == "TICKET":
                self.assertIn("per-trade limit INR 2500.00", r["payload"]["text"])
        approved = [r["payload"] for r in records if r["kind"] == "DECISION"
                    and r["payload"]["status"] == "AWAITING_APPROVAL"]
        for p in approved:
            self.assertLessEqual(Dec(p["risk"]["total_defined_risk_inr"]), Dec(2500))
            self.assertEqual(p["risk"]["outcome"], "APPROVED")
        # at least one decision shows a higher-ranked spread that was too big for the limit and was passed over
        self.assertTrue(any(p["strategy"].get("higher_ranked_blocked_by_risk") for p in approved))
        for skipped in (p["strategy"]["higher_ranked_blocked_by_risk"] for p in approved
                        if p["strategy"].get("higher_ranked_blocked_by_risk")):
            for s in skipped:
                self.assertIn("PER_TRADE_RISK_EXCEEDED", s["reason_codes"])

    def test_days_that_should_not_be_traded_still_are_not(self):
        for name in ("sideways", "high_volatility", "illiquid_options", "missing_invalid_data"):
            summary, _ = self.run_day(name, "human")
            self.assertEqual(set(summary.status_counts), {"NO_TRADE"}, name)
            self.assertEqual(summary.tickets_awaiting, (), name)

    def test_a_yes_still_decides_everything_and_a_journal_still_verifies(self):
        summary, records = self.run_day("bullish_trend", "human")
        fp = summary.tickets_awaiting[0]
        scenario = sc.scenario("bullish_trend")
        ctx = P.load_context(sc.config(), "fixture", "human")
        path = os.path.join(self.dir, "answered.jsonl")
        out = P.run_day(FixtureProvider(scenario), scenario.day, ctx, os.path.join(self.dir, "trader"),
                        J.Journal(path, "fixture", "r2"), {fp: "yes"})
        self.assertEqual(out.entries, 1)
        self.assertTrue(J.verify(path).ok)

    def test_the_cli_takes_the_profile_and_the_default_is_still_strict(self):
        out = []

        class Cap(object):
            def write(self, t):
                out.append(t)
        cli.main(["candidate", "--scenario", "bullish_trend", "--at", "12:30", "--json"], Cap())
        strict_text = "".join(out)
        del out[:]
        cli.main(["candidate", "--scenario", "bullish_trend", "--at", "12:30", "--profile", "human", "--json"], Cap())
        self.assertNotEqual(strict_text, "".join(out))
        self.assertEqual(cli.main(["candidate", "--scenario", "bullish_trend", "--at", "12:30", "--profile", "../x"],
                                  _Sink()), 2)                                      # refused, nothing runs


class IntradayProfileTests(NoNetworkTestCase):
    def setUp(self):
        super(IntradayProfileTests, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.cfg = sc.config()._replace(entry_window_start_ist="09:45")

    def run_day(self, name):
        scenario = sc.scenario(name)
        ctx = P.load_context(self.cfg, "fixture", "intraday")
        path = os.path.join(self.dir, name + ".jsonl")
        summary = P.run_day(FixtureProvider(scenario), scenario.day, ctx, os.path.join(self.dir, "trader"),
                            J.Journal(path, "fixture", "r"), {})
        return summary, J.read_records(path)

    def test_only_the_intraday_profile_carries_a_setup_and_it_can_start_at_0945(self):
        from nifty_core.strategy import engine as SE
        ctx = P.load_context(self.cfg, "fixture", "intraday")
        self.assertIsNotNone(ctx.setup_params)
        self.assertEqual((ctx.regime_params.min_bars, ctx.setup_params.opening_range_candles), (6, 6))
        SE.check_alignment(ctx.cfg, ctx.regime_params, ctx.calendar.session(sc.scenario("bullish_trend").day))
        for profile in (None, "default", "human"):
            self.assertIsNone(P.load_context(sc.config(), "fixture", profile).setup_params)

    def test_the_setup_fingerprint_is_in_the_journal(self):
        _, records = self.run_day("bullish_trend")
        self.assertTrue(records[0]["payload"]["intraday_setup_thresholds"])
        _, strict = self.run_day_plain("bullish_trend")
        self.assertNotIn("intraday_setup_thresholds", strict[0]["payload"])

    def run_day_plain(self, name):
        scenario = sc.scenario(name)
        path = os.path.join(self.dir, name + "_plain.jsonl")
        summary = P.run_day(FixtureProvider(scenario), scenario.day, P.load_context(sc.config()),
                            os.path.join(self.dir, "trader"), J.Journal(path, "fixture", "r"), {})
        return summary, J.read_records(path)

    def test_every_decision_explains_itself_with_a_setup_or_a_gate_reason(self):
        for name in sc.NAMES:
            summary, records = self.run_day(name)
            for r in records:
                if r["kind"] != "DECISION":
                    continue
                reasons = [x["code"] for x in r["payload"]["regime"]["reasons"]]
                self.assertTrue(reasons, name)
                self.assertTrue(all(c.startswith("SETUP_") or c == "INTRADAY_SETUP" or c.isupper() for c in reasons), reasons)
            self.assertTrue(set(summary.status_counts) <= {"NO_TRADE", "BLOCKED", "AWAITING_APPROVAL"}, name)

    def test_chop_bad_data_and_thin_option_days_still_give_no_trade(self):
        for name in ("sideways", "high_volatility", "illiquid_options", "missing_invalid_data"):
            summary, _ = self.run_day(name)
            self.assertEqual(summary.tickets_awaiting, (), name)
            self.assertEqual(summary.entries, 0, name)

    def test_the_setup_never_overrides_no_data_high_volatility_or_sideways(self):
        from nifty_core.regime import engine as RE
        from nifty_core.strategy import engine as SE
        scenario = sc.scenario("bearish_trend")
        ctx = P.load_context(self.cfg, "fixture", "intraday")
        provider = FixtureProvider(scenario)
        t = P.tick_times(ctx, scenario.day)[40]
        base = RE.classify_at(provider, ctx.calendar, t, ctx.regime_params, ctx.data_mode, scenario.data_label)
        for label in (RE.NO_DATA, RE.HIGH_VOLATILITY, RE.SIDEWAYS):
            kept = SE.apply_intraday_setup(base._replace(label=label), provider, ctx.calendar, ctx.regime_params,
                                           ctx.setup_params, t)
            self.assertEqual(kept.label, label)
        decided = SE.apply_intraday_setup(base._replace(label=RE.NO_TRADE), provider, ctx.calendar, ctx.regime_params,
                                          ctx.setup_params, t)
        self.assertIn(decided.label, (RE.BULLISH, RE.BEARISH, RE.NO_TRADE))
        self.assertTrue(all(r.code.startswith("SETUP_") or r.code == "INTRADAY_SETUP" for r in decided.reasons))

    def test_a_setup_direction_still_goes_through_every_other_gate(self):
        """Even when the setup names a direction, stale quotes, the risk limit and your yes still decide the trade."""
        found = False
        for name in ("bullish_trend", "bearish_trend", "sideways"):
            summary, records = self.run_day(name)
            for r in records:
                if r["kind"] == "DECISION" and r["payload"]["regime"]["label"] in ("BULLISH", "BEARISH"):
                    found = True
                    self.assertIn(r["payload"]["status"], ("NO_TRADE", "BLOCKED", "AWAITING_APPROVAL"))
            self.assertEqual(summary.entries, 0)                        # no answers were supplied: nothing is entered
        self.assertTrue(found, "the fixtures should contain at least one setup-named direction")


if __name__ == "__main__":
    unittest.main()
