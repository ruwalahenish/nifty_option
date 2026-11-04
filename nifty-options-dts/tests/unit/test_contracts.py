import datetime
import unittest

from nifty_core import contracts
from nifty_core.errors import ContractError, RateNotCovered
from nifty_core.versioned import load_json
from tests._base import NoNetworkTestCase

D = datetime.date
REAL = "real_readonly"


class ContractSpecsTests(NoNetworkTestCase):
    def setUp(self):
        super(ContractSpecsTests, self).setUp()
        self.specs = contracts.ContractSpecs.load()

    def test_lot_size_follows_nse_circular_176_2025(self):
        self.assertEqual(self.specs.lot_size(D(2025, 12, 30)).value, 75)    # last expiry on the old lot
        self.assertEqual(self.specs.lot_size(D(2025, 12, 23)).value, 75)
        self.assertEqual(self.specs.lot_size(D(2026, 1, 6)).value, 65)      # first expiry on the new lot
        self.assertEqual(self.specs.lot_size(D(2026, 11, 10)).value, 65)

    def test_lot_gap_between_circular_dates_is_an_error_not_a_guess(self):
        for d in (D(2025, 12, 31), D(2026, 1, 1), D(2026, 1, 5)):
            with self.assertRaises(RateNotCovered):
                self.specs.lot_size(d)

    def test_lot_size_before_the_circular_is_not_claimed(self):
        with self.assertRaises(RateNotCovered):
            self.specs.lot_size(D(2025, 6, 1))

    def test_expiry_weekday_changed_from_thursday_to_tuesday(self):
        self.assertEqual(self.specs.expiry_weekday(D(2025, 8, 28)).value, 3)
        self.assertEqual(self.specs.expiry_weekday(D(2025, 9, 9)).value, 1)
        self.assertEqual(self.specs.expiry_weekday(D(2026, 11, 10)).value, 1)
        with self.assertRaises(RateNotCovered):                              # the transition gap
            self.specs.expiry_weekday(D(2025, 9, 2))

    def test_tick_and_strike_interval(self):
        self.assertEqual(str(self.specs.tick_size(D(2026, 11, 4), REAL).value), "0.05")
        self.assertEqual(self.specs.strike_interval(D(2026, 11, 4), REAL).value, 50)

    def test_session_close_moved_to_15_40_on_2026_08_03(self):
        open_t, close_t, r = self.specs.session_times(D(2026, 8, 3), REAL)
        self.assertEqual((open_t, close_t), (datetime.time(9, 15), datetime.time(15, 40)))
        self.assertEqual(r.status, "VERIFIED_PRIMARY")
        open_t, close_t, r = self.specs.session_times(D(2026, 7, 31))       # fixture only
        self.assertEqual(close_t, datetime.time(15, 30))
        self.assertEqual(r.status, "UNVERIFIED")
        self.assertTrue(r.warnings)
        with self.assertRaises(ContractError):
            self.specs.session_times(D(2026, 7, 31), REAL)

    def test_verified_values_record_sources_and_dates(self):
        r = self.specs.lot_size(D(2026, 1, 6))
        self.assertEqual(r.status, "VERIFIED_PRIMARY")
        self.assertEqual(r.verified_on, D(2026, 11, 4))
        self.assertEqual(r.effective_from, D(2026, 1, 6))
        self.assertIn("NSE_FAOP_70616", r.sources)

    def test_report_for_a_current_date_is_fully_verified_in_real_mode(self):
        for name, status, problem, warns in self.specs.verification_report(D(2026, 11, 4), REAL):
            self.assertEqual(status, "VERIFIED_PRIMARY", name)
            self.assertIsNone(problem, name)

    def test_the_only_unverified_version_is_the_pre_cas_session(self):
        self.assertEqual(len(self.specs.unverified_versions()), 1)
        self.assertTrue(self.specs.unverified_versions()[0].startswith("session("))


class ContractValidationTests(NoNetworkTestCase):
    def doc(self):
        return load_json(contracts.DEFAULT_PATH)

    def test_bad_lot_values_are_rejected(self):
        for bad in (0, -5, 65.5, "65", True):
            d = self.doc()
            d["items"]["lot_size"]["versions"][1]["value"] = bad
            with self.assertRaises(ContractError, msg=repr(bad)):
                contracts.ContractSpecs(d)

    def test_wrong_underlying_schema_and_missing_item(self):
        for key, val in (("underlying", "BANKNIFTY"), ("schema_version", 1)):
            d = self.doc()
            d[key] = val
            with self.assertRaises(ContractError):
                contracts.ContractSpecs(d)
        d = self.doc()
        del d["items"]["tick_size"]
        with self.assertRaises(ContractError):
            contracts.ContractSpecs(d)

    def test_overlapping_lot_versions_are_rejected(self):
        d = self.doc()
        d["items"]["lot_size"]["versions"][0]["effective_to"] = "2026-02-01"
        with self.assertRaises(ContractError):
            contracts.ContractSpecs(d)


if __name__ == "__main__":
    unittest.main()
