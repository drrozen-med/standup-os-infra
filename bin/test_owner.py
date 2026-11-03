"""Tests for bin/owner.py: status validation (rule 1 of #79), atomic write, log, brief, CLI.

unittest only, against a temp OWNER_DIR.
Run: python3 bin/test_owner.py
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("owner", os.path.join(HERE, "owner.py"))
spec = importlib.util.spec_from_loader("owner", loader)
owner = importlib.util.module_from_spec(spec)
loader.exec_module(owner)


def good(**kw):
    s = {"state": "working", "summary": "CDL closed test: 3 of 5 testers onboarded",
         "doing": "fixing the Android install step (#212)", "next": "merge #212 after review",
         "shipped": ["merged #210"], "needs_uri": [], "blocked": None}
    s.update(kw)
    return s


BLOCKED = {"what": "Play Console upload", "tried": ["gh workflow", "fastlane locally"],
           "unblocker": "uri", "by": "2026-11-05"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"OWNER_DIR": self.tmp.name})
        self.env.start()
        os.environ.pop("OWNER_SESSION", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = owner.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def reasons(self, obj):
        clean, reasons = owner.validate(obj)
        self.assertIsNone(clean)
        return "\n".join(reasons)


class Validation(Base):
    def test_good_status_passes(self):
        clean, reasons = owner.validate(good())
        self.assertEqual(reasons, [])
        self.assertEqual(clean["state"], "working")

    def test_every_missing_field_is_named(self):
        r = self.reasons({})
        for f in owner.FIELDS:
            self.assertIn("missing field: %s" % f, r)

    def test_bad_state(self):
        self.assertIn("state must be one of", self.reasons(good(state="idle")))

    def test_empty_text_fields(self):
        r = self.reasons(good(summary=" ", doing="", next=None))
        for f in ("summary", "doing", "next"):
            self.assertIn("%s is empty" % f, r)

    def test_summary_max_120(self):
        self.assertEqual(owner.validate(good(summary="x" * 120))[1], [])
        self.assertIn("summary is 121 chars", self.reasons(good(summary="x" * 121)))

    def test_lists_must_be_lists(self):
        r = self.reasons(good(shipped="merged #1", needs_uri="d1"))
        self.assertIn("shipped must be a list", r)
        self.assertIn("needs_uri must be a list", r)

    def test_passive_phrases_rejected_in_summary_and_doing(self):
        for phrase in owner.PASSIVE:
            r = self.reasons(good(summary="Today: %s here" % phrase.upper()))
            self.assertIn("summary reads as passive", r, phrase)
            self.assertIn("doing reads as passive", self.reasons(good(doing="well, %s" % phrase)), phrase)
        self.assertIn("passive", self.reasons(good(summary="I can\u2019t merge it")))   # curly apostrophe
        self.assertEqual(owner.validate(good(next="still waiting is fine in next"))[1], [])   # only summary/doing

    def test_passive_allowed_when_properly_blocked(self):
        clean, reasons = owner.validate(good(state="blocked", summary="stuck on Play Console upload",
                                             blocked=BLOCKED))
        self.assertEqual(reasons, [])
        self.assertEqual(clean["blocked"]["unblocker"], "uri")

    def test_blocked_needs_two_different_tries_and_the_rest(self):
        r = self.reasons(good(state="blocked", blocked=dict(BLOCKED, tried=["gh workflow"])))
        self.assertIn("at least 2 different ways", r)
        r = self.reasons(good(state="blocked", blocked=dict(BLOCKED, tried=["Retry", " retry "])))
        self.assertIn("at least 2 different ways you tried (has 1)", r)
        r = self.reasons(good(state="blocked", blocked={"tried": ["a", "b"]}))
        for part in ("blocked.what", "blocked.unblocker", "blocked.by"):
            self.assertIn(part, r)
        self.assertIn("blocked.by must be a date", self.reasons(good(state="blocked", blocked=dict(BLOCKED, by="Friday"))))
        self.assertIn("not an object", self.reasons(good(state="blocked", blocked=None)))

    def test_blocked_with_too_few_tries_still_cannot_be_passive(self):
        r = self.reasons(good(state="blocked", summary="stuck", blocked=dict(BLOCKED, tried=["once"])))
        self.assertIn("summary reads as passive", r)

    def test_waiting_on_uri_needs_decision_ids(self):
        self.assertIn("needs_uri is empty", self.reasons(good(state="waiting-on-uri")))
        self.assertIn("needs_uri is empty", self.reasons(good(state="waiting-on-uri", needs_uri=["  "])))
        self.assertEqual(owner.validate(good(state="waiting-on-uri", needs_uri=["cdl-price"]))[1], [])

    def test_blocked_is_dropped_unless_state_is_blocked(self):
        clean, _ = owner.validate(good(blocked=BLOCKED))
        self.assertIsNone(clean["blocked"])


class StatusFile(Base):
    def test_set_writes_status_and_logs(self):
        code, out, _ = self.cli("status", "set", "cdl", "--session", "claude:abc", "--json", json.dumps(good()))
        self.assertEqual(code, 0)
        with open(self.path("cdl", "status.json")) as f:
            st = json.load(f)
        self.assertEqual(st["session"], "claude:abc")
        self.assertRegex(st["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(json.loads(out), st)
        with open(self.path("cdl", "log.md")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^- \d{4}-\d\d-\d\d \d\d:\d\d \[owner\] working: CDL closed test: 3 of 5")

    def test_reject_writes_nothing_and_prints_every_reason(self):
        self.cli("status", "set", "cdl", "--json", json.dumps(good()))
        with open(self.path("cdl", "status.json")) as f:
            before = f.read()
        code, out, err = self.cli("status", "set", "cdl", "--json", json.dumps(good(summary="nothing changed", next="")))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("rejected, nothing written", err)
        self.assertIn("next is empty", err)
        self.assertIn("summary reads as passive", err)
        with open(self.path("cdl", "status.json")) as f:
            self.assertEqual(f.read(), before)
        with open(self.path("cdl", "log.md")) as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_bad_json_is_a_reject(self):
        code, _, err = self.cli("status", "set", "cdl", "--json", "{nope")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertFalse(os.path.exists(self.path("cdl")))

    def test_json_from_stdin_and_session_from_env_or_previous(self):
        with mock.patch.dict(os.environ, {"OWNER_SESSION": "claude:env"}):
            self.assertEqual(self.cli("status", "set", "cdl", "--json", "-", stdin=json.dumps(good()))[0], 0)
        self.cli("status", "set", "cdl", "--json", json.dumps(good()))
        with open(self.path("cdl", "status.json")) as f:
            self.assertEqual(json.load(f)["session"], "claude:env")      # kept from the previous status

    def test_write_is_atomic(self):
        self.cli("status", "set", "cdl", "--json", json.dumps(good()))
        with mock.patch.object(owner.json, "dumps", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                owner.set_status("cdl", good(summary="second"))
        with open(self.path("cdl", "status.json")) as f:
            self.assertEqual(json.load(f)["summary"], good()["summary"])
        real = owner.os.replace
        with mock.patch.object(owner.os, "replace", side_effect=OSError("crash before rename")):
            with self.assertRaises(OSError):
                owner.set_status("cdl", good(summary="third"))
        self.assertIs(owner.os.replace, real)
        self.assertEqual(sorted(os.listdir(self.path("cdl"))), ["log.md", "status.json"])   # no temp file left
        with open(self.path("cdl", "status.json")) as f:
            self.assertEqual(json.load(f)["summary"], good()["summary"])   # old file intact, never half written

    def test_show(self):
        self.assertEqual(self.cli("status", "show", "cdl")[0], 1)
        self.cli("status", "set", "cdl", "--json", json.dumps(good()))
        code, out, _ = self.cli("status", "show", "cdl")
        self.assertEqual((code, json.loads(out)["state"]), (0, "working"))


class LogAndBrief(Base):
    def test_log_line_format_and_actor(self):
        code, out, _ = self.cli("log", "cdl", "--actor", "supervisor", "merged", "#5\n and  #6")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^- \d{4}-\d\d-\d\d \d\d:\d\d \[supervisor\] merged #5 and #6$")
        self.assertEqual(self.cli("log", "cdl", "--actor", "Bad Actor!", "x")[0], 2)

    def test_slug_is_the_basename_of_the_project_key(self):
        self.assertEqual(owner.slug_of("/Users/u/projects/boardsBridge"), "boardsBridge")
        self.assertEqual(owner.slug_of("/Users/u/projects/boardsBridge/"), "boardsBridge")
        self.assertEqual(owner.slug_of("folder:cdl"), "cdl")
        self.assertEqual(owner.slug_of("desktop:CFO, Books"), "CFO__Books")
        self.assertEqual(owner.slug_of("../../etc"), "etc")

    def test_brief_has_goals_status_scoreboard_log_and_command(self):
        os.makedirs(self.path("cdl"))
        with open(self.path("cdl", "goals.md"), "w") as f:
            f.write("CDL closed test running by Nov 15\n")
        with open(self.path("cdl", "scoreboard.json"), "w") as f:
            json.dump({"repo": "cdl", "open_prs": 7}, f)
        for i in range(12):
            owner.append_log("cdl", "supervisor", "entry %02d" % i)
        owner.set_status("cdl", good(), session="claude:abc")
        b = owner.brief("cdl")
        self.assertIn("CDL closed test running by Nov 15", b)
        self.assertIn('"open_prs": 7', b)
        self.assertIn(good()["summary"], b)
        self.assertNotIn("entry 02", b)          # only the last 10 log lines (11 entries + the status line)
        self.assertIn("entry 03", b)
        self.assertIn("entry 11", b)
        self.assertIn("python3 ~/projects/standup-os/bin/owner.py status set cdl --session claude:abc --json -", b)
        for word in ("No passive status", "Knock until released", "gh", "orca", "Outsourcerer", "nav.py",
                     "brain search --query", "review every change"):
            self.assertIn(word, b)

    def test_brief_without_files(self):
        code, out, _ = self.cli("brief", "newproj")
        self.assertEqual(code, 0)
        self.assertIn("No goals.md yet", out)
        self.assertIn("No status yet", out)
        self.assertIn("status set newproj --json -", out)

    def test_ls(self):
        self.cli("status", "set", "cdl", "--json", json.dumps(good()))
        os.makedirs(self.path("aba"))
        code, out, _ = self.cli("ls", "--json")
        rows = json.loads(out)
        self.assertEqual([(r["slug"], r["state"]) for r in rows], [("aba", None), ("cdl", "working")])


if __name__ == "__main__":
    unittest.main()
