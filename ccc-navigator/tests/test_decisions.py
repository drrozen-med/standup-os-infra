import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import decisions  # noqa: E402
from decisions import DecisionError  # noqa: E402

DAY = 86400


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.wt = d / "wt.json"
        self.wt.write_text(json.dumps([
            {"id": "aba-price", "lane": "aba", "priority": 1, "question": "Which price?", "options": ["$349", "$79", "other"],
             "deadline": time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * DAY)), "status": "open"},
            {"id": "later-thing", "lane": "x", "priority": 3, "question": "Later?", "options": ["yes", "no"], "status": "open"},
            {"id": "closed-one", "priority": 1, "question": "Old", "options": ["a"], "status": "done"},
            {"id": "Bad Id!", "question": "skip me", "options": ["a"]},
        ]))
        self.inbox = decisions.Inbox(d / "own.json", d / "answers.json", self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_sorts_and_skips_closed_and_bad(self):
        open_ = self.inbox.list()["open"]
        self.assertEqual([x["id"] for x in open_], ["aba-price", "later-thing"])
        self.assertEqual(open_[0]["days_left"], 3)
        self.assertEqual(open_[0]["origin"], "watchtower")

    def test_answer_moves_to_answered_and_reopen(self):
        rec, before = self.inbox.answer("aba-price", "$79", by="you")
        self.assertIsNone(before)
        lst = self.inbox.list()
        self.assertEqual([x["id"] for x in lst["open"]], ["later-thing"])
        self.assertEqual(lst["answered"][0]["answer"]["choice"], "$79")
        self.assertEqual(json.loads(self.wt.read_text())[0]["status"], "open")     # watchtower file untouched
        self.inbox.reopen("aba-price")
        self.assertEqual(len(self.inbox.list()["open"]), 2)
        with self.assertRaises(DecisionError):
            self.inbox.reopen("aba-price")

    def test_answer_validation(self):
        for args, code in ((("nope", "x"), "unknown_decision"), (("aba-price", "$5"), "bad_choice"),
                           (("aba-price", "other"), "bad_choice"), (("later-thing", ""), "bad_choice")):
            with self.assertRaises(DecisionError) as cm:
                self.inbox.answer(*args)
            self.assertEqual(cm.exception.code, code, args)
        rec, _ = self.inbox.answer("later-thing", "other", note="next month")      # other always allowed with a note
        self.assertEqual((rec["choice"], rec["note"]), ("other", "next month"))

    def test_ask_adds_to_own_queue(self):
        d = self.inbox.ask("Deploy CRM now?", ["deploy", "hold"], lane="gtm", priority=5, session="claude:abc", by="copilot")
        self.assertEqual(d["priority"], 3)
        self.assertIn(d["id"], [x["id"] for x in self.inbox.list()["open"]])
        self.assertEqual(self.inbox.get(d["id"])["origin"], "navigator")
        rec, _ = self.inbox.answer(d["id"], "hold")
        msg = decisions.message_for(self.inbox.get(d["id"]), rec)
        self.assertIn("hold", msg)
        self.assertIn("Deploy CRM now?", msg)

    def test_ask_validation(self):
        for kw, code in (({"question": "hi", "options": ["a"]}, "bad_question"),
                         ({"question": "A real question?", "options": []}, "bad_options"),
                         ({"question": "A real question?", "options": ["a", "a"]}, "bad_options"),
                         ({"question": "A real question?", "options": ["a"], "deadline": "soon"}, "bad_deadline"),
                         ({"question": "A real question?", "options": ["a"], "decision_id": "Bad Id"}, "bad_id"),
                         ({"question": "A real question?", "options": ["a"], "decision_id": "aba-price"}, "exists")):
            with self.assertRaises(DecisionError) as cm:
                self.inbox.ask(**kw)
            self.assertEqual(cm.exception.code, code, kw)

    def test_missing_watchtower_file_is_fine(self):
        inbox = decisions.Inbox(Path(self.tmp.name) / "o.json", Path(self.tmp.name) / "a.json", Path(self.tmp.name) / "none.json")
        self.assertEqual(inbox.list(), {"open": [], "answered": []})

    def test_set_ranks_behavior(self):
        inbox = decisions.Inbox(Path(self.tmp.name) / "own.json", Path(self.tmp.name) / "answers.json", self.wt)
        rec, before = inbox.set_ranks(["aba-price", "later-thing"], why={"aba-price": "x" * 200, "later-thing": "ok"})
        self.assertEqual(rec["ids"], ["aba-price", "later-thing"])
        self.assertEqual(len(rec["why"]["aba-price"]), 140)
        self.assertEqual(rec["why"]["later-thing"], "ok")
        self.assertIsInstance(before, dict)

        rec2, _ = inbox.set_ranks(["unknown", "aba-price"], why={})
        self.assertEqual(rec2["ids"], ["aba-price"])

        rec3, _ = inbox.set_ranks(["aba-price", "later-thing", "aba-price"])
        self.assertEqual(rec3["ids"], ["aba-price", "later-thing"])

        # cap at 7
        many = [f"d{i}" for i in range(10)]
        for mid in many:
            inbox.ask(f"Question {mid} now?", ["yes", "no"], decision_id=mid)
        rec4, _ = inbox.set_ranks(many)
        self.assertEqual(len(rec4["ids"]), 7)

        # drops answered ids
        inbox.answer("d0", "yes")
        rec5, _ = inbox.set_ranks(["d0", "d1"])
        self.assertEqual(rec5["ids"], ["d1"])

    def test_clear_ranks_empties(self):
        inbox = decisions.Inbox(Path(self.tmp.name) / "own.json", Path(self.tmp.name) / "answers.json", self.wt)
        inbox.set_ranks(["aba-price"])
        rec, before = inbox.clear_ranks()
        self.assertEqual(rec["ids"], [])
        self.assertEqual(rec["why"], {})
        self.assertIsInstance(before, dict)

    def test_list_ranked_order_and_fields(self):
        inbox = decisions.Inbox(Path(self.tmp.name) / "own.json", Path(self.tmp.name) / "answers.json", self.wt)
        inbox.set_ranks(["later-thing", "aba-price"], why={"later-thing": "do first", "aba-price": "also ok"})
        lst = inbox.list()
        open_ = lst["open"]
        self.assertEqual([x["id"] for x in open_], ["later-thing", "aba-price"])
        self.assertEqual(open_[0]["rank"], 1)
        self.assertEqual(open_[0]["why"], "do first")
        self.assertEqual(open_[1]["rank"], 2)
        self.assertEqual(open_[1]["why"], "also ok")

    def test_answered_ranked_drops_out_of_open(self):
        inbox = decisions.Inbox(Path(self.tmp.name) / "own.json", Path(self.tmp.name) / "answers.json", self.wt)
        inbox.set_ranks(["aba-price", "later-thing"])
        inbox.answer("aba-price", "$79")
        lst = inbox.list()
        open_ = lst["open"]
        self.assertEqual([x["id"] for x in open_], ["later-thing"])
        self.assertEqual(open_[0].get("rank"), 2)


if __name__ == "__main__":
    unittest.main()
