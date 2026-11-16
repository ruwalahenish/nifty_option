"""Early exits for selector positions: each structure leaves when the reason it was entered for is gone."""
import types
import unittest
from decimal import Decimal as D

from nifty_core.market.ist import parse_ist
from nifty_core.selector import exits as X
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase

DAY = "2026-11-05"


def candles(closes):
    return H.candles_from(DAY, [D(str(c)) for c in closes], wick=D("1"))


def at_end(cs):
    return cs[-1].end


def plan(rule, **extra):
    return types.SimpleNamespace(strategy_type="X", exit_plan={"invalidate": dict(rule=rule, **extra)})


FLAT = [24450, 24452, 24449, 24451, 24450, 24452, 24451]


class CreditSpreadExitTests(NoNetworkTestCase):
    def test_a_bull_put_spread_leaves_when_a_candle_closes_below_its_level(self):
        c = plan("close_below", level="24450")
        cs = candles(FLAT + [24449])
        self.assertIn("below the key level 24,450.00", X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(FLAT + [24451])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))

    def test_a_bear_call_spread_leaves_when_a_candle_closes_above_its_level(self):
        c = plan("close_above", level="24450")
        cs = candles(FLAT + [24451])
        self.assertIn("above the key level", X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(FLAT + [24449])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))

    def test_the_level_is_not_breached_by_a_wick_only_a_close_counts(self):
        c = plan("close_below", level="24450")
        cs = candles(FLAT + [24452])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))            # the candle's low is 24451 wick, close is above


class RangeStructureExitTests(NoNetworkTestCase):
    RULE = dict(low="24400", high="24500", short_put="24300", short_call="24600", buffer_points="50")

    def test_a_close_outside_the_day_range_means_the_range_has_broken(self):
        c = plan("outside_range", **self.RULE)
        for last, word in ((24501, "outside the day's range"), (24399, "outside the day's range")):
            cs = candles(FLAT + [last])
            self.assertIn(word, X.early_exit(cs, at_end(cs), c, H.PARAMS), last)

    def test_a_close_within_one_strike_interval_of_a_short_strike_leaves(self):
        c = plan("outside_range", **dict(self.RULE, low="24200", high="24700"))
        cs = candles(FLAT + [24352])                                            # 52 from the short put: fine
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(FLAT + [24349])                                            # 49 from the short put
        self.assertIn("short put strike", X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(FLAT + [24551])
        self.assertIn("short call strike", X.early_exit(cs, at_end(cs), c, H.PARAMS))

    def test_a_quiet_range_is_held(self):
        c = plan("outside_range", **self.RULE)
        cs = candles(FLAT + [24450])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))


class DebitSpreadExitTests(NoNetworkTestCase):
    UP = [24400, 24410, 24420, 24430, 24440, 24450, 24460, 24470]

    def test_a_bull_call_spread_leaves_when_a_candle_closes_back_below_the_day_average(self):
        c = types.SimpleNamespace(strategy_type="BULL_CALL_DEBIT")
        cs = candles(self.UP + [24380])
        self.assertIn("back below the day average", X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(self.UP + [24475])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))

    def test_a_bear_put_spread_is_the_mirror_image(self):
        c = types.SimpleNamespace(strategy_type="BEAR_PUT_DEBIT")
        down = [24470, 24460, 24450, 24440, 24430, 24420, 24410, 24400]
        cs = candles(down + [24490])
        self.assertIn("back above the day average", X.early_exit(cs, at_end(cs), c, H.PARAMS))
        cs = candles(down + [24395])
        self.assertIsNone(X.early_exit(cs, at_end(cs), c, H.PARAMS))


class NoVerdictTests(NoNetworkTestCase):
    def test_no_candles_or_too_few_candles_give_no_verdict(self):
        bull = types.SimpleNamespace(strategy_type="BULL_CALL_DEBIT")
        self.assertIsNone(X.early_exit([], parse_ist("%s 12:00" % DAY), bull, H.PARAMS))
        cs = candles([24400, 24390, 24380])
        self.assertIsNone(X.early_exit(cs, at_end(cs), bull, H.PARAMS))         # fewer candles than the selector needs

    def test_a_candle_still_forming_is_never_used(self):
        c = plan("close_below", level="24450")
        cs = candles(FLAT + [24400])
        self.assertIsNone(X.early_exit(cs, cs[-1].start + (cs[-1].end - cs[-1].start) / 2, c, H.PARAMS))

    def test_an_unknown_strategy_has_no_rule(self):
        self.assertIsNone(X.early_exit(candles(FLAT), parse_ist("%s 12:00" % DAY), types.SimpleNamespace(strategy_type="OTHER"), H.PARAMS))


if __name__ == "__main__":
    unittest.main()
