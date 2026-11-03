"""Tests for bin/owner-scoreboard.py: scoring, worst-first order, owner files keep their log.

unittest only, against a temp Watchtower snapshot and a temp owner dir.
Run: python3 bin/test_owner_scoreboard.py
"""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load(tmp):
    os.environ["OWNER_DIR"] = os.path.join(tmp, "owners")
    os.environ["WATCHTOWER_GITHUB_JSON"] = os.path.join(tmp, "github.json")
    loader = importlib.machinery.SourceFileLoader("owner_scoreboard", os.path.join(HERE, "owner-scoreboard.py"))
    spec = importlib.util.spec_from_loader("owner_scoreboard", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def pr(n, idle, ci="green"):
    return {"number": n, "title": "t", "idle_days": idle, "ci": ci}


SNAP = {"fetched": 1793680065.0, "lanes": {
    "a": [{"repo": "drrozen-med/vera", "default_ci": "red", "default_branch": "main",
           "open_prs": [pr(1, 10, "red"), pr(2, 1)], "flags": ["3 open blocker issue(s)"]}],
    "b": [{"repo": "drrozen-med/boardsBridge", "default_ci": "green", "default_branch": "main",
           "open_prs": [pr(n, 2) for n in range(10, 30)], "flags": ["16 more open PRs not checked"]},
          {"repo": "drrozen-med/cdl-bridge", "default_ci": "green", "open_prs": [], "flags": []}],
}}


class Scoreboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "github.json"), "w") as f:
            json.dump(SNAP, f)
        self.m = load(self.tmp.name)

    def tearDown(self):
        os.environ.pop("OWNER_DIR", None)
        os.environ.pop("WATCHTOWER_GITHUB_JSON", None)
        self.tmp.cleanup()

    def test_scores_and_order(self):
        b = self.m.board()
        by = {s["repo"]: s for s in b["repos"]}
        self.assertTrue(by["cdl-bridge"]["at_target"])
        self.assertEqual(by["vera"]["stale_prs"], [1])
        self.assertEqual(by["vera"]["red_prs"], [1])
        self.assertEqual(by["vera"]["blocker_issues"], 3)
        self.assertIn("CI red on main", by["vera"]["problems"])
        self.assertEqual(by["boardsBridge"]["open_prs"], 36)      # 20 listed + 16 not checked
        self.assertFalse(by["boardsBridge"]["at_target"])
        self.assertFalse(by["airline"]["known"])                  # managed but not in the snapshot
        order = [s["repo"] for s in b["repos"]]
        self.assertLess(order.index("vera"), order.index("boardsBridge"))   # red main before PR overflow
        self.assertEqual(order[-1], "cdl-bridge")
        self.assertEqual(b["fetched"][:4], "2026")

    def test_owner_file_keeps_log_across_refresh(self):
        self.m.append_log("vera", "closed  #2\nas duplicate")
        self.m.main([])
        with open(self.m.owner_path("vera")) as f:
            text = f.read()
        self.assertIn("# Owner: vera", text)
        self.assertIn("CI red on main", text)
        self.assertIn("closed #2 as duplicate", text)
        self.m.main([])
        with open(self.m.owner_path("vera")) as f:
            self.assertEqual(f.read().count("closed #2 as duplicate"), 1)

    def test_missing_snapshot(self):
        os.remove(os.path.join(self.tmp.name, "github.json"))
        b = self.m.board()
        self.assertEqual(b["at_target"], 0)
        self.assertTrue(all(not s["known"] for s in b["repos"]))


if __name__ == "__main__":
    unittest.main()
