import os
import tempfile
import unittest

import nifty_core
from nifty_core import config
from nifty_core.errors import KillSwitchActive, LiveTradingNotSupported
from nifty_core.guards import paper_only
from tests._base import NoNetworkTestCase


class PaperOnlyGuardTests(NoNetworkTestCase):
    def test_paper_only_constants_are_true(self):
        self.assertIs(nifty_core.PAPER_ONLY, True)
        self.assertIs(paper_only.PAPER_ONLY, True)

    def test_guard_accepts_a_paper_config(self):
        paper_only.assert_paper_only(config.load_config(env={}))
        paper_only.assert_paper_only(None)

    def test_guard_refuses_a_non_paper_object(self):
        class Fake(object):
            paper = False
        with self.assertRaises(LiveTradingNotSupported):
            paper_only.assert_paper_only(Fake())

    def test_guard_refuses_if_the_constant_is_tampered_with(self):
        original = paper_only.PAPER_ONLY
        try:
            paper_only.PAPER_ONLY = False
            with self.assertRaises(LiveTradingNotSupported):
                paper_only.assert_paper_only(None)
        finally:
            paper_only.PAPER_ONLY = original


class KillSwitchTests(NoNetworkTestCase):
    def test_kill_file_blocks_and_is_not_removed_by_the_package(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(paper_only.kill_switch_active(d))
            paper_only.assert_no_kill(d)
            paper_only.trip_kill_switch(d, "test")
            self.assertTrue(os.path.exists(os.path.join(d, "KILL")))
            with self.assertRaises(KillSwitchActive):
                paper_only.assert_no_kill(d)
            # tripping again, or checking, never clears it
            paper_only.trip_kill_switch(d, "again")
            with self.assertRaises(KillSwitchActive):
                paper_only.assert_no_kill(d)

    def test_kill_switch_creates_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "trader", "state")
            paper_only.trip_kill_switch(sub)
            self.assertTrue(paper_only.kill_switch_active(sub))


class NetworkTripwireTests(NoNetworkTestCase):
    def test_tripwire_fails_a_test_that_opens_a_socket(self):
        import socket
        with self.assertRaises(AssertionError):
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        with self.assertRaises(AssertionError):
            socket.getaddrinfo("example.com", 80)


if __name__ == "__main__":
    unittest.main()
