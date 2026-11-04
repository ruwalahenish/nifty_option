import datetime
import os
import unittest

from nifty_core.errors import ContractError, RateNotCovered
from nifty_core.versioned import ROOT, VersionedItem, VersionedTable, load_sources
from tests._base import NoNetworkTestCase

D = datetime.date
SRC = load_sources()
SID = "NSE_FAOP_70616"


def v(value, ef=None, et=None, status="VERIFIED_PRIMARY", verified="2026-11-04", sources=(SID,)):
    return {"value": value, "effective_from": ef, "effective_to": et, "status": status,
            "sources": list(sources), "verified_on": verified}


def item(*versions):
    return VersionedItem("x", {"versions": list(versions)}, SRC)


class ResolveTests(NoNetworkTestCase):
    def test_dated_versions_pick_the_one_in_force(self):
        it = item(v(1, "2024-01-01", "2024-12-31"), v(2, "2025-01-01"))
        self.assertEqual(it.resolve(D(2024, 12, 31), "real_readonly").value, 1)
        self.assertEqual(it.resolve(D(2025, 1, 1), "real_readonly").value, 2)
        with self.assertRaises(RateNotCovered):
            it.resolve(D(2023, 12, 31), "real_readonly")

    def test_unknown_start_is_trusted_only_from_verification_date(self):
        it = item(v(5, None, None))                                     # verified 2026-11-04
        self.assertEqual(it.resolve(D(2026, 11, 4), "real_readonly").value, 5)
        with self.assertRaises(RateNotCovered):
            it.resolve(D(2026, 11, 3), "real_readonly")
        r = it.resolve(D(2020, 1, 1), "fixture")                        # allowed, but flagged
        self.assertTrue(r.extrapolated)
        self.assertTrue(any("start date unknown" in w for w in r.warnings))

    def test_unverified_is_fixture_only(self):
        it = item(v(7, None, "2030-01-01", status="UNVERIFIED", verified=None, sources=()))
        r = it.resolve(D(2026, 1, 1), "fixture")
        self.assertEqual(r.status, "UNVERIFIED")
        self.assertTrue(r.warnings)
        with self.assertRaises(ContractError):                          # never silently used
            it.resolve(D(2026, 1, 1), "real_readonly")

    def test_unverified_with_a_known_start_is_refused_outside_fixture(self):
        it = item(v(7, "2026-01-01", None, status="UNVERIFIED", verified=None, sources=()))
        with self.assertRaises(ContractError):
            it.resolve(D(2026, 6, 1), "real_readonly")
        self.assertEqual(it.resolve(D(2026, 6, 1), "fixture").value, 7)


class ValidationTests(NoNetworkTestCase):
    def test_overlap_is_rejected(self):
        with self.assertRaises(ContractError):
            item(v(1, "2024-01-01", "2025-06-30"), v(2, "2025-06-30"))

    def test_known_end_with_unknown_start_does_not_overlap_a_later_version(self):
        item(v(1, None, "2026-02-28"), v(2, "2026-03-01"))

    def test_verified_needs_sources_and_verification_date(self):
        with self.assertRaises(ContractError):
            item(v(1, sources=()))
        with self.assertRaises(ContractError):
            item(v(1, verified=None))
        with self.assertRaises(ContractError):
            item(v(1, sources=("NO_SUCH_SOURCE",)))

    def test_bad_status_dates_and_empty_lists(self):
        for bad in (v(1, status="MAYBE"), v(1, ef="2025-02-30"), v(1, ef="2025-02-01", et="2025-01-01")):
            with self.assertRaises(ContractError):
                item(bad)
        with self.assertRaises(ContractError):
            VersionedItem("x", {"versions": []}, SRC)
        with self.assertRaises(ContractError):
            VersionedTable({"items": {}}, SRC)

    def test_every_source_in_the_registry_is_well_formed_and_has_evidence(self):
        for sid, s in SRC.items():
            self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$", sid)
            self.assertTrue(s["url"].startswith("https://"), sid)
            if s.get("evidence"):
                self.assertTrue(os.path.exists(os.path.join(ROOT, s["evidence"])), sid)


if __name__ == "__main__":
    unittest.main()
