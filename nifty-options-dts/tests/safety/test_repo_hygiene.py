"""Standard library only, Python 3.8 syntax, no accidental secrets in config, documented paper-only."""
import ast
import json
import os
import re
import sys
import unittest

from nifty_core.guards import forbidden
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_PACKAGES = {"nifty_core", "tools", "tests"}
BAD_38 = ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(")


def py_files():
    for sub in ("nifty_core", "tools", "tests"):
        for path in forbidden.python_files(os.path.join(ROOT, sub)):
            yield path


class HygieneTests(NoNetworkTestCase):
    def test_every_file_parses_as_python_3_8(self):
        for path in py_files():
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            ast.parse(src, filename=path, feature_version=(3, 8))
            if path == os.path.abspath(__file__).replace(".pyc", ".py"):
                continue   # this file lists the banned strings
            ast.parse(src, filename=path, feature_version=(3, 8))
            for bad in BAD_38:
                self.assertNotIn(bad, src, "%s uses %s" % (path, bad))
            self.assertIsNone(re.search(r"(?<![\w.])(list|dict|tuple|set)\[", src), path)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "needs Python 3.10+ to list the stdlib")
    def test_imports_are_standard_library_or_local(self):
        allowed = set(sys.stdlib_module_names) | LOCAL_PACKAGES
        for path in py_files():
            with open(path, encoding="utf-8") as fh:
                mods = forbidden.imported_modules(fh.read())
            self.assertEqual(sorted(mods - allowed), [], path)

    def test_config_files_declare_paper_and_no_credentials(self):
        with open(os.path.join(ROOT, "config", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        for key in cfg:
            self.assertFalse(re.search(r"key|secret|token|password|broker", key, re.I), key)

    def test_readme_states_paper_only_and_not_advice(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().lower().split())
        self.assertIn("paper", text)
        self.assertIn("no live trading", text)
        self.assertIn("not financial advice", text)


if __name__ == "__main__":
    unittest.main()
