import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nav  # noqa: E402


class NavFilters(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000
        self.items = [
            {
                "key": "claude:1", "original": "Billing fix", "nick": "",
                "repo": "/code/app", "repo_name": "app", "group": "infra",
                "engine": "claude", "live": True, "mtime": self.now - 1800,
            },
            {
                "key": "codex:2", "original": "Docs update", "nick": "Nightly docs",
                "repo": "/code/site", "repo_name": "site", "group": "docs",
                "engine": "codex", "live": False, "mtime": self.now - 20 * 86400,
                "hidden": True,
            },
            {
                "key": "devin:3", "original": "Deploy", "nick": "",
                "repo": "/code/app", "repo_name": "app", "group": "infra",
                "engine": "devin", "live": False, "mtime": self.now - 3 * 3600,
            },
        ]

    def test_flatten_tree_keeps_repo_context(self):
        tree = {
            "repos": [{
                "key": "/code/app", "name": "app", "path": "/code/app",
                "groups": [{"name": "infra", "items": [self.items[0]]}],
            }]
        }
        flat = nav.flatten_tree(tree)
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["repo"], "/code/app")
        self.assertEqual(flat[0]["group"], "infra")

    def test_combined_filters(self):
        found = nav.filter_items(
            self.items, repo="app", group="infra", engine="claude",
            live=True, older=10 * 60, newer=3600, now=self.now,
        )
        self.assertEqual([item["key"] for item in found], ["claude:1"])

    def test_hidden_modes_and_match(self):
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, now=self.now)],
            ["claude:1", "devin:3"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, hidden_mode="include", now=self.now)],
            ["claude:1", "codex:2", "devin:3"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, hidden_mode="only", now=self.now)],
            ["codex:2"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(
                self.items, match="NIGHTLY", hidden_mode="include", now=self.now)],
            ["codex:2"],
        )

    def test_hidden_flag_alias(self):
        parser = nav.build_parser()
        self.assertTrue(parser.parse_args(["ls", "--hidden"]).include_hidden)
        self.assertTrue(parser.parse_args(["ls", "--include-hidden"]).include_hidden)

    def test_age_parsing_and_limit(self):
        self.assertEqual(nav.parse_age("14d"), 14 * 86400)
        self.assertEqual(nav.parse_age("6h"), 6 * 3600)
        with self.assertRaises(argparse.ArgumentTypeError):
            nav.parse_age("later")
        found = nav.filter_items(self.items, limit=1, now=self.now)
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
