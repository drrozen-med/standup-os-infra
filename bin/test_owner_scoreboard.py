"""Tests for bin/owner-scoreboard.py: scoring, worst-first order, owner folders (scoreboard.json, log.md), migration.

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

    def owner_file(self, *parts):
        return os.path.join(self.tmp.name, "owners", *parts)

    def test_folder_layout_scoreboard_and_log(self):
        self.m.append_log("vera", "closed  #2\nas duplicate")
        self.m.main(["--log", "vera", "--actor", "owner", "merged", "#3"])
        self.m.main([])
        with open(self.owner_file("vera", "scoreboard.json")) as f:
            board = json.load(f)
        self.assertIn("CI red on main", board["problems"])
        self.assertEqual(board["target"]["max_open_prs"], 5)
        self.assertEqual(board["fetched"][:4], "2026")
        self.m.main([])
        with open(self.owner_file("vera", "log.md")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)                                     # refresh never touches the log
        self.assertRegex(lines[0], r"^- \d{4}-\d\d-\d\d \d\d:\d\d \[supervisor\] closed #2 as duplicate$")
        self.assertRegex(lines[1], r"\[owner\] merged #3$")
        self.assertTrue(os.path.exists(self.owner_file("airline", "scoreboard.json")))   # every managed repo
        self.assertEqual(self.m.main(["--log", "vera", "--actor", "Bad!", "x"]), 2)

    def test_old_single_file_is_migrated_once(self):
        os.makedirs(self.owner_file())
        with open(self.owner_file("vera.md"), "w") as f:
            f.write("# Owner: vera\n\nScoreboard: old\n\n## Log\n- 2026-11-03 07:21 re-ran CI\n"
                    "- 2026-11-03 07:42 [owner] opened #166\nnot a log line\n")
        os.makedirs(self.owner_file("vera"))
        with open(self.owner_file("vera", "log.md"), "w") as f:
            f.write("- 2026-11-03 09:00 [owner] working: newer\n")
        self.m.main([])
        self.m.main([])
        with open(self.owner_file("vera", "log.md")) as f:
            self.assertEqual(f.read().splitlines(), ["- 2026-11-03 07:21 [supervisor] re-ran CI",
                                                     "- 2026-11-03 07:42 [owner] opened #166",
                                                     "- 2026-11-03 09:00 [owner] working: newer"])
        self.assertFalse(os.path.exists(self.owner_file("vera.md")))
        self.assertTrue(os.path.exists(self.owner_file("vera.md.migrated")))

    def test_missing_snapshot(self):
        os.remove(os.path.join(self.tmp.name, "github.json"))
        b = self.m.board()
        self.assertEqual(b["at_target"], 0)
        self.assertTrue(all(not s["known"] for s in b["repos"]))


if __name__ == "__main__":
    unittest.main()
