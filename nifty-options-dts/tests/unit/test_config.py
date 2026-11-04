import json
import unittest
from decimal import Decimal

from nifty_core import config
from nifty_core.errors import ConfigError
from tests._base import NoNetworkTestCase

CLEAN_ENV = {}


def raw(**overrides):
    with open(config.DEFAULT_PATH, encoding="utf-8") as fh:
        d = json.load(fh, parse_float=Decimal)
    d.update(overrides)
    return d


class ApprovedDefaultsTests(NoNetworkTestCase):
    def test_example_config_matches_the_approved_decisions(self):
        c = config.load_config(env=CLEAN_ENV)
        self.assertEqual(c.account_capital_inr, Decimal(250000))
        self.assertEqual(c.risk_per_trade_inr, Decimal(2500))
        self.assertEqual(c.max_daily_loss_inr, Decimal(5000))
        self.assertEqual(c.max_lots, 1)
        self.assertEqual(c.max_trades_per_day, 2)
        self.assertEqual(c.risk_basis, "net_debit_plus_costs_plus_slippage")
        self.assertIs(c.paper, True)
        self.assertIs(c.allow_averaging, False)
        self.assertIs(c.allow_overnight, False)

    def test_config_is_immutable(self):
        c = config.validate(raw(), CLEAN_ENV)
        with self.assertRaises(AttributeError):
            setattr(c, "paper", not c.paper)


class ValidationTests(NoNetworkTestCase):
    def test_unknown_and_missing_keys(self):
        with self.assertRaises(ConfigError):
            config.validate(raw(surprise=1), CLEAN_ENV)
        d = raw()
        del d["max_lots"]
        with self.assertRaises(ConfigError):
            config.validate(d, CLEAN_ENV)

    def test_numbers(self):
        for key, bad in (("account_capital_inr", 0), ("account_capital_inr", -1),
                         ("risk_per_trade_pct", 0), ("risk_per_trade_pct", 101),
                         ("max_lots", 0), ("max_lots", 1.5), ("max_lots", True),
                         ("max_trades_per_day", 0), ("slippage_points_per_leg", -1),
                         ("account_capital_inr", "250000"), ("data_stale_after_seconds", 0)):
            with self.assertRaises(ConfigError, msg="%s=%r" % (key, bad)):
                config.validate(raw(**{key: bad}), CLEAN_ENV)

    def test_daily_loss_cannot_be_below_per_trade_risk(self):
        with self.assertRaises(ConfigError):
            config.validate(raw(risk_per_trade_pct=Decimal("3.0"), max_daily_loss_pct=Decimal("2.0")), CLEAN_ENV)

    def test_times_must_be_ordered_and_inside_the_session(self):
        for kw in ({"entry_window_start_ist": "09:00"},          # before the open
                   {"entry_window_end_ist": "09:30"},             # end before start
                   {"flat_by_ist": "14:00"},                      # flat-by before entry end
                   {"flat_by_ist": "15:45"},                      # after the close
                   {"flat_by_ist": "3pm"}):
            with self.assertRaises(ConfigError, msg=str(kw)):
                config.validate(raw(**kw), CLEAN_ENV)

    def test_data_mode(self):
        config.validate(raw(data_mode="real_readonly"), CLEAN_ENV)
        with self.assertRaises(ConfigError):
            config.validate(raw(data_mode="live"), CLEAN_ENV)

    def test_rupee_limits_are_exact_decimals(self):
        c = config.validate(raw(account_capital_inr=Decimal("123456.78"),
                                risk_per_trade_pct=Decimal("0.5")), CLEAN_ENV)
        self.assertEqual(c.risk_per_trade_inr, Decimal("617.2839"))


if __name__ == "__main__":
    unittest.main()
