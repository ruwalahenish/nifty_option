"""Core-engine boundaries (PLAN.md section 6): no hidden clock, no network, no credentials, no live mode."""
import os
import re
import unittest

from nifty_core.guards import forbidden
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join(ROOT, "nifty_core")
CLOCK_FILE = os.path.join(CORE, "market", "ist.py")

HIDDEN_CLOCK = re.compile(r"(datetime\.now\(|\.utcnow\(|\.today\(|\btime\.time\(|\btime\.localtime|\btime\.tzset|"
                          r"datetime\.fromtimestamp\(|\btime\.strftime\()")
NETWORK_WORDS = re.compile(r"\b(urlopen|urlretrieve|HTTPConnection|HTTPSConnection|create_connection|"
                           r"socket\.socket|requests\.(get|post)|websocket)\b")


def core_sources():
    for path in forbidden.python_files(CORE):
        if path == os.path.join(CORE, "guards", "forbidden.py"):
            continue
        with open(path, encoding="utf-8") as fh:
            yield path, fh.read()


class BoundaryTests(NoNetworkTestCase):
    def test_only_the_system_clock_class_reads_real_time(self):
        for path, src in core_sources():
            if path == CLOCK_FILE:
                continue
            self.assertIsNone(HIDDEN_CLOCK.search(src), "%s reads the real clock" % os.path.relpath(path, ROOT))
        with open(CLOCK_FILE, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(len(re.findall(r"datetime\.now\(", src)), 1)

    def test_core_contains_no_network_calls(self):
        for path, src in core_sources():
            self.assertIsNone(NETWORK_WORDS.search(src), os.path.relpath(path, ROOT))

    def test_core_never_opens_files_for_writing_except_the_kill_switch(self):
        for path, src in core_sources():
            for m in re.finditer(r"open\([^)]*['\"][wax]\+?b?['\"]", src):
                if path.endswith(os.path.join("journal", "journal.py")):
                    # the journal may only APPEND (mode "a"); it has no way to rewrite or truncate
                    self.assertRegex(m.group(0), r"['\"]a['\"]$", "%s must only append: %s" % (path, m.group(0)))
                    continue
                self.assertTrue(path.endswith(os.path.join("guards", "paper_only.py")),
                                "%s writes a file: %s" % (os.path.relpath(path, ROOT), m.group(0)))

    def test_cost_and_market_layers_have_no_order_or_broker_connectivity(self):
        for sub in ("costs", "market"):
            for path in forbidden.python_files(os.path.join(CORE, sub)):
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                self.assertEqual(forbidden.scan_source(src), [], path)
                self.assertEqual(forbidden.forbidden_imports(src), [], path)

    def test_broker_profiles_carry_tariffs_only_no_credentials_or_endpoints(self):
        folder = os.path.join(ROOT, "config", "costs", "brokers")
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                text = fh.read().lower()
            for banned in ("api_key", "secret", "token", "password", "endpoint", "login", "client_id"):
                self.assertNotIn(banned, text, "%s mentions %s" % (name, banned))
            self.assertEqual(forbidden.scan_source(text), [], name)


if __name__ == "__main__":
    unittest.main()
