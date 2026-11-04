import unittest

from nifty_core.pricing import black76


class Black76Tests(unittest.TestCase):
    def test_known_at_the_money_value(self):
        # F = K = 100, 20% vol, 1 year, no rates: C = P = 100 * (2*N(0.1) - 1) = 7.9656
        self.assertAlmostEqual(black76.price(100, 100, 1.0, 0.2, "CE"), 7.9656, places=3)
        self.assertAlmostEqual(black76.price(100, 100, 1.0, 0.2, "PE"), 7.9656, places=3)

    def test_put_call_parity_with_zero_rates(self):
        for f, k, t, v in ((25000, 24800, 0.02, 0.13), (25000, 25500, 0.1, 0.2), (100, 90, 0.5, 0.3)):
            c = black76.price(f, k, t, v, "CE")
            p = black76.price(f, k, t, v, "PE")
            self.assertAlmostEqual(c - p, f - k, places=6)

    def test_expiry_and_zero_vol_give_intrinsic_value(self):
        self.assertEqual(black76.price(100, 90, 0, 0.2, "CE"), 10)
        self.assertEqual(black76.price(100, 90, 0, 0.2, "PE"), 0)
        self.assertEqual(black76.price(100, 110, 1, 0, "PE"), 10)

    def test_monotonic_in_vol_and_strike(self):
        self.assertGreater(black76.price(100, 100, 0.1, 0.3, "CE"), black76.price(100, 100, 0.1, 0.2, "CE"))
        calls = [black76.price(100, k, 0.1, 0.2, "CE") for k in (90, 95, 100, 105, 110)]
        self.assertEqual(calls, sorted(calls, reverse=True))
        puts = [black76.price(100, k, 0.1, 0.2, "PE") for k in (90, 95, 100, 105, 110)]
        self.assertEqual(puts, sorted(puts))

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            black76.price(100, 100, 1, 0.2, "XX")
        with self.assertRaises(ValueError):
            black76.price(0, 100, 1, 0.2, "CE")


if __name__ == "__main__":
    unittest.main()
