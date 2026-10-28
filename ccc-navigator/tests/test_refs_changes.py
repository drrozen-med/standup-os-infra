import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import changes  # noqa: E402
import refs  # noqa: E402
from refs import Ref  # noqa: E402

TREE = {"repos": [
    {"key": "/code/app", "name": "App", "path": "~/code/app", "groups": [
        {"name": "infra", "items": [{"key": "claude:1", "nick": "Billing fix", "original": "x"}]},
        {"name": "", "items": [{"key": "codex:2", "nick": "", "original": "Docs pass"}]},
    ]},
    {"key": "/code/app-web", "name": "Web", "path": "~/code/app-web", "groups": []},
]}


class Refs(unittest.TestCase):
    def test_round_trip_every_kind(self):
        for ref in (Ref("session", "claude:1"), Ref("session", "batch:ab12"),
                    Ref("group", "/code/a|b", "x|y z"), Ref("group", "/code/app", ""),
                    Ref("project", "/code/app"), Ref("project", "devin:Lane 1")):
            self.assertEqual(refs.parse_ref(refs.format_ref(ref)), ref)

    def test_legacy_forms(self):
        self.assertEqual(refs.parse_ref("claude:1"), Ref("session", "claude:1"))
        self.assertEqual(refs.parse_ref("g:/code/app|infra"), Ref("group", "/code/app", "infra"))
        self.assertEqual(refs.parse_ref("r:/code/app"), Ref("project", "/code/app"))
        link = "http://127.0.0.1:8095/#g=" + "g%3A%2Fcode%2Fapp%7Cinfra" + "&p=%2Fx"
        self.assertEqual(refs.parse_ref(link), Ref("group", "/code/app", "infra"))
        self.assertEqual(refs.parse_ref("local_ab12cd34-0000", {"local_ab12cd34-0000": "u1"}), Ref("session", "claude:u1"))

    def test_bad_refs(self):
        for bad, code in (("", "bad_ref"), ("hello world", "bad_ref"), ("nav://group/onlyproject", "bad_ref"),
                          ("nav://thing/x", "bad_ref"), ("g:noproject", "bad_ref"), ("x" * 3000, "bad_ref"),
                          ("local_ab12cd34-0000", "unknown_desktop_id")):
            with self.assertRaises(refs.RefError) as cm:
                refs.parse_ref(bad)
            self.assertEqual(cm.exception.code, code, bad)

    def test_resolve_is_exact(self):
        self.assertEqual([t["key"] for t in refs.resolve(Ref("session", "claude:1"), TREE)], ["claude:1"])
        self.assertEqual(len(refs.resolve(Ref("project", "/code/app"), TREE)), 1)    # not app-web too
        self.assertEqual(refs.resolve(Ref("group", "/code/app", ""), TREE)[0]["ref"], "nav://group/%2Fcode%2Fapp|")
        self.assertEqual(refs.resolve(Ref("session", "claude:9"), TREE), [])

    def test_find_returns_candidates(self):
        self.assertEqual({t["kind"] for t in refs.find("app", TREE)}, {"project"})
        self.assertEqual([t["key"] for t in refs.find("billing", TREE)], ["claude:1"])
        self.assertEqual(len(refs.find("app", TREE, limit=1)), 1)
        self.assertEqual(refs.find("", TREE), [])


class Changes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = changes.ChangeLog(Path(self.tmp.name) / "changes.jsonl")
        self.state = {"nicknames": {"claude:1": "old"}}

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, s, k):
        return self.state.get(s, {}).get(k)

    def write(self, fields):
        for s, kv in fields.items():
            for k, v in kv.items():
                if v is None:
                    self.state.setdefault(s, {}).pop(k, None)
                else:
                    self.state.setdefault(s, {})[k] = v

    def rename(self, new, actor="you"):
        before = {"nicknames": {"claude:1": self.read("nicknames", "claude:1")}}
        self.write({"nicknames": {"claude:1": new}})
        return self.log.record(actor, "session.rename", {"key": "claude:1", "name": new}, before,
                               {"nicknames": {"claude:1": new}})

    def test_record_recent_and_file_mode(self):
        self.rename("a", actor="copilot")
        self.log.record("titler", "session.rename", {"key": "claude:2"}, {}, {"nicknames": {"claude:2": "t"}})
        self.assertEqual([c["actor"] for c in self.log.recent()], ["titler", "copilot"])
        self.assertEqual([c["actor"] for c in self.log.recent(actor="copilot")], ["copilot"])
        self.assertEqual(len(self.log.recent(key="claude:2")), 1)
        self.assertEqual(os.stat(self.log.path).st_mode & 0o777, 0o600)

    def test_undo_restores_and_only_once(self):
        cid = self.rename("new")
        uid = self.log.undo(cid, "you", self.read, self.write)
        self.assertEqual(self.state["nicknames"]["claude:1"], "old")
        self.assertEqual(self.log.get(uid)["action"], "undo")
        self.assertTrue(self.log.get(cid)["undone"])
        with self.assertRaises(changes.ChangeError) as cm:
            self.log.undo(cid, "you", self.read, self.write)
        self.assertEqual(cm.exception.code, "already_undone")

    def test_undo_absent_value_removes_key(self):
        self.state = {}
        cid = self.rename("first")
        self.log.undo(cid, "you", self.read, self.write)
        self.assertNotIn("claude:1", self.state["nicknames"])

    def test_conflict_names_the_later_change(self):
        first = self.rename("a")
        second = self.rename("b", actor="copilot")
        with self.assertRaises(changes.ChangeError) as cm:
            self.log.undo(first, "you", self.read, self.write)
        self.assertEqual((cm.exception.code, cm.exception.later), ("conflict", second))
        self.assertEqual(self.state["nicknames"]["claude:1"], "b")       # untouched

    def test_guards(self):
        with self.assertRaises(changes.ChangeError) as cm:
            self.log.record("Bad Actor!", "x", {}, {}, {})
        self.assertEqual(cm.exception.code, "bad_actor")
        with self.assertRaises(changes.ChangeError):
            self.log.record("you", "x", {}, {"nicknames": "flat"}, {})
        sid = self.log.record("you", "session.send", {"text": "y" * 500}, {}, {}, undoable=False)
        self.assertEqual(len(self.log.get(sid)["params"]["text"]), changes.SEND_TEXT_MAX)
        for cid, code in ((sid, "not_undoable"), ("cnope", "unknown_change")):
            with self.assertRaises(changes.ChangeError) as cm:
                self.log.undo(cid, "you", self.read, self.write)
            self.assertEqual(cm.exception.code, code)

    def test_rotation_keeps_history_readable(self):
        log = changes.ChangeLog(Path(self.tmp.name) / "r.jsonl", max_bytes=300, keep=2)
        ids = [log.record("you", "a", {"i": i}, {}, {"pinned": {"k%d" % i: True}}) for i in range(12)]
        self.assertTrue(Path(self.tmp.name, "r.jsonl.1").exists())
        self.assertFalse(Path(self.tmp.name, "r.jsonl.3").exists())
        self.assertEqual(log.recent(limit=1)[0]["id"], ids[-1])
        self.assertIsNone(log.get(ids[0]))       # oldest rotated away

    def test_bad_lines_are_skipped(self):
        cid = self.rename("x")
        with open(self.log.path, "a") as fh:
            fh.write("not json\n")
        self.assertEqual(self.log.recent()[0]["id"], cid)


if __name__ == "__main__":
    unittest.main()
