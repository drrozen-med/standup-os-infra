import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import navigator  # noqa: E402
import triage  # noqa: E402

NOW = 2_000_000_000


def item(**kw):
    base = {"key": "claude:x", "mtime": NOW - 600, "live": False, "needs_you": False}
    base.update(kw)
    return base


def said(text):
    return [("user_text", "go"), ("assistant", text)]


class ClassifyTests(unittest.TestCase):
    def label(self, it, msgs):
        return triage.classify(it, msgs, NOW)[0]

    def test_state_and_flagged_win(self):
        self.assertEqual(self.label(item(live=True, state="working"), []), "working")
        self.assertEqual(self.label(item(needs_you=True), []), "needs-you")

    def test_live_alone_does_not_mean_working(self):
        self.assertEqual(self.label(item(live=True, state="resting"), []), "idle")
        self.assertEqual(self.label(item(live=True), []), "idle")

    def test_question_needs_you_and_reason_is_the_question(self):
        label, reason = triage.classify(item(), said("Done with A.\nShould I also do B?\nNotes below."), NOW)
        self.assertEqual(label, "needs-you")
        self.assertEqual(reason, "Should I also do B?")

    def test_blocked_but_not_on_negations(self):
        self.assertEqual(self.label(item(), said("I'm blocked on the Cloudflare token.")), "blocked")
        self.assertEqual(self.label(item(), said("No verification failed at any step.")), "idle")

    def test_done_but_not_when_negated(self):
        self.assertEqual(self.label(item(), said("All 34 tests pass. Merged.")), "done")
        self.assertEqual(self.label(item(), said("The PR was opened and left open, not merged.")), "idle")

    def test_user_spoke_last_is_not_read_as_agent_text(self):
        self.assertEqual(self.label(item(), [("assistant", "Should I go?"), ("user_text", "yes")]), "idle")

    def test_stale_after_three_days(self):
        self.assertEqual(self.label(item(mtime=NOW - 4 * 86400), []), "stale")


class RunPassTests(unittest.TestCase):
    def test_true_mtime_controls_transcript_rereads(self):
        session = item(sid="x", engine="claude", live=True, state="resting")
        tree = {"repos": [{"key": "/r", "groups": [{"items": [session]}]}]}

        class Client:
            def __init__(self):
                self.reads = 0

            def _request(self, path):
                if path.startswith("/api/tree"):
                    return tree
                self.reads += 1
                return {"events": [{"type": "assistant", "text": "Done."}]}

            def post(self, path, body):
                return {"changed": 1}

        client = Client()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(triage, "CACHE", Path(d) / "cache.json"):
            triage.run_pass(client, now=NOW)
            triage.run_pass(client, now=NOW)
            self.assertEqual(client.reads, 1)
            session["mtime"] += 1
            triage.run_pass(client, now=NOW)
            self.assertEqual(client.reads, 2)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = navigator.State(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_notes_append_remove_and_persist(self):
        self.state.add_note("claude:a", "first")
        self.state.add_note("claude:a", "second", by="copilot")
        self.assertIsNone(self.state.add_note("claude:a", "  "))
        self.assertTrue(self.state.remove_note("claude:a", 0))
        self.assertFalse(self.state.remove_note("claude:a", 5))
        saved = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual([n["text"] for n in saved["notes"]["claude:a"]], ["second"])
        self.assertEqual(saved["notes"]["claude:a"][0]["by"], "copilot")

    def test_owner_label_beats_sorter_until_cleared(self):
        self.state.set_triage({"claude:a": {"label": "done", "by": "you"}})
        self.assertEqual(self.state.set_triage({"claude:a": {"label": "idle", "by": "sorter"}}), 0)
        self.assertEqual(self.state.snapshot()["triage"]["claude:a"]["label"], "done")
        self.state.set_triage({"claude:a": {"label": "", "by": "you"}})
        self.state.set_triage({"claude:a": {"label": "idle", "by": "sorter"}})
        self.assertEqual(self.state.snapshot()["triage"]["claude:a"]["label"], "idle")

    def test_hide_log_and_undo(self):
        eid = self.state.set_hidden(["claude:a", "g:/p|ws"], True, "2 sessions")
        self.assertTrue(eid)
        self.assertIsNone(self.state.set_hidden(["claude:a"], False))  # unhides are not logged
        self.state.set_hidden(["claude:a"], True, "a")
        snap = self.state.snapshot()
        self.assertEqual([e["label"] for e in snap["hide_log"]], ["2 sessions", "a"])
        entry = self.state.undo_hide(eid)
        self.assertEqual(entry["keys"], ["claude:a", "g:/p|ws"])
        self.assertNotIn("g:/p|ws", self.state.snapshot()["hidden"])
        self.assertIsNone(self.state.undo_hide(eid))       # only once
        self.assertIsNone(self.state.undo_hide("nope"))

    def test_hide_log_is_capped(self):
        for i in range(navigator.HIDE_LOG_MAX + 5):
            self.state.set_hidden(["k%d" % i], True)
        log = self.state.snapshot()["hide_log"]
        self.assertEqual(len(log), navigator.HIDE_LOG_MAX)
        self.assertEqual(log[-1]["keys"], ["k%d" % (navigator.HIDE_LOG_MAX + 4)])

    def test_unchanged_label_keeps_its_timestamp(self):
        self.state.set_triage({"claude:a": {"label": "idle", "reason": "r1"}})
        ts = self.state.snapshot()["triage"]["claude:a"]["ts"]
        time.sleep(0.01)
        self.state.set_triage({"claude:a": {"label": "idle", "reason": "r2"}})
        self.assertEqual(self.state.snapshot()["triage"]["claude:a"]["ts"], ts)


if __name__ == "__main__":
    unittest.main()
