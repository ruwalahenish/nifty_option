"""Static proof that no order-capable code, network code or credential exists in the project."""
import os
import re
import unittest

from nifty_core.guards import forbidden
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join(ROOT, "nifty_core")
SELF_EXEMPT = (os.path.join(CORE, "guards", "forbidden.py"),)       # it defines the forbidden words
SAFETY_TESTS = os.path.join(ROOT, "tests", "safety")                # they contain bad samples on purpose


def project_text_files(exts):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "trader")]
        for name in files:
            path = os.path.join(base, name)
            if name.endswith(exts) and path not in SELF_EXEMPT and not path.startswith(SAFETY_TESTS):
                yield path


class ScannerWorksTests(NoNetworkTestCase):
    """A scanner that never fires proves nothing, so prove it fires."""

    def test_flags_order_calls(self):
        for bad in ("client.place_order(x)", "place_stock_order(sym)", "broker.cancel_order(1)",
                    "kite.place_order", "close_all_positions()", "from kiteconnect import KiteConnect",
                    "exercise_options_position(1)"):
            self.assertTrue(forbidden.scan_source(bad), bad)

    def test_flags_live_flags_and_credentials(self):
        for bad in ('"paper": false', "paper = False", "live: false",
                    'api_key = "ABCDEFGHIJKLMNOP1234"', "password: 'hunter2hunter2hunter2'"):
            self.assertTrue(forbidden.scan_source(bad), bad)

    def test_does_not_flag_harmless_text(self):
        for ok in ('"paper": true', "get_option_chain(expiry)", "spread = DebitSpread(legs)"):
            self.assertEqual(forbidden.scan_source(ok), [], ok)

    def test_flags_network_and_subprocess_imports(self):
        for bad in ("import socket", "import requests", "from urllib.request import urlopen",
                    "import http.client", "import subprocess", "from asyncio import run"):
            self.assertTrue(forbidden.forbidden_imports(bad), bad)
        self.assertEqual(forbidden.forbidden_imports("import json\nimport datetime"), [])

    def test_forbidden_method_names(self):
        for bad in ("place_order", "submit", "execute_trade", "buy", "sell_spread", "cancel",
                    "modify_position", "squareoff", "sendOrder"):
            self.assertTrue(forbidden.method_name_is_forbidden(bad), bad)
        # the planned read-only provider interface (PLAN.md 1.1) must be clean
        for ok in ("get_clock", "get_underlying_bars", "get_option_chain", "get_quote",
                   "get_expiries", "get_event_calendar"):
            self.assertFalse(forbidden.method_name_is_forbidden(ok), ok)


class ProjectIsCleanTests(NoNetworkTestCase):
    def test_no_order_patterns_or_secrets_in_the_project(self):
        findings = []
        for path in project_text_files((".py", ".json", ".toml")):
            with open(path, encoding="utf-8") as fh:
                for rule, snippet in forbidden.scan_source(fh.read()):
                    findings.append("%s: %s (%s)" % (os.path.relpath(path, ROOT), rule, snippet))
        self.assertEqual(findings, [])

    def test_core_has_no_network_or_process_imports(self):
        findings = []
        for path in forbidden.python_files(CORE):
            if path in SELF_EXEMPT:
                continue
            with open(path, encoding="utf-8") as fh:
                bad = forbidden.forbidden_imports(fh.read())
            if bad:
                findings.append("%s imports %s" % (os.path.relpath(path, ROOT), bad))
        self.assertEqual(findings, [])

    def test_the_exempt_file_itself_has_no_network_imports(self):
        for path in SELF_EXEMPT:
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(forbidden.forbidden_imports(fh.read()), [])

    def test_core_never_deletes_files(self):
        # the kill switch must stay until a human removes it
        rx = re.compile(r"(os\.remove|os\.unlink|shutil\.rmtree|\.unlink\(|os\.rmdir)")
        for path in forbidden.python_files(CORE):
            with open(path, encoding="utf-8") as fh:
                self.assertIsNone(rx.search(fh.read()), path)


if __name__ == "__main__":
    unittest.main()
