import json
import os
import unittest
from decimal import Decimal as Dec

from nifty_core.errors import ConfigError
from nifty_core.regime import params as P
from nifty_core.versioned import load_json
from tests._base import NoNetworkTestCase


def values(**override):
    v = dict(load_json(P.DEFAULT_PATH)["values"])
    v.update(override)
    return v


class ParamsFileTests(NoNetworkTestCase):
    def test_default_file_loads_with_the_documented_values(self):
        p = P.load_params()
        self.assertEqual(p.interval_minutes, 5)
        self.assertEqual(p.min_bars, 12)
        self.assertEqual(p.max_data_lag_minutes, 10)
        self.assertEqual(p.net_move_pct, Dec("0.30"))
        self.assertEqual((p.range_position_high, p.range_position_low), (Dec("0.65"), Dec("0.35")))
        self.assertEqual((p.recent_window, p.recent_dominance), (6, 4))
        self.assertEqual((p.structure_window, p.votes_required), (12, 3))
        self.assertEqual(p.sideways_max_range_pct, Dec("0.45"))
        self.assertEqual(p.abnormal_bar_range_pct, Dec("0.50"))
        self.assertEqual(p.high_vol_stdev_pct, Dec("0.10"))

    def test_file_is_declared_as_strategy_assumptions_not_facts(self):
        doc = load_json(P.DEFAULT_PATH)
        self.assertEqual(doc["kind"], "STRATEGY_ASSUMPTIONS")
        self.assertIn("NOT validated", doc["description"])
        text = json.dumps(doc, default=str).lower()
        for fact_word in ("lot", "expiry", "tick", "stt", "brokerage", "holiday", "session_close"):
            self.assertNotIn(fact_word, text)

    def test_every_threshold_is_in_the_file_and_nothing_else(self):
        self.assertEqual(sorted(load_json(P.DEFAULT_PATH)["values"]), sorted(P.FIELDS))

    def test_fingerprint_is_stable_and_changes_with_any_value(self):
        a, b = P.load_params(), P.load_params()
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(len(a.fingerprint), 16)
        for name, new in (("net_move_pct", Dec("0.31")), ("votes_required", 4), ("min_bars", 13)):
            self.assertNotEqual(P.build_params(values(**{name: new})).fingerprint, a.fingerprint, name)

    def test_bad_values_are_rejected(self):
        bad = (dict(interval_minutes=1), dict(min_bars=0), dict(min_bars=11), dict(structure_window=10),
               dict(structure_window=3), dict(recent_dominance=3), dict(recent_dominance=7),
               dict(range_position_low=Dec("0.7")), dict(range_position_high=Dec("1")),
               dict(votes_required=2), dict(votes_required=5), dict(net_move_pct=Dec("0")),
               dict(net_move_pct=Dec("-1")), dict(high_vol_stdev_pct="0.1"), dict(sideways_max_range_pct=True),
               dict(max_data_lag_minutes=1.5))
        for override in bad:
            with self.assertRaises(ConfigError, msg=str(override)):
                P.build_params(values(**override))

    def test_unknown_and_missing_names_are_rejected(self):
        v = values()
        v["surprise"] = 1
        with self.assertRaises(ConfigError):
            P.build_params(v)
        v = values()
        del v["votes_required"]
        with self.assertRaises(ConfigError):
            P.build_params(v)

    def test_wrong_file_kind_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "kind": "VERIFIED_FACTS", "values": {}}, fh)
            with self.assertRaises(ConfigError):
                P.load_params(path)


if __name__ == "__main__":
    unittest.main()
