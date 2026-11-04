"""The read-only recorder, tested with a fake transport: no network, no real token."""
import csv
import datetime as dt
import json
import os
import tempfile
import unittest

from nifty_live import recorder, upstox_reader as ur
from tests._base import NoNetworkTestCase

TOKEN = "TESTTOKEN-not-a-real-secret"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def leg(key, bid, ask):
    return {"instrument_key": key, "market_data": {"ltp": 100.5, "volume": 10, "oi": 500, "bid_price": bid,
                                                     "bid_qty": 75 if bid else 0, "ask_price": ask, "ask_qty": 150}}


CHAIN = {"status": "success", "data": [
    {"expiry": "2026-11-10", "strike_price": 25000.0, "underlying_spot_price": 25055.3,
     "call_options": leg("NSE_FO|1", 150.1, 151.0), "put_options": leg("NSE_FO|2", 0, 40.0)},
    {"expiry": "2026-11-10", "strike_price": 27000.0, "underlying_spot_price": 25055.3,
     "call_options": leg("NSE_FO|3", 1.0, 1.1), "put_options": leg("NSE_FO|4", 1.0, 1.1)}]}
CANDLES = {"status": "success", "data": {"candles": [
    ["2026-11-05T09:20:00+05:30", 25056.0, 25060.0, 25050.0, 25058.0, 0, 0],
    ["2026-11-05T09:15:00+05:30", 25050.0, 25060.1, 25040.0, 25055.3, 0, 0],
    ["2026-11-05T09:25:00+05:30", 25058.0, 25062.0, 25055.0, 25061.0, 0, 0]]}}


class FakeTransport(object):
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        doc = CHAIN if "/option/chain" in url else CANDLES
        return self.status, json.dumps(doc).encode()


class ReaderTests(NoNetworkTestCase):
    def test_chain_rows_keep_missing_quotes_missing(self):
        spot, rows = ur.get_option_chain(TOKEN, "current_week", transport=FakeTransport())
        self.assertEqual(spot, "25055.3")
        put = [r for r in rows if r["strike"] == 25000 and r["type"] == "PE"][0]
        self.assertEqual(put["bid"], "")             # a zero bid is no quote, never a price of 0
        self.assertEqual(put["bid_qty"], "")
        call = [r for r in rows if r["strike"] == 25000 and r["type"] == "CE"][0]
        self.assertEqual((call["bid"], call["ask"], call["bid_qty"]), ("150.1", "151.0", "75"))

    def test_requests_are_get_only_https_to_one_host_with_the_token_in_a_header(self):
        t = FakeTransport()
        ur.get_option_chain(TOKEN, transport=t)
        ur.get_candles(TOKEN, transport=t)
        for url, headers in t.calls:
            self.assertTrue(url.startswith("https://api.upstox.com/"), url)
            self.assertNotIn(TOKEN, url)
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertIn("NSE_INDEX%7CNifty%2050", t.calls[1][0])

    def test_other_paths_are_refused_before_any_request(self):
        t = FakeTransport()
        with self.assertRaises(ur.UpstoxReadError):
            ur._get_json("/v2/portfolio/long-term-holdings", None, TOKEN, t)
        self.assertEqual(t.calls, [])

    def test_errors_never_contain_the_token(self):
        def bad(url, headers, timeout):
            return 401, ("invalid " + TOKEN).encode()
        with self.assertRaises(ur.UpstoxReadError) as cm:
            ur.get_option_chain(TOKEN, transport=bad)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(cm.exception.status, 401)

    def test_missing_token_is_reported(self):
        with self.assertRaises(ur.UpstoxReadError):
            ur.token_from_env({})


class RecorderTests(NoNetworkTestCase):
    def test_snapshot_candles_and_scenario(self):
        t0 = dt.datetime(2026, 11, 5, 9, 31, tzinfo=IST)
        with tempfile.TemporaryDirectory() as d:
            n = recorder.record_snapshot(TOKEN, d, t0, "current_week", 1000, FakeTransport())
            self.assertEqual(n, 2)                    # the 27000 strike is outside +-1000 of spot
            recorder.record_snapshot(TOKEN, d, t0 + dt.timedelta(seconds=30), "current_week", 1000, FakeTransport())
            with open(os.path.join(d, "chain.csv"), encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["snapshot_time"], "2026-11-05T09:31:00+05:30")
            # candles: sorted, and the one still forming at 09:27 is left out
            c = recorder.write_candles(TOKEN, d, dt.datetime(2026, 11, 5, 9, 27, tzinfo=IST), FakeTransport())
            self.assertEqual(c, 2)
            doc = recorder.build_scenario(d, "2026-11-05", os.path.join(d, "out", "s.json"))
            self.assertEqual(doc["data_label"], "RECORDED_REAL")
            self.assertEqual(len(doc["chains"]), 2)
            self.assertEqual([x["start"][11:16] for x in doc["candles"]], ["09:15", "09:20"])

    def test_auto_expiry_skips_empty_keywords_and_returns_an_explicit_date(self):
        empty = {"status": "success", "data": []}

        def transport(url, headers, timeout):
            doc = CHAIN if "next_week" in url else empty
            return 200, json.dumps(doc).encode()
        self.assertEqual(recorder.resolve_expiry(TOKEN, "auto", transport), "2026-11-10")
        self.assertEqual(recorder.resolve_expiry(TOKEN, "2026-11-12", transport), "2026-11-12")
        with self.assertRaises(ur.UpstoxReadError):
            recorder.resolve_expiry(TOKEN, "current_month", transport)

    def test_session_window(self):
        self.assertTrue(recorder.in_session(dt.datetime(2026, 11, 5, 10, 0, tzinfo=IST)))      # Thursday
        self.assertFalse(recorder.in_session(dt.datetime(2026, 11, 5, 16, 0, tzinfo=IST)))
        self.assertFalse(recorder.in_session(dt.datetime(2026, 11, 7, 10, 0, tzinfo=IST)))     # Saturday


if __name__ == "__main__":
    unittest.main()
