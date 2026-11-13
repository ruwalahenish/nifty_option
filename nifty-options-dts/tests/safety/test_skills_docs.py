"""The skills and documentation must state the safety position and match the behaviour of the CLI."""
import json
import os
import re
import unittest

from nifty_core import cli
from nifty_core.guards import forbidden
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS = os.path.join(ROOT, "skills")
EXPECTED = ("nifty-regime", "nifty-strategy", "nifty-select", "nifty-risk", "nifty-paper", "nifty-positions", "nifty-journal")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no front matter"
    return m.group(1)


def subcommands():
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(getattr(action, "choices", None), dict):
            return set(action.choices)
    return set()


class SkillDocsTests(NoNetworkTestCase):
    def test_all_expected_skills_exist_and_nothing_else(self):
        self.assertEqual(sorted(d for d in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, d))), sorted(EXPECTED))

    def test_each_skill_states_paper_only_and_not_financial_advice(self):
        for name in EXPECTED:
            text = read(os.path.join(SKILLS, name, "SKILL.md"))
            low = text.lower()
            self.assertIn("paper-only", low, name)
            self.assertIn("not financial advice", low, name)
            self.assertIn("paper trading only", low, name)
            self.assertIn("never places a real order", low, name)
            self.assertIn("simulat", low, name)

    def test_front_matter_is_valid_and_matches_the_folder(self):
        for name in EXPECTED:
            fm = frontmatter(read(os.path.join(SKILLS, name, "SKILL.md")))
            self.assertRegex(fm, r"(?m)^name: %s$" % re.escape(name))
            self.assertRegex(fm, r"(?m)^description: >-$")
            desc = " ".join(l.strip() for l in fm.split("description: >-")[1].split("argument-hint")[0].splitlines())
            self.assertIn("Paper-only", desc, name)
            self.assertIn("not financial advice", desc.lower(), name)
            self.assertIn("argument-hint:", fm)

    def test_every_command_in_a_skill_is_a_real_cli_subcommand_and_every_subcommand_is_documented(self):
        used = set()
        for name in EXPECTED:
            for cmd in re.findall(r"python -m nifty_core\.cli ([a-z\-]+)", read(os.path.join(SKILLS, name, "SKILL.md"))):
                used.add(cmd)
        self.assertEqual(used, subcommands())

    def test_documented_example_commands_actually_run(self):
        import io
        for name in ("nifty-regime", "nifty-strategy", "nifty-risk"):
            text = read(os.path.join(SKILLS, name, "SKILL.md"))
            line = re.search(r"python -m nifty_core\.cli (.*)", text).group(1)
            code = cli.main(line.split(), io.StringIO())
            self.assertEqual(code, 0, line)

    def test_the_paper_skill_explains_the_yes_rule_and_forbids_self_approval(self):
        text = read(os.path.join(SKILLS, "nifty-paper", "SKILL.md"))
        for piece in ('STARTS with "yes"', "never type the yes yourself", "no flag to approve automatically",
                      "word for word" if "word for word" in text else "recorded"):
            self.assertIn(piece, text)

    def test_skills_contain_no_order_or_credential_patterns(self):
        for name in EXPECTED:
            self.assertEqual(forbidden.scan_source(read(os.path.join(SKILLS, name, "SKILL.md"))), [], name)

    def test_the_plugin_manifest_points_at_the_skills(self):
        manifest = json.loads(read(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        self.assertEqual(manifest["name"], "nifty-options-dts")
        self.assertEqual(manifest["skills"], "./skills")
        self.assertTrue(os.path.isdir(os.path.join(ROOT, manifest["skills"])))
        self.assertIn("paper", manifest["description"].lower())
        self.assertIn("not financial advice", manifest["description"].lower())
        self.assertIn("whole plugin", manifest["description"].lower())


class ReadmeTests(NoNetworkTestCase):
    def setUp(self):
        super(ReadmeTests, self).setUp()
        self.text = read(os.path.join(ROOT, "README.md"))
        self.low = self.text.lower()

    def test_readme_has_the_safety_statement(self):
        self.assertIn("## Safety statement", self.text)
        for piece in ("paper only", "not financial advice", "no live trading", "no broker", "recommendation for real"):
            self.assertIn(piece, self.low)

    def test_readme_says_the_skills_install_as_a_whole_plugin(self):
        self.assertIn("whole plugin", self.low)
        self.assertIn("individual skill", self.low)

    def test_readme_documents_every_cli_command_and_every_skill(self):
        for cmd in subcommands():
            self.assertIn("nifty_core.cli %s" % cmd, self.text, cmd)
        for name in EXPECTED:
            self.assertIn(name, self.text)

    def test_readme_does_not_overstate_the_status(self):
        self.assertIn("P10", self.text)
        self.assertIn("not evidence of profitability", self.low)
        for banned in ("profitable strategy", "guaranteed", "risk-free", "make money"):
            self.assertNotIn(banned, self.low)

    def test_readme_links_the_phase_documents_that_exist(self):
        for rel in re.findall(r"\]\((docs/[A-Z_]+\.md)\)", self.text):
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), rel)

    def test_end_to_end_document_matches_the_cli(self):
        text = read(os.path.join(ROOT, "docs", "END_TO_END.md"))
        for cmd in subcommands():
            self.assertIn("nifty_core.cli %s" % cmd, text)
        self.assertIn("AWAITING_APPROVAL", text)
        self.assertIn("DECLINED", text)


if __name__ == "__main__":
    unittest.main()
