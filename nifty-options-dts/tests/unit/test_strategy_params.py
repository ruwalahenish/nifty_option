import json
import unittest
from decimal import Decimal as Dec

from nifty_core.errors import ConfigError
from nifty_core.strategy import params as P
from nifty_core.versioned import load_json
from tests._base import NoNetworkTestCase


def values(**override):
    v = dict(load_json(P.DEFAULT_PATH)["values"])
    v.update(override)
    return v


class StrategyParamsTests(NoNetworkTestCase):
    def test_default_file_loads_with_the_documented_values(self):
        p = P.load_params()
        self.assertEqual(p.min_days_to_expiry, 2)
        self.assertEqual(p.long_strike_offsets_steps, (0, 1))
        self.assertEqual(p.width_points_options, (100, 150))
        self.assertEqual(p.candidate_lots, 1)
        self.assertEqual(p.max_leg_spread_pct, Dec("3.0"))
        self.assertEqual((p.min_leg_volume, p.min_leg_open_interest, p.min_depth_lots), (5000, 20000, 1))
        self.assertEqual(p.max_debit_to_width, Dec("0.65"))
        self.assertEqual(p.max_spot_divergence_pct, Dec("0.25"))
        self.assertIs(p.block_on_high_impact_event, True)

    def test_file_is_declared_as_unvalidated_strategy_assumptions(self):
        doc = load_json(P.DEFAULT_PATH)
        self.assertEqual(doc["kind"], "STRATEGY_ASSUMPTIONS")
        self.assertIn("NOT validated", doc["description"])

    def test_file_holds_no_exchange_cost_risk_or_regime_values(self):
        text = json.dumps(load_json(P.DEFAULT_PATH), default=str).lower()
        for word in ("lot_size", "tick", "stt", "brokerage", "holiday", "session", "capital", "risk_per_trade",
                     "max_daily_loss", "slippage", "stop", "net_move", "votes"):
            self.assertNotIn(word, text, word)

    def test_every_threshold_is_in_the_file_and_nothing_else(self):
        self.assertEqual(sorted(load_json(P.DEFAULT_PATH)["values"]), sorted(P.FIELDS))

    def test_fingerprint_is_stable_and_changes_with_any_value(self):
        a = P.load_params()
        self.assertEqual(a.fingerprint, P.load_params().fingerprint)
        for name, new in (("max_leg_spread_pct", Dec("3.1")), ("width_points_options", [100]),
                          ("long_strike_offsets_steps", [0]), ("block_on_high_impact_event", False),
                          ("min_leg_volume", 5001)):
            self.assertNotEqual(P.build_params(values(**{name: new})).fingerprint, a.fingerprint, name)

    def test_bad_values_are_rejected(self):
        bad = (dict(min_days_to_expiry=0), dict(candidate_lots=0), dict(candidate_lots=True), dict(min_depth_lots=0),
               dict(min_leg_volume=-1), dict(min_leg_open_interest=1.5), dict(max_leg_spread_pct=Dec("0")),
               dict(max_leg_spread_pct="3"), dict(max_debit_to_width=Dec("1")), dict(max_debit_to_width=Dec("1.2")),
               dict(max_spot_divergence_pct=Dec("-1")), dict(block_on_high_impact_event="yes"),
               dict(long_strike_offsets_steps=[]), dict(long_strike_offsets_steps=[-1]),
               dict(long_strike_offsets_steps=[0, 0]), dict(long_strike_offsets_steps=[1.5]),
               dict(long_strike_offsets_steps=0), dict(width_points_options=[]), dict(width_points_options=[0]),
               dict(width_points_options=[100, 100]), dict(width_points_options=[Dec("50.5")]))
        for override in bad:
            with self.assertRaises(ConfigError, msg=str(override)):
                P.build_params(values(**override))

    def test_zero_minimum_volume_and_open_interest_are_allowed(self):
        p = P.build_params(values(min_leg_volume=0, min_leg_open_interest=0))
        self.assertEqual((p.min_leg_volume, p.min_leg_open_interest), (0, 0))

    def test_unknown_and_missing_names_are_rejected(self):
        v = values()
        v["surprise"] = 1
        with self.assertRaises(ConfigError):
            P.build_params(v)
        v = values()
        del v["max_debit_to_width"]
        with self.assertRaises(ConfigError):
            P.build_params(v)

    def test_lists_are_sorted_and_immutable(self):
        p = P.build_params(values(width_points_options=[150, 100], long_strike_offsets_steps=[1, 0]))
        self.assertEqual((p.width_points_options, p.long_strike_offsets_steps), ((100, 150), (0, 1)))


if __name__ == "__main__":
    unittest.main()
