"""PLAN.md section 6, guard 15: the original day-trader-skill repo is never modified."""
import json
import os
import subprocess
import unittest

from tools import upstream_hash
from tests._base import NoNetworkTestCase


@unittest.skipUnless(os.path.isdir(upstream_hash.UPSTREAM), "upstream day-trader-skill not next to this project")
class UpstreamUnchangedTests(NoNetworkTestCase):
    def setUp(self):
        super(UpstreamUnchangedTests, self).setUp()
        with open(upstream_hash.BASELINE, encoding="utf-8") as fh:
            self.baseline = json.load(fh)

    def test_file_tree_hash_matches_the_recorded_baseline(self):
        digest, count = upstream_hash.tree_hash(upstream_hash.UPSTREAM)
        self.assertEqual(count, self.baseline["file_count"])
        self.assertEqual(digest, self.baseline["tree_sha256"])

    def test_git_head_and_working_tree_are_unchanged(self):
        head = upstream_hash.git_head(upstream_hash.UPSTREAM)
        if head is None:
            self.skipTest("git not available")
        self.assertEqual(head, self.baseline["git_head"])
        status = subprocess.check_output(["git", "-C", upstream_hash.UPSTREAM, "status", "--porcelain"])
        self.assertEqual(status.strip(), b"")

    def test_the_hash_function_detects_a_change(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w") as fh:
                fh.write("one")
            h1, _ = upstream_hash.tree_hash(d)
            with open(os.path.join(d, "a.txt"), "w") as fh:
                fh.write("two")
            h2, _ = upstream_hash.tree_hash(d)
            self.assertNotEqual(h1, h2)
            os.makedirs(os.path.join(d, ".git"))
            with open(os.path.join(d, ".git", "x"), "w") as fh:
                fh.write("ignored")
            self.assertEqual(upstream_hash.tree_hash(d)[0], h2)       # .git is not hashed
            shutil.copy(os.path.join(d, "a.txt"), os.path.join(d, "b.txt"))
            self.assertNotEqual(upstream_hash.tree_hash(d)[0], h2)    # a new file changes it


if __name__ == "__main__":
    unittest.main()
