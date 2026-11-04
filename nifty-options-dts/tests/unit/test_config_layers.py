"""Verified exchange facts, cost rules and our strategy choices stay in separate places."""
import json
import os
import re
import unittest

from nifty_core import config
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXCHANGE_OR_COST_FRAGMENTS = ("lot_size", "expiry", "tick", "strike_interval", "stt", "brokerage", "gst",
                              "txn", "stamp", "session_open", "session_close", "holiday", "sebi", "ipft")


class ConfigLayerTests(NoNetworkTestCase):
    def test_every_config_key_belongs_to_exactly_one_group(self):
        grouped = [k for keys in config.KEY_GROUPS.values() for k in keys]
        self.assertEqual(sorted(grouped), sorted(config.KNOWN_KEYS))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_config_holds_no_exchange_or_cost_facts(self):
        for key in config.KNOWN_KEYS:
            for fragment in EXCHANGE_OR_COST_FRAGMENTS:
                self.assertNotIn(fragment, key, key)

    def test_entry_window_and_flat_by_are_strategy_assumptions(self):
        strategy = config.KEY_GROUPS["strategy_assumptions"]
        for key in ("entry_window_start_ist", "entry_window_end_ist", "flat_by_ist"):
            self.assertIn(key, strategy)

    def test_the_contract_layer_holds_no_strategy_choices(self):
        with open(os.path.join(ROOT, "config", "contracts", "nifty_contract_specs.json"), encoding="utf-8") as fh:
            items = json.load(fh)["items"]
        self.assertEqual(sorted(items), ["expiry_holiday_rule", "lot_size", "session", "strike_interval", "tick_size",
                                         "weekly_expiry_weekday"])
        text = json.dumps(items).lower()
        for word in ("flat_by", "entry_window", "risk_per_trade", "slippage"):
            self.assertNotIn(word, text)

    def test_cost_files_hold_no_strategy_choices(self):
        for path in (os.path.join(ROOT, "config", "costs", "statutory_charges.json"),
                     os.path.join(ROOT, "config", "costs", "brokers", "zerodha.json")):
            with open(path, encoding="utf-8") as fh:
                text = fh.read().lower()
            for word in ("flat_by", "entry_window", "risk_per_trade", "slippage_points", "max_lots"):
                self.assertNotIn(word, text, path)

    def test_the_example_config_has_only_known_keys(self):
        with open(os.path.join(ROOT, "config", "config.example.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)), sorted(config.KNOWN_KEYS))


if __name__ == "__main__":
    unittest.main()
