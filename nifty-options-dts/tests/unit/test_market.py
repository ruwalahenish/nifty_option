import datetime
import unittest

from nifty_core import config as config_mod
from nifty_core.errors import CalendarNotCovered, ConfigError, ContractError, RateNotCovered
from nifty_core.market import calendar_nse, expiry, ist
from nifty_core.contracts import ContractSpecs
from tests._base import NoNetworkTestCase

D = datetime.date
T = datetime.time
REAL = "real_readonly"

# NSE/FAOP/71777 (F&O trading holidays 2026), typed here independently of the data file
HOLIDAYS_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
                 "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
                 "2026-11-10", "2026-11-24", "2026-12-25"]


class IstTests(NoNetworkTestCase):
    def test_ist_is_utc_plus_5_30_without_dst(self):
        for month in (1, 6, 12):
            m = ist.ist_datetime(D(2026, month, 15), T(9, 15))
            self.assertEqual(m.utcoffset(), datetime.timedelta(hours=5, minutes=30))
        utc = datetime.datetime(2026, 11, 4, 3, 45, tzinfo=ist.UTC)
        self.assertEqual(ist.to_ist(utc), ist.ist_datetime(D(2026, 11, 4), T(9, 15)))

    def test_naive_datetimes_are_refused(self):
        with self.assertRaises(ValueError):
            ist.to_ist(datetime.datetime(2026, 11, 4, 9, 15))

    def test_parse_and_date_around_midnight(self):
        m = ist.parse_ist("2026-11-04 09:15")
        self.assertEqual(m.hour, 9)
        late = datetime.datetime(2026, 11, 4, 20, 0, tzinfo=ist.UTC)         # 01:30 next day in IST
        self.assertEqual(ist.ist_date(late), D(2026, 11, 5))
        with self.assertRaises(ValueError):
            ist.parse_ist("04/11/2026 9:15")

    def test_clocks(self):
        c = ist.FixedClock(ist.parse_ist("2026-11-04 10:00"))
        self.assertEqual(c.now().hour, 10)
        c.set(ist.parse_ist("2026-11-04 11:30"))
        self.assertEqual(c.now().minute, 30)
        self.assertEqual(ist.SystemClock().now().utcoffset(), datetime.timedelta(hours=5, minutes=30))


class CalendarTests(NoNetworkTestCase):
    def setUp(self):
        super(CalendarTests, self).setUp()
        self.specs = ContractSpecs.load()
        self.cal = calendar_nse.NseCalendar.load(specs=self.specs, data_mode=REAL)
        self.fixture_cal = calendar_nse.NseCalendar.load(specs=self.specs, data_mode="fixture")

    def test_all_fifteen_published_weekday_holidays_are_closed(self):
        for text in HOLIDAYS_2026:
            d = D.fromisoformat(text)
            self.assertFalse(self.cal.is_trading_day(d), text)
            self.assertIsNone(self.cal.session(d), text)
            self.assertNotEqual(self.cal.holiday_reason(d), "weekend")

    def test_ordinary_days_and_weekends(self):
        self.assertTrue(self.cal.is_trading_day(D(2026, 11, 4)))              # Wednesday
        self.assertEqual(self.cal.holiday_reason(D(2026, 11, 7)), "weekend")  # Saturday
        self.assertEqual(self.cal.holiday_reason(D(2026, 11, 1)), "weekend")  # Sunday
        self.assertFalse(self.cal.is_trading_day(D(2026, 2, 15)))             # Mahashivratri, Sunday

    def test_muhurat_session_on_a_sunday(self):
        s = self.cal.session(D(2026, 11, 8))
        self.assertEqual(s.kind, "MUHURAT")
        self.assertEqual((s.pre_open_start, s.open, s.close), (T(14, 5), T(14, 20), T(15, 40)))
        self.assertTrue(self.cal.is_trading_day(D(2026, 11, 8)))

    def test_regular_session_times_and_bar_counts(self):
        s = self.cal.session(D(2026, 11, 4))
        self.assertEqual((s.kind, s.open, s.close), ("REGULAR", T(9, 15), T(15, 40)))
        self.assertEqual(self.cal.bars_in_session(D(2026, 11, 4)), 77)         # 385 minutes / 5
        self.assertEqual(self.cal.bars_in_session(D(2026, 11, 8)), 16)         # 14:20 to 15:40
        self.assertEqual(self.cal.bars_in_session(D(2026, 11, 10)), 0)         # holiday
        self.assertEqual(self.fixture_cal.bars_in_session(D(2026, 7, 31)), 75)  # pre-CAS close 15:30 (unverified)

    def test_pre_cas_session_is_refused_outside_fixture_mode(self):
        with self.assertRaises(ContractError):
            self.cal.session(D(2026, 7, 31))
        s = self.fixture_cal.session(D(2026, 7, 31))
        self.assertTrue(any("UNVERIFIED" in w for w in s.warnings))

    def test_uncovered_year_is_refused_outside_fixture_mode_and_flagged_in_fixture_mode(self):
        for fn in (self.cal.is_trading_day, self.cal.session, self.cal.holiday_reason):
            with self.assertRaises(CalendarNotCovered):
                fn(D(2027, 3, 3))
        s = self.fixture_cal.session(D(2027, 3, 3))                           # fixture fallback, flagged
        self.assertTrue(s.assumed)
        self.assertTrue(any("not covered" in w for w in s.warnings))
        self.assertTrue(self.fixture_cal.is_trading_day(D(2027, 3, 3)))
        self.assertFalse(self.fixture_cal.is_trading_day(D(2027, 3, 6)))       # still a Saturday

    def test_previous_and_next_trading_day_skip_holidays_and_weekends(self):
        self.assertEqual(self.cal.previous_trading_day(D(2026, 11, 11)), D(2026, 11, 9))   # Nov 10 holiday
        self.assertEqual(self.cal.previous_trading_day(D(2026, 11, 9)), D(2026, 11, 8))    # Muhurat Sunday
        self.assertEqual(self.cal.next_trading_day(D(2026, 11, 6)), D(2026, 11, 8))
        self.assertEqual(self.cal.next_trading_day(D(2026, 12, 24)), D(2026, 12, 28))      # Dec 25, weekend

    def test_config_must_fit_the_session(self):
        cfg = config_mod.load_config(env={})
        calendar_nse.check_config_fits_session(cfg, self.cal.session(D(2026, 11, 4)))
        with self.assertRaises(ConfigError):                                   # Muhurat opens 14:20
            calendar_nse.check_config_fits_session(cfg, self.cal.session(D(2026, 11, 8)))
        with self.assertRaises(ConfigError):
            calendar_nse.check_config_fits_session(cfg, None)
        early = self.fixture_cal.session(D(2026, 7, 31))                       # closes 15:30; flat_by 15:15 fits
        calendar_nse.check_config_fits_session(cfg, early)
        late = cfg._replace(flat_by_ist="15:35")
        with self.assertRaises(ConfigError):
            calendar_nse.check_config_fits_session(late, early)

    def test_bad_calendar_data_is_rejected(self):
        doc = {"schema_version": 1, "calendars": {"2026": {
            "status": "VERIFIED_PRIMARY", "sources": ["NSE_FAOP_71777"], "verified_on": "2026-11-04",
            "trading_holidays": [{"date": "2027-01-26", "description": "wrong year"}]}}}
        with self.assertRaises(CalendarNotCovered):
            calendar_nse.NseCalendar(doc, self.specs)
        doc["calendars"]["2026"]["trading_holidays"] = []
        doc["calendars"]["2026"]["sources"] = []
        with self.assertRaises(CalendarNotCovered):
            calendar_nse.NseCalendar(doc, self.specs)


class ExpiryTests(NoNetworkTestCase):
    def setUp(self):
        super(ExpiryTests, self).setUp()
        self.specs = ContractSpecs.load()
        self.cal = calendar_nse.NseCalendar.load(specs=self.specs, data_mode=REAL)
        self.fcal = calendar_nse.NseCalendar.load(specs=self.specs, data_mode="fixture")

    def weekly(self, text, mode=REAL):
        cal = self.cal if mode == REAL else self.fcal
        return expiry.weekly_expiry(D.fromisoformat(text), cal, self.specs, mode)

    def test_ordinary_weekly_expiry_is_tuesday(self):
        e = self.weekly("2026-11-18")                       # Wednesday; its week's Tuesday is Nov 17
        self.assertEqual((e.date, e.shifted, e.kind), (D(2026, 11, 17), False, "WEEKLY"))
        self.assertEqual(e.date.weekday(), 1)
        self.assertEqual(self.weekly("2026-11-17").date, D(2026, 11, 17))

    def test_holiday_tuesday_moves_to_the_previous_trading_day(self):
        e = self.weekly("2026-11-10")                       # Diwali-Balipratipada
        self.assertEqual((e.nominal, e.date, e.shifted), (D(2026, 11, 10), D(2026, 11, 9), True))
        self.assertEqual(self.weekly("2026-11-24").date, D(2026, 11, 23))
        # earlier 2026 holidays, in fixture mode (their rule start dates are not verified)
        self.assertEqual(self.weekly("2026-10-20", "fixture").date, D(2026, 10, 19))      # Dussehra
        self.assertEqual(self.weekly("2026-04-14", "fixture").date, D(2026, 4, 13))       # Ambedkar Jayanti
        self.assertEqual(self.weekly("2026-03-03", "fixture").date, D(2026, 3, 2))        # Holi

    def test_monthly_expiry_is_the_last_tuesday_moved_for_holidays(self):
        real = lambda y, mo: expiry.monthly_expiry(y, mo, self.cal, self.specs, REAL)
        fix = lambda y, mo: expiry.monthly_expiry(y, mo, self.fcal, self.specs, "fixture")
        self.assertEqual(real(2026, 11).date, D(2026, 11, 23))     # Nov 24 holiday
        self.assertEqual(real(2026, 12).date, D(2026, 12, 29))
        self.assertEqual(fix(2026, 10).date, D(2026, 10, 27))
        self.assertEqual(fix(2026, 3).date, D(2026, 3, 30))        # Mar 31 holiday
        self.assertTrue(fix(2026, 3).shifted)
        self.assertFalse(fix(2026, 10).shifted)

    def test_next_weekly_expiry(self):
        n = lambda text: expiry.next_weekly_expiry(D.fromisoformat(text), self.cal, self.specs, REAL)
        self.assertEqual(n("2026-11-04").date, D(2026, 11, 9))     # the shifted one
        self.assertEqual(n("2026-11-09").date, D(2026, 11, 9))     # expiry day counts
        self.assertEqual(n("2026-11-10").date, D(2026, 11, 17))    # holiday itself: the next week

    def test_is_expiry_day(self):
        f = lambda text: expiry.is_expiry_day(D.fromisoformat(text), self.cal, self.specs, REAL)
        self.assertTrue(f("2026-11-09"))
        self.assertTrue(f("2026-11-17"))
        self.assertFalse(f("2026-11-18"))
        self.assertFalse(f("2026-11-10"))                          # the nominal day is a holiday now
        self.assertTrue(f("2026-11-23"))                           # shifted weekly and monthly

    def test_2025_rule_change_in_fixture_mode_matches_the_nse_annexure(self):
        # NSE/FAOP/68685 annexure: weekly 04-Sep-25 -> 09-Sep-25; monthly 25-Sep-25 -> 30-Sep-25
        self.assertEqual(self.weekly("2025-08-27", "fixture").date, D(2025, 8, 28))    # last Thursday-rule week
        with self.assertRaises(RateNotCovered):
            self.weekly("2025-09-03", "fixture")                                       # no expiry that week
        self.assertEqual(self.weekly("2025-09-10", "fixture").date, D(2025, 9, 9))
        self.assertEqual(expiry.monthly_expiry(2025, 8, self.fcal, self.specs, "fixture").date, D(2025, 8, 28))
        self.assertEqual(expiry.monthly_expiry(2025, 9, self.fcal, self.specs, "fixture").date, D(2025, 9, 30))
        # 2025 is not in the verified calendar and the old rule's start date is unknown: both are flagged
        e = self.weekly("2025-09-10", "fixture")
        self.assertTrue(e.warnings)

    def test_old_dates_are_refused_outside_fixture_mode(self):
        with self.assertRaises((CalendarNotCovered, ContractError)):
            expiry.weekly_expiry(D(2025, 9, 10), self.cal, self.specs, REAL)

    def test_uncovered_future_year_is_refused_for_real_use(self):
        with self.assertRaises(CalendarNotCovered):
            expiry.weekly_expiry(D(2027, 3, 3), self.cal, self.specs, REAL)
        e = expiry.weekly_expiry(D(2027, 3, 3), self.fcal, self.specs, "fixture")      # fixture fallback
        self.assertEqual(e.date, D(2027, 3, 2))


if __name__ == "__main__":
    unittest.main()
