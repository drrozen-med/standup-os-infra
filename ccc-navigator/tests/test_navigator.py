import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import navigator  # noqa: E402
import orca  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


class RepoResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "BoardBridge"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        navigator._repo_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_checkout_and_subdir(self):
        (self.repo / "src").mkdir()
        self.assertEqual(navigator.resolve_repo(str(self.repo / "src")), (str(self.repo), "git", None))

    def test_claude_worktree_folds_into_main_repo(self):
        wt = self.repo / ".claude" / "worktrees" / "frosty-euler-bfdfd4"
        git("worktree", "add", "-q", str(wt), "-b", "claude/frosty", cwd=self.repo)
        self.assertEqual(navigator.resolve_repo(str(wt)), (str(self.repo), "git", "frosty-euler-bfdfd4"))

    def test_worktree_outside_repo(self):
        wt = self.root / "orca" / "workspaces" / "BoardBridge" / "cunner"
        git("worktree", "add", "-q", str(wt), "-b", "amir/cunner", cwd=self.repo)
        self.assertEqual(navigator.resolve_repo(str(wt))[0], str(self.repo))

    def test_deleted_worktree_uses_path_pattern(self):
        gone = str(self.repo / ".claude" / "worktrees" / "retired")
        self.assertEqual(navigator.resolve_repo(gone), (str(self.repo), "heuristic", "retired"))
        self.assertEqual(navigator.resolve_repo("/x/Repo-wt-fe"), ("/x/Repo", "heuristic", "fe"))

    def test_not_a_repo(self):
        self.assertEqual(navigator.resolve_repo(str(self.root)), (None, "none", None))


class Orca(unittest.TestCase):
    def test_parse_and_match(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "Orca"
            prof = data / "profiles" / "p1"
            prof.mkdir(parents=True)
            (data / "orca-profile-index.json").write_text(json.dumps({"activeProfileId": "p1"}))
            con = sqlite3.connect(prof / "profile-state.db")
            con.execute("create table profile_state_documents(domain TEXT PRIMARY KEY, payload TEXT)")
            docs = {
                "repos": [{"id": "r1", "path": "/code/BB", "displayName": "BB"}],
                "worktreeMeta": {
                    "r1::/w/BB/sms": {"displayName": "SMS visibility"},
                    "r1::/w/BB/sms/nested": {"displayName": "Nested", "isArchived": True},
                    "r1::/w/BB/cunner": {},
                },
                "folderWorkspaces": [{"id": "f", "name": "Notes", "folderPath": "/notes"}],
            }
            for k, v in docs.items():
                con.execute("insert into profile_state_documents values (?, ?)", (k, json.dumps(v)))
            con.commit()
            con.close()

            orca._cache.update({"at": 0.0, "sig": None})
            ws, meta = orca.load_workspaces(str(data))
            self.assertTrue(meta["found"])
            by_path = {w["path"]: w for w in ws}
            self.assertEqual(by_path["/w/BB/sms"]["name"], "SMS visibility")
            self.assertEqual(by_path["/w/BB/sms"]["repo_path"], "/code/BB")
            self.assertTrue(by_path["/w/BB/sms/nested"]["archived"])
            self.assertEqual(by_path["/w/BB/cunner"]["name"], "cunner")  # no name, folder gone: basename
            self.assertEqual(orca.workspace_for(ws, "/w/BB/sms/src")["name"], "SMS visibility")
            self.assertEqual(orca.workspace_for(ws, "/w/BB/sms/nested/x")["name"], "Nested")  # deepest wins
            self.assertIsNone(orca.workspace_for(ws, "/w/BB/smsx"))
            self.assertEqual(orca.workspace_for(ws, "/notes")["name"], "Notes")

    def test_branch_fallback_for_renamed_folder(self):
        ws = [{"path": "/w/new-name", "name": "Outbound", "branch": "a/outbound"},
              {"path": "/w/other", "name": "Other", "branch": "a/other"}]
        self.assertEqual(orca.workspace_for(ws, "/w/old-name", "a/outbound")["name"], "Outbound")
        self.assertIsNone(orca.workspace_for(ws, "/w/old-name", "main"))

    def test_missing_orca(self):
        orca._cache.update({"at": 0.0, "sig": None})
        with tempfile.TemporaryDirectory() as d:
            ws, meta = orca.load_workspaces(d)
        self.assertEqual(ws, [])
        self.assertFalse(meta["found"])


def row(sid, cwd, prompt, mins=1, **kw):
    r = {"session_id": sid, "engine": "claude", "source": "interactive", "session_cwd": cwd,
         "folder_path": cwd, "first_message": prompt, "modified": time.time() - mins * 60}
    r.update(kw)
    return r


class Tree(unittest.TestCase):
    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()
        self.state = {"nicknames": {}, "groups": {}, "assign": {}}

    def build(self, rows, ws=None, window="1d"):
        return navigator.build_tree(rows, self.state, window, ws or [], "http://ccc")

    def test_noise_is_hidden_and_batches_collapse(self):
        with tempfile.TemporaryDirectory() as d:
            headless = Path(d) / "h.jsonl"
            headless.write_text(json.dumps({"type": "user", "entrypoint": "sdk-cli"}) + "\n")
            codex_exec = Path(d) / "c.jsonl"
            codex_exec.write_text(json.dumps({"type": "session_meta", "payload": {"source": "exec"}}) + "\n")
            rows = [
                row("a", "/r/A", "Real work on the billing fix please"),
                row("s", "/x/command-center/scratch", "Generate concise title for prompt"),
                row("h", "/r/A", "Reply with exactly: OK", jsonl_path=str(headless)),
                row("c", "/r/A", "Summarise this file for me please", engine="codex", jsonl_path=str(codex_exec)),
                row("old", "/r/A", "Something from last month", mins=60 * 24 * 30),
                row("t", "/r/A", "Trashed one, should never show", trashed=True),
            ] + [row(f"b{i}", "/r/A", "You are a senior family physician grading case") for i in range(4)]
            t = self.build(rows)
        hidden = {h["label"]: h["count"] for h in t["hidden"]}
        self.assertEqual(hidden["CCC's own title-generation runs"], 1)
        self.assertEqual(hidden["Headless runs (claude -p, codex exec)"], 2)
        self.assertEqual(hidden["Archived or trashed"], 1)
        items = [s for r in t["repos"] for g in r["groups"] for s in g["items"]]
        self.assertEqual(len(items), 2)  # real session + one batch row
        batch = [s for s in items if s.get("batch")][0]
        self.assertEqual(batch["batch"], 4)
        self.assertEqual(len(batch["runs"]), 4)

    def test_group_precedence_manual_then_orca(self):
        ws = [{"path": "/w/BB/sms", "name": "SMS visibility", "branch": "", "repo_path": "/code/BB"}]
        rows = [row("1", "/w/BB/sms", "Show SMS status on the buyer timeline"),
                row("2", "/w/BB/sms", "Add Twilio webhook signature check")]
        self.state["assign"]["claude:2"] = {"repo": "/code/BB", "group": "infra"}
        self.state["nicknames"]["claude:1"] = "SMS status"
        t = self.build(rows, ws)
        self.assertEqual(len(t["repos"]), 1)
        repo = t["repos"][0]
        self.assertEqual(repo["key"], "/code/BB")  # folder gone, repo taken from Orca
        groups = {g["name"]: g for g in repo["groups"]}
        self.assertEqual(groups["SMS visibility"]["source"], "orca")
        self.assertEqual(groups["SMS visibility"]["items"][0]["nick"], "SMS status")
        self.assertEqual(groups["infra"]["source"], "manual")
        self.assertEqual(groups["infra"]["items"][0]["sid"], "2")

    def test_open_url_and_pi_has_none(self):
        t = self.build([row("abc", "/r/A", "Some real task to work on"),
                        row("p1", "/r/A", "Pi task here to work on", engine="pi")])
        items = {s["sid"]: s for r in t["repos"] for g in r["groups"] for s in g["items"]}
        self.assertEqual(items["abc"]["open_url"], "http://ccc/?ccc_popout=conversation&conv=abc")
        self.assertEqual(items["p1"]["open_url"], "")

    def test_claude_scratch_workspaces_fold_into_one_repo(self):
        root = f"{navigator.HOME}/Library/Application Support/Claude/scratch-workspaces"
        rows = [
            row("1", f"{root}/-Users-tu-Library-Application-Support-Qux",
                "Draft the launch note for this project"),
            row("2", f"{root}/-Users-tu-projects-beta/src", "Another scratch session entirely"),
            row("3", f"{root}/-Users-tu-Library-Application-Support-Qux/nested",
                "Working one level deeper in the workspace"),
        ]
        t = self.build(rows)
        self.assertEqual(len(t["repos"]), 1)  # three folders, one repo
        repo = t["repos"][0]
        self.assertEqual(repo["key"], "folder:claude-scratch")
        self.assertEqual(repo["name"], "Claude scratch")
        self.assertEqual(repo["path"], "~/Library/Application Support/Claude/scratch-workspaces")
        items = [s for g in repo["groups"] for s in g["items"]]
        self.assertEqual(sorted(s["sid"] for s in items), ["1", "2", "3"])
        self.assertEqual(t["diagnostics"]["resolution"]["claude-scratch"], 3)

    def test_unresolved_folder_named_by_last_path_component(self):
        rows = [
            row("1", "/Users/tu/Notes/quarterly-review", "Summarise the quarter for me",
                folder_label="-Users-tu-Library-Appli-aaaaa-bbbbb"),
            row("2", "/Users/tu/Notes/deep-dive/", "Trailing slash should not leak in"),
            row("3", "", "Session with no folder at all"),
        ]
        t = self.build(rows)
        repos = {r["key"]: r for r in t["repos"]}
        self.assertEqual(sorted(repos), ["folder:deep-dive", "folder:quarterly-review", "folder:unknown"])
        self.assertEqual(repos["folder:quarterly-review"]["name"], "quarterly-review")
        self.assertEqual(repos["folder:deep-dive"]["name"], "deep-dive")
        self.assertEqual(repos["folder:unknown"]["name"], "unknown")
        self.assertTrue(all("-Users-tu-Library" not in r["name"] for r in repos.values()))


class StateFile(unittest.TestCase):
    def test_roundtrip_and_group_ops(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s" / "state.json"
            st = navigator.State(p)
            st.set_nickname("k1", "  Neon  ")
            st.assign("k1", "/r", "infra")
            st.assign("k2", "/r", "infra")
            st.rename_group("/r", "infra", "platform")
            st2 = navigator.State(p)
            self.assertEqual(st2.data["nicknames"], {"k1": "Neon"})
            self.assertEqual(st2.data["groups"], {"/r": ["platform"]})
            self.assertEqual(st2.data["assign"]["k2"]["group"], "platform")
            st2.remove_group("/r", "platform")
            self.assertEqual(st2.data["assign"], {})
            st2.set_nickname("k1", "")
            self.assertEqual(navigator.State(p).data["nicknames"], {})


class Pi(unittest.TestCase):
    def test_reads_header_name_and_first_message(self):
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d) / "--code-proj--"
            sd.mkdir()
            (sd / "2026_x.jsonl").write_text("\n".join(json.dumps(o) for o in [
                {"type": "session", "version": 3, "id": "u1", "cwd": "/code/proj"},
                {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Tidy units"}]}},
                {"type": "session_info", "name": "systemd cleanup"},
            ]) + "\n")
            rows, meta = navigator.pi_sessions(d)
        self.assertEqual(meta["count"], 1)
        r = rows[0]
        self.assertEqual((r["session_id"], r["session_cwd"], r["display_name"], r["first_message"]),
                         ("u1", "/code/proj", "systemd cleanup", "Tidy units"))


if __name__ == "__main__":
    unittest.main()
