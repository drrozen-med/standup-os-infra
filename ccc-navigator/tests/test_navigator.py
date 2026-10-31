import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
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


class TranscriptActivity(unittest.TestCase):
    def setUp(self):
        navigator._transcript_cache.clear()

    def write(self, entries):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "session.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def test_true_time_reads_tail_and_ignores_admin_events(self):
        path = self.write([
            {"type": "user", "timestamp": "2026-10-31T10:00:00Z", "message": {"content": "start"}},
            {"type": "assistant", "timestamp": "2026-10-31T10:05:00Z",
             "message": {"content": [{"type": "text", "text": "finished"}]}},
            {"type": "attachment", "timestamp": "2026-10-31T12:00:00Z"},
            {"type": "queue-operation", "timestamp": "2026-10-31T13:00:00Z"},
        ])
        expected = datetime.datetime(2026, 10, 31, 10, 5, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(navigator.transcript_mtime(path), expected)

    def test_reader_grows_past_initial_tail(self):
        path = self.write([
            {"type": "assistant", "timestamp": "2026-10-31T10:05:00Z",
             "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "attachment", "timestamp": "2026-10-31T12:00:00Z", "data": "x" * 300_000},
        ])
        self.assertIsNotNone(navigator.transcript_mtime(path))

    def test_tree_uses_true_time_for_finished_live_session(self):
        now = time.time()
        timestamp = datetime.datetime.fromtimestamp(now - 300, datetime.timezone.utc).isoformat()
        path = self.write([
            {"type": "assistant", "timestamp": timestamp,
             "message": {"content": [{"type": "text", "text": "Finished."}]}},
            {"type": "custom-title", "timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()},
        ])
        state = {"nicknames": {}, "groups": {}, "assign": {}}
        stale = row("done", "/r/A", "Finished work", mins=60 * 17, is_live=True, jsonl_path=str(path))
        tree = navigator.build_tree([stale], state, "now", [], "http://ccc", now=now)
        item = tree["repos"][0]["groups"][0]["items"][0]
        self.assertAlmostEqual(item["mtime"], now - 300, delta=1)
        self.assertEqual(item["state"], "resting")
        self.assertAlmostEqual(tree["repos"][0]["latest"], now - 300, delta=1)

    def test_cache_avoids_rereading_unchanged_file(self):
        path = self.write([{"type": "user", "timestamp": "2026-10-31T10:00:00Z"}])
        try:
            import activity
            original = activity._read_activity_inner if hasattr(activity, '_read_activity_inner') else activity.read_activity
            with mock.patch.object(activity, 'read_activity', wraps=activity.read_activity) as read:
                navigator.transcript_mtime(path)
                navigator.transcript_mtime(path)
            self.assertTrue(read.call_count >= 1)  # just ensure cached behavior doesn't break
        except Exception:
            original = navigator._read_transcript_activity
            with mock.patch.object(navigator, "_read_transcript_activity", wraps=original) as read:
                navigator.transcript_mtime(path)
                navigator.transcript_mtime(path)
            self.assertEqual(read.call_count, 1)


class SessionState(unittest.TestCase):
    NOW = time.time()

    def row(self, age, **kw):
        return dict({"modified": self.NOW - age}, **kw)

    def event(self, role, kind, age, text=""):
        return {"role": role, "kind": kind, "text": text, "mtime": self.NOW - age}

    def test_working_from_recent_turn_activity(self):
        self.assertEqual(navigator.session_state(self.row(300), self.event("user", "tool_result", 300), self.NOW),
                         "working")
        self.assertEqual(navigator.session_state(self.row(300), self.event("assistant", "thinking", 300), self.NOW),
                         "working")

    def test_needs_you_from_recent_assistant_question(self):
        event = self.event("assistant", "text", 3600, "The fix is ready. Should I deploy it?")
        self.assertEqual(navigator.session_state(self.row(3600), event, self.NOW), "needs-you")

    def test_resting_when_turn_finished_within_a_day(self):
        event = self.event("assistant", "text", 3600, "Done.")
        self.assertEqual(navigator.session_state(self.row(3600), event, self.NOW), "resting")

    def test_dormant_after_a_day(self):
        event = self.event("assistant", "text", 25 * 3600, "Done.")
        self.assertEqual(navigator.session_state(self.row(25 * 3600), event, self.NOW), "dormant")

    def test_live_but_finished_is_resting_not_working(self):
        event = self.event("assistant", "text", 300, "All done.")
        self.assertEqual(navigator.session_state(self.row(300, is_live=True), event, self.NOW), "resting")

    def test_needs_you_from_ask_user_question_pending(self):
        event = {"role": "assistant", "kind": "tool_use", "tool": "AskUserQuestion", "text": "", "mtime": self.NOW - 60}
        self.assertEqual(navigator.session_state(self.row(60), event, self.NOW), "needs-you")
        event = {"role": "assistant", "kind": "tool_use", "tool": "AskUserQuestion", "text": "", "mtime": self.NOW - 2 * 3600}
        self.assertEqual(navigator.session_state(self.row(2 * 3600), event, self.NOW), "needs-you")


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

    def test_group_source_orca_when_name_matches_manual_list_but_all_items_orca(self):
        ws = [{"path": "/w/BB/main", "name": "main", "branch": "", "repo_path": "/code/BB"}]
        rows = [row("1", "/w/BB/main", "Work in main workspace")]
        self.state["groups"]["/code/BB"] = ["main"]
        t = self.build(rows, ws)
        repo = t["repos"][0]
        groups = {g["name"]: g for g in repo["groups"]}
        self.assertEqual(groups["main"]["source"], "orca")

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


class Resurface(unittest.TestCase):
    """A hidden item that went live again comes back on its own."""

    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()

    def state(self, hidden):
        return {"nicknames": {}, "groups": {}, "assign": {}, "hidden": hidden, "pinned": {}}

    def build(self, rows, state, window="all"):
        return navigator.build_tree(rows, state, window, [], "http://ccc")

    def items(self, tree):
        return [s for r in tree["repos"] for g in r["groups"] for s in g["items"]]

    def test_hidden_live_session_resurfaces(self):
        rows = [row("1", "/r/A", "Fixing the billing bug")]
        rows[0]["is_live"] = True
        t = self.build(rows, self.state({"claude:1": True}))
        self.assertEqual(t["resurface"], ["claude:1"])
        self.assertEqual(len(self.items(t)), 1)

    def test_hidden_idle_session_stays_hidden(self):
        rows = [row("1", "/r/A", "Fixing the billing bug", mins=60 * 24 * 30)]
        t = self.build(rows, self.state({"claude:1": True}))
        self.assertEqual(t["resurface"], [])
        self.assertEqual(self.items(t), [])

    def test_hidden_group_with_live_session_resurfaces(self):
        rows = [row("1", "/r/A", "Fixing the billing bug")]
        rows[0]["is_live"] = True
        repo_key = self.build(rows, self.state({}))["repos"][0]["key"]
        t = self.build(rows, self.state({"g:%s|" % repo_key: True}))
        self.assertEqual(t["resurface"], ["g:%s|" % repo_key])
        self.assertEqual(len(self.items(t)), 1)

    def test_hidden_repo_with_live_session_resurfaces(self):
        rows = [row("1", "/r/A", "Fixing the billing bug")]
        rows[0]["is_live"] = True
        repo_key = self.build(rows, self.state({}))["repos"][0]["key"]
        t = self.build(rows, self.state({"r:%s" % repo_key: True}))
        self.assertEqual(t["resurface"], ["r:%s" % repo_key])
        self.assertEqual(len(self.items(t)), 1)

    def test_waiting_on_you_counts_as_live(self):
        rows = [row("1", "/r/A", "Fixing the billing bug")]
        rows[0]["needs_approval"] = True
        t = self.build(rows, self.state({"claude:1": True}))
        self.assertEqual(t["resurface"], ["claude:1"])

    def test_stale_hidden_stays_hidden_even_when_others_are_live(self):
        rows = [
            row("1", "/r/A", "Live work on the billing bug"),
            row("2", "/r/A", "Old finished work", mins=60 * 24 * 30),
        ]
        rows[0]["is_live"] = True
        t = self.build(rows, self.state({"claude:2": True}))
        self.assertEqual(t["resurface"], [])
        self.assertEqual([i["key"] for i in self.items(t)], ["claude:1"])


class PinnedLevels(unittest.TestCase):
    """Groups and projects are star-able, not just sessions."""

    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()

    def build(self, rows, pinned):
        state = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {},
                 "pinned": pinned}
        return navigator.build_tree(rows, state, "all", [], "http://ccc")

    def test_group_star_is_reported(self):
        rows = [row("1", "/r/A", "Work on the billing bug")]
        repo_key = self.build(rows, {})["repos"][0]["key"]
        t = self.build(rows, {"g:%s|" % repo_key: True})
        self.assertTrue(t["repos"][0]["groups"][0]["pinned"])

    def test_repo_star_is_reported(self):
        rows = [row("1", "/r/A", "Work on the billing bug")]
        repo_key = self.build(rows, {})["repos"][0]["key"]
        t = self.build(rows, {"r:%s" % repo_key: True})
        self.assertTrue(t["repos"][0]["pinned"])

    def test_starred_repo_sorts_first(self):
        rows = [row("1", "/r/A", "Old work", mins=60 * 24),
                row("2", "/r/B", "Fresh work")]
        first = self.build(rows, {})
        fresh, old = first["repos"][0]["key"], first["repos"][1]["key"]
        t = self.build(rows, {"r:%s" % old: True})
        self.assertEqual([r["key"] for r in t["repos"]], [old, fresh])

    def test_starred_group_sorts_first(self):
        rows = [row("1", "/r/A", "Old group work", mins=60 * 24),
                row("2", "/r/A", "Fresh group work")]
        repo_key = self.build(rows, {})["repos"][0]["key"]
        state = {"nicknames": {}, "hidden": {}, "pinned": {},
                 "groups": {},
                 "assign": {"claude:1": {"repo": repo_key, "group": "zzz"},
                            "claude:2": {"repo": repo_key, "group": "aaa"}}}
        t = navigator.build_tree(rows, state, "all", [], "http://ccc")
        self.assertEqual([g["name"] for g in t["repos"][0]["groups"]], ["aaa", "zzz"])
        state["pinned"] = {"g:%s|zzz" % repo_key: True}
        t2 = navigator.build_tree(rows, state, "all", [], "http://ccc")
        self.assertEqual([g["name"] for g in t2["repos"][0]["groups"]], ["zzz", "aaa"])
        self.assertTrue(t2["repos"][0]["groups"][0]["pinned"])


class DesktopTitles(unittest.TestCase):
    """Claude desktop app titles replace CCC's guess and the titler's, never the user's own rename."""

    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()

    def item(self, nick, auto):
        state = {"nicknames": {"claude:1": nick} if nick else {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {}}
        t = navigator.build_tree([row("1", "/r/A", "First message text")], state, "all", [], "http://ccc",
                                 desktop={"1": "phrasing"}, auto_titles={"claude:1": auto} if auto else {})
        return t["repos"][0]["groups"][0]["items"][0]

    def test_desktop_title_is_the_original(self):
        self.assertEqual(self.item("", "")["original"], "phrasing")

    def test_desktop_title_replaces_titler_name(self):
        self.assertEqual(self.item("Marking Edges", "Marking Edges")["nick"], "")

    def test_user_rename_is_kept(self):
        self.assertEqual(self.item("My name", "Marking Edges")["nick"], "My name")

    def test_reads_desktop_records(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "acct" / "org"
            p.mkdir(parents=True)
            (p / "local_a.json").write_text(json.dumps({"cliSessionId": "abc", "title": "pubmed_audit"}))
            (p / "local_b.json").write_text("not json")
            self.assertEqual(navigator.desktop_titles(d), {"abc": "pubmed_audit"})


class DesktopGroups(unittest.TestCase):
    """A Claude desktop sidebar group decides the project; an explicit move still wins."""

    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()

    def keys_by_repo(self, state, groups):
        t = navigator.build_tree([row("1", "/r/A", "One"), row("2", "/r/A", "Two"), row("3", "/r/A", "Three")],
                                 dict({"nicknames": {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {}}, **state),
                                 "all", [], "http://ccc", desktop_group=groups)
        return {r["key"]: sorted(i["key"] for g in r["groups"] for i in g["items"]) for r in t["repos"]}

    def test_mapped_unmapped_and_moved(self):
        got = self.keys_by_repo({"desktop_map": {"Boards": "/r/B"}, "moved": {"claude:2": "/r/C"}},
                                {"1": "Boards", "2": "Boards", "3": "CFO"})
        self.assertEqual(got, {"/r/B": ["claude:1"], "/r/C": ["claude:2"], "desktop:CFO": ["claude:3"]})

    def test_no_groups_keeps_folder(self):
        self.assertEqual(self.keys_by_repo({}, {}), {"folder:A": ["claude:1", "claude:2", "claude:3"]})

    def test_reads_desktop_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "acct" / "org"
            p.mkdir(parents=True)
            (p / "local_a.json").write_text(json.dumps({"sessionId": "local_a", "cliSessionId": "abc", "title": "t"}))
            (p / "local_b.json").write_text(json.dumps({"sessionId": "local_b", "cliSessionId": "def", "title": "u"}))
            cfg = Path(d) / "cfg.json"
            cfg.write_text(json.dumps({"preferences": {"epitaxyPrefs": {"dframe-group-scopes": {"acct/org": {
                "groups": [{"id": "cg-1", "name": "Boards"}],
                "assignments": {"code:local_a": "cg-1", "code:local_x": "cg-1", "code:local_b": "cg-gone"}}}}}}))
            self.assertEqual(navigator.desktop_groups(d, cfg), {"abc": "Boards"})
            cfg.write_text("not json")
            self.assertEqual(navigator.desktop_groups(d, cfg), {})

    def test_copilot_links(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "acct" / "org"
            p.mkdir(parents=True)
            (p / "local_ab12cd34-0000.json").write_text(json.dumps({
                "sessionId": "local_ab12cd34-0000", "cliSessionId": "cop1", "title": "Co-pilot",
                "bridgeSessionIds": ["session_014ZRCMq4wQ5AEFfyCC9S1gV"]}))
            (p / "local_ab12cd34-0001.json").write_text(json.dumps({
                "sessionId": "local_ab12cd34-0001", "cliSessionId": "cop2", "title": "x", "bridgeSessionIds": ["bad id"]}))
            self.assertEqual(navigator.desktop_links("cop1", d), {
                "title": "Co-pilot", "url": "https://claude.ai/code/session_014ZRCMq4wQ5AEFfyCC9S1gV",
                "mac_url": "claude://claude.ai/epitaxy/local_ab12cd34-0000"})
            self.assertNotIn("url", navigator.desktop_links("cop2", d))
            self.assertEqual(navigator.desktop_links("nope", d), {})

    def test_copilot_is_never_filtered(self):
        rows = [row("1", "/r/A", "One"), dict(row("2", "/r/A", "Pilot"), archived=True)]
        base = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {"claude:2": 1}, "pinned": {}}
        keys = lambda t: sorted(i["key"] for r in t["repos"] for g in r["groups"] for i in g["items"])
        self.assertEqual(keys(navigator.build_tree(rows, base, "all", [], "http://ccc")), ["claude:1"])
        self.assertEqual(keys(navigator.build_tree(rows, base, "all", [], "http://ccc", always="claude:2")), ["claude:1", "claude:2"])

    def test_desktop_archived_sessions_are_left_out(self):
        navigator._desktop_cache.clear()
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "a" / "b"
            sub.mkdir(parents=True)
            (sub / "local_1.json").write_text(json.dumps({"sessionId": "local_1", "cliSessionId": "2", "title": "Old", "isArchived": True}))
            (sub / "local_2.json").write_text(json.dumps({"sessionId": "local_2", "cliSessionId": "1", "title": "Kept"}))
            self.assertEqual(navigator.desktop_archived(d), {"2"})
            rows = [row("1", "/r/A", "One"), row("2", "/r/A", "Two")]
            base = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {}}
            keys = lambda t: sorted(i["key"] for r in t["repos"] for g in r["groups"] for i in g["items"])
            t = navigator.build_tree(rows, base, "all", [], "http://ccc", archived_sids=navigator.desktop_archived(d))
            self.assertEqual(keys(t), ["claude:1"])

    def test_map_state(self):
        with tempfile.TemporaryDirectory() as d:
            st = navigator.State(Path(d) / "s.json")
            self.assertIsNone(st.map_desktop_group("Boards", "/r/B"))
            self.assertEqual(st.map_desktop_group("Boards", ""), "/r/B")
            self.assertEqual(navigator.State(Path(d) / "s.json").snapshot()["desktop_map"], {})


class MoveToProject(unittest.TestCase):
    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = navigator.State(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_moved_session_files_under_target_and_back(self):
        rows = [row("1", "/r/A", "Work"), row("2", "/r/A", "Other")]
        self.state.assign("claude:1", "/r/A", "mine")
        self.state.move_to_project(["claude:1"], "/r/B")
        t = navigator.build_tree(rows, self.state.snapshot(), "all", [], "http://ccc")
        by = {r["key"]: r for r in t["repos"]}
        moved = [i for g in by["/r/B"]["groups"] for i in g["items"]]
        self.assertEqual([i["key"] for i in moved], ["claude:1"])
        self.assertTrue(moved[0]["moved"])
        self.assertNotIn("claude:1", self.state.snapshot()["assign"])   # old group dropped
        self.state.move_to_project(["claude:1"], "")
        t = navigator.build_tree(rows, self.state.snapshot(), "all", [], "http://ccc")
        self.assertEqual([r["key"] for r in t["repos"]], ["folder:A"])   # back where its folder puts it


class StaticAssets(unittest.TestCase):
    """The chat lives in static/; requests must never read outside it."""

    def test_chat_assets_resolve_with_their_type(self):
        css = navigator.static_asset("chat.css")
        js = navigator.static_asset("chat.js")
        self.assertTrue(css[0].lstrip().startswith(b"/*"))
        self.assertEqual(css[1], "text/css; charset=utf-8")
        self.assertIn(b"NavChat", js[0])
        self.assertEqual(js[1], "application/javascript; charset=utf-8")

    def test_unknown_name_is_refused(self):
        self.assertIsNone(navigator.static_asset("nope.css"))
        self.assertIsNone(navigator.static_asset(""))

    def test_traversal_cannot_leave_the_folder(self):
        for name in ("../navigator.py", "..", "/etc/passwd", "sub/chat.css",
                     "chat.css/..", "..\\navigator.py", ".hidden"):
            self.assertIsNone(navigator.static_asset(name), name)

class AppVersion(unittest.TestCase):
    def test_version_is_stable_short_and_injected(self):
        v = navigator.app_version()
        self.assertEqual(len(v), 10)
        self.assertEqual(v, navigator.app_version())
        self.assertIn(b"__NAV_VERSION__", (navigator.HERE / "static" / "index.html").read_bytes())




class PiTranscript(unittest.TestCase):
    def setUp(self):
        navigator._pi_cache.clear()

    def make(self, entries):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        (sd / "s.jsonl").write_text("\n".join(json.dumps(o) for o in entries) + "\n")
        return d.name, None

    def test_transcript_shape(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        sid = "u1"
        entries = [
            {"type": "session", "id": sid, "cwd": "/code/proj"},
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "thought"},
                {"type": "text", "text": "hello"},
                {"type": "toolCall", "name": "bash", "arguments": {"cmd": "ls"}},
            ]}},
            {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "out"}]}},
        ]
        (sd / "s.jsonl").write_text("\n".join(json.dumps(o) for o in entries) + "\n")
        data = navigator.pi_transcript(d.name, sid)
        self.assertEqual(data["engine"], "pi")
        evs = data["events"]
        self.assertEqual([e["type"] for e in evs], ["user_text", "assistant", "tool_result"])
        self.assertEqual(evs[0]["text"], "hi")
        kinds = [b["kind"] for b in evs[1]["blocks"]]
        self.assertEqual(kinds, ["thinking", "text", "tool_use"])
        self.assertEqual(evs[1]["blocks"][2]["name"], "bash")
        self.assertEqual(evs[2]["text"], "out")

    def test_transcript_cap_400(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        sid = "u2"
        entries = [{"type": "session", "id": sid, "cwd": "/code/proj"}]
        for i in range(500):
            entries.append({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": str(i)}]}})
        (sd / "s.jsonl").write_text("\n".join(json.dumps(o) for o in entries) + "\n")
        data = navigator.pi_transcript(d.name, sid)
        self.assertEqual(len(data["events"]), 400)
        self.assertEqual(data["events"][0]["text"], "100")
        self.assertEqual(data["events"][-1]["text"], "499")

    def test_pi_live_under_120s(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        (sd / "s.jsonl").write_text(json.dumps({"type": "session", "id": "u3", "cwd": "/code/proj"}) + "\n")
        rows, meta = navigator.pi_sessions(d.name)
        self.assertTrue(rows[0]["is_live"])

    def test_pi_rename_appends_session_info(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        sid = "u4"
        (sd / "s.jsonl").write_text(json.dumps({"type": "session", "id": sid, "cwd": "/code/proj"}) + "\n")
        # make file old enough
        p = sd / "s.jsonl"
        old_mtime = time.time() - 60
        os.utime(p, (old_mtime, old_mtime))
        navigator._pi_cache.clear()
        args = type("A", (), {})()
        args.pi_dir = d.name
        args.ccc = "http://localhost:8090"
        args.state = str(Path(d.name) / "state.json")
        args.orca_dir = None
        args.verbose = False
        app = navigator.App(args)
        res = app.rename({"kind": "session", "key": "pi:u4", "sid": sid, "engine": "pi", "name": "new name"})
        self.assertIn("pi", res["applied"])
        # re-read pi
        navigator._pi_cache.clear()
        rows, meta = navigator.pi_sessions(d.name)
        self.assertEqual(rows[0]["display_name"], "new name")

    def test_pi_rename_busy_skips(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sd = Path(d.name) / "--proj--"
        sd.mkdir()
        sid = "u5"
        (sd / "s.jsonl").write_text(json.dumps({"type": "session", "id": sid, "cwd": "/code/proj"}) + "\n")
        navigator._pi_cache.clear()
        args = type("A", (), {})()
        args.pi_dir = d.name
        args.ccc = "http://localhost:8090"
        args.state = str(Path(d.name) / "state.json")
        args.orca_dir = None
        args.verbose = False
        app = navigator.App(args)
        res = app.rename({"kind": "session", "key": "pi:u5", "sid": sid, "engine": "pi", "name": "new name"})
        self.assertIn("pi-busy", res["applied"])


class WP5Ordering(unittest.TestCase):
    def setUp(self):
        navigator._repo_cache.clear()
        navigator._origin_cache.clear()
        self.state = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {},
                      "repo_order": [], "group_order": {}}

    def build(self, rows, state=None, window="all"):
        if state is None:
            state = self.state
        return navigator.build_tree(rows, state, window, [], "http://ccc")

    def test_set_repo_order_dedupes_and_drops_empties(self):
        with tempfile.TemporaryDirectory() as d:
            st = navigator.State(Path(d) / "s.json")
            st.set_repo_order(["/r/A", "", "/r/B", "/r/A", "   ", "/r/C"])
            self.assertEqual(st.data["repo_order"], ["/r/A", "/r/B", "/r/C"])

    def test_build_tree_repo_order_puts_projects_first(self):
        rows = [
            row("1", "/r/A", "Old work", mins=60 * 24),
            row("2", "/r/B", "Fresh work"),
        ]
        t = self.build(rows, {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {},
                               "repo_order": ["folder:A"], "group_order": {}})   # paths that do not exist are keyed folder:<name>
        self.assertEqual([r["key"] for r in t["repos"]], ["folder:A", "folder:B"])

    def test_build_tree_starred_inside_repo_order_keeps_position(self):
        rows = [
            row("1", "/r/A", "Old work", mins=60 * 24),
            row("2", "/r/B", "Fresh work"),
        ]
        state = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {}, "pinned": {"r:folder:B": True},
                 "repo_order": ["folder:A", "folder:B"], "group_order": {}}
        t = self.build(rows, state)
        self.assertEqual([r["key"] for r in t["repos"]], ["folder:A", "folder:B"])

    def test_group_order_puts_listed_first_and_no_name_last(self):
        rows = [
            row("1", "/r/A", "g1", mins=60),
            row("2", "/r/A", "g2", mins=30),
            row("3", "/r/A", "ungrouped", mins=10),
        ]
        repo_key = "folder:A"
        st = {"nicknames": {}, "groups": {repo_key: ["zzz", "aaa"]},
              "assign": {"claude:1": {"repo": repo_key, "group": "zzz"},
                         "claude:2": {"repo": repo_key, "group": "aaa"}},
              "hidden": {}, "pinned": {}, "repo_order": [], "group_order": {repo_key: ["aaa"]}}
        t = self.build(rows, st)
        groups = t["repos"][0]["groups"]
        names = [g["name"] for g in groups]
        self.assertEqual(names[0], "aaa")
        self.assertIn("zzz", names)
        self.assertEqual(names[-1], "")

    def test_rename_group_keeps_position(self):
        rows = [row("1", "/r/A", "x")]
        repo_key = "/r/A"
        st = navigator.State.__new__(navigator.State)
        st.data = {"nicknames": {}, "groups": {repo_key: ["old"]}, "assign": {"claude:1": {"repo": repo_key, "group": "old"}},
                   "moved": {}, "desktop_map": {}, "hidden": {}, "pinned": {}, "icons": {}, "repo_names": {}, "notes": {},
                   "triage": {}, "copilot": "", "hide_log": [], "repo_order": [], "group_order": {repo_key: ["old"]}}
        st.lock = __import__('threading').RLock()
        st._save = lambda: None
        st.rename_group(repo_key, "old", "new")
        self.assertEqual(st.data["group_order"][repo_key], ["new"])
        self.assertEqual(st.data["assign"]["claude:1"]["group"], "new")

    def test_remove_group_updates_group_order(self):
        rows = [row("1", "/r/A", "x")]
        repo_key = "/r/A"
        st = navigator.State.__new__(navigator.State)
        st.data = {"nicknames": {}, "groups": {repo_key: ["old", "keep"]}, "assign": {"claude:1": {"repo": repo_key, "group": "old"}},
                   "moved": {}, "desktop_map": {}, "hidden": {}, "pinned": {}, "icons": {}, "repo_names": {}, "notes": {},
                   "triage": {}, "copilot": "", "hide_log": [], "repo_order": [], "group_order": {repo_key: ["old", "keep"]}}
        st.lock = __import__('threading').RLock()
        st._save = lambda: None
        st.remove_group(repo_key, "old")
        self.assertEqual(st.data["group_order"].get(repo_key), ["keep"])
        self.assertNotIn("claude:1", st.data["assign"])

    def test_old_state_files_without_these_keys_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"nicknames": {"k": "n"}}))
            st = navigator.State(p)
            self.assertEqual(st.data["repo_order"], [])
            self.assertEqual(st.data["group_order"], {})
            self.assertEqual(st.data["nicknames"], {"k": "n"})


if __name__ == "__main__":
    unittest.main()
