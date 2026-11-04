"""The config must refuse every route to live trading (PLAN.md section 6, guard 3).

Lives in tests/safety because it contains the forbidden words on purpose.
"""
import json
import unittest
from decimal import Decimal

from nifty_core import config
from nifty_core.errors import ConfigError, LiveTradingNotSupported
from tests._base import NoNetworkTestCase

CLEAN_ENV = {}


def raw(**overrides):
    with open(config.DEFAULT_PATH, encoding="utf-8") as fh:
        d = json.load(fh, parse_float=Decimal)
    d.update(overrides)
    return d


class PaperOnlyTests(NoNetworkTestCase):
    def test_paper_false_is_refused(self):
        with self.assertRaises(LiveTradingNotSupported):
            config.validate(raw(paper=False), CLEAN_ENV)

    def test_paper_must_be_literally_true(self):
        for bad in (1, "true", None, "yes"):
            with self.assertRaises(LiveTradingNotSupported, msg=repr(bad)):
                config.validate(raw(paper=bad), CLEAN_ENV)

    def test_missing_paper_key_is_refused(self):
        d = raw()
        del d["paper"]
        with self.assertRaises(ConfigError):
            config.validate(d, CLEAN_ENV)

    def test_live_or_broker_style_keys_are_refused(self):
        for key in ("live", "live_trading", "broker", "api_key", "API_SECRET", "access_token",
                    "password", "place_orders", "order_endpoint", "client_id", "real_money"):
            d = raw()
            d[key] = "x"
            with self.assertRaises(LiveTradingNotSupported, msg=key):
                config.validate(d, CLEAN_ENV)

    def test_environment_cannot_switch_paper_off(self):
        for env in ({"NIFTY_PAPER": "false"}, {"NIFTY_PAPER": "0"}, {"NIFTY_PAPER": ""},
                    {"NIFTY_LIVE": "1"}, {"NIFTY_LIVE_TRADING": "true"}):
            with self.assertRaises(LiveTradingNotSupported, msg=str(env)):
                config.validate(raw(), env)

    def test_environment_paper_true_is_fine(self):
        config.validate(raw(), {"NIFTY_PAPER": "true"})
        config.validate(raw(), {"NIFTY_LIVE": "0"})

    def test_averaging_and_overnight_cannot_be_enabled(self):
        with self.assertRaises(ConfigError):
            config.validate(raw(allow_averaging=True), CLEAN_ENV)
        with self.assertRaises(ConfigError):
            config.validate(raw(allow_overnight=True), CLEAN_ENV)

    def test_only_the_approved_risk_basis_is_accepted(self):
        for basis in ("stop_loss_distance", "net_debit_only", "", None):
            with self.assertRaises(ConfigError, msg=repr(basis)):
                config.validate(raw(risk_basis=basis), CLEAN_ENV)


if __name__ == "__main__":
    unittest.main()
