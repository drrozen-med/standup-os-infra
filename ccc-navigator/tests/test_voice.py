"""Ride mode: text for the ear, command parsing, the briefing, and what is held for a tap."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice  # noqa: E402


def tree(*items, repo="BoardsBridge"):
    return {"repos": [{"name": repo, "orig_name": repo.lower(), "groups": [{"name": "main", "items": list(items)}]}]}


def item(key, title, state="needs-you", mtime=1.0, engine="claude", **kw):
    return dict(key=key, original=title, nick="", state=state, mtime=mtime, engine=engine, **kw)


class SpokenTests(unittest.TestCase):
    def test_drops_code_paths_links_and_hashes(self):
        s = voice.spoken("**TL;DR:** Fixed in `navigator.py:42` (PR #55, commit 98b0ee9).\n"
                         "```bash\nls -la\n```\nSee https://example.com and docs/x.md.")
        self.assertTrue(s.startswith("Fixed"), s)
        for junk in ("navigator.py", "```", "ls -la", "98b0ee9", "https", "docs/", "**", "TL;DR"):
            self.assertNotIn(junk, s)

    def test_keeps_plain_words_in_backticks(self):
        self.assertIn("yes", voice.spoken("Reply `yes` to go on."))

    def test_branch_names_and_issue_numbers_are_not_read(self):
        s = voice.spoken("The code (feat/dodo-entitlement) is on staging (#3086).")
        self.assertNotIn("feat", s)
        self.assertNotIn("3086", s)

    def test_long_text_cut_at_a_sentence(self):
        s = voice.spoken("One sentence here. " * 100, limit=100)
        self.assertLessEqual(len(s), 100)
        self.assertTrue(s.endswith("."))

    def test_hebrew_and_english_split_by_sentence(self):
        runs = voice.by_language("Zlotnik is waiting. אל תשלח את הטיוטה. Say yes or no.")
        self.assertEqual([r[0] for r in runs], ["en", "he", "en"])


class ParseTests(unittest.TestCase):
    def test_commands(self):
        for said, intent in [("yes", "yes"), ("Yeah.", "yes"), ("no thanks", "no"), ("later", "later"),
                             ("what?", "repeat"), ("next", "next"), ("tell me more", "more"), ("cancel", "cancel"),
                             ("כן", "yes"), ("לא", "no")]:
            self.assertEqual(voice.parse(said)["intent"], intent, said)

    def test_options_by_number(self):
        self.assertEqual(voice.parse("option two"), {"intent": "option", "n": 2})

    def test_talk_and_tell(self):
        self.assertEqual(voice.parse("talk to boardsbridge"), {"intent": "talk", "target": "boardsbridge"})
        p = voice.parse("Tell boards bridge to hold the deploy until tomorrow.")
        self.assertEqual((p["intent"], p["target"]), ("tell", "boards bridge"))
        self.assertEqual(p["text"], "hold the deploy until tomorrow.")

    def test_long_speech_is_a_message(self):
        p = voice.parse("Merge it after you rerun the tests and tell me")
        self.assertEqual(p["intent"], "message")


class SafetyTests(unittest.TestCase):
    def test_hard_to_undo_answers_are_held(self):
        self.assertTrue(voice.needs_confirm("Buy Cloudflare Workers Paid ($5/month)?", "yes"))
        self.assertTrue(voice.needs_confirm("Promote staging to production?", "yes, promote"))
        self.assertFalse(voice.needs_confirm("Rename the group to Infra?", "yes"))

    def test_no_and_later_are_always_safe(self):
        self.assertFalse(voice.needs_confirm("Deploy to production?", "no"))
        self.assertFalse(voice.needs_confirm("Deploy to production?", "later"))


class BriefingTests(unittest.TestCase):
    def test_decisions_then_waiting_sessions(self):
        t = tree(item("claude:a", "Speakers page", mtime=2), item("claude:b", "Old one", state="idle"))
        inbox = {"open": [{"id": "d1", "question": "Buy the domain?", "options": ["yes", "no"], "rank": 1}]}
        b = voice.briefing(t, inbox, lambda k: "**TL;DR:** Ready to put it live. Then more detail.")
        self.assertEqual([i["kind"] for i in b["items"]], ["decision", "session"])
        self.assertTrue(b["items"][0]["confirm"])
        self.assertTrue(b["items"][0]["say"].startswith("One. Buy the domain?"))
        self.assertIn("Ready to put it live.", b["items"][1]["say"])
        self.assertNotIn("Then more detail", b["items"][1]["say"])
        self.assertIn("2 things need you", b["intro"])

    def test_all_clear(self):
        b = voice.briefing(tree(), {"open": []})
        self.assertEqual(b["items"], [])
        self.assertIn("All clear", b["intro"])

    def test_single_option_decision(self):
        b = voice.briefing(tree(), {"open": [{"id": "d", "question": "Install the app.", "options": ["done"]}]})
        self.assertIn("Say done when it's done", b["items"][0]["say"])


class FindTests(unittest.TestCase):
    def test_spoken_name_with_spaces_or_typos(self):
        t = tree(item("claude:a", "Speakers page", mtime=5), item("claude:z", "zlotnik", mtime=1))
        self.assertEqual(voice.find_session("boards bridge", t)[0], "claude:a")
        self.assertEqual(voice.find_session("bord's bridge", t)[0], "claude:a")
        self.assertEqual(voice.find_session("zlotnik", t)[0], "claude:z")

    def test_background_jobs_are_not_talk_targets(self):
        t = tree(item("outsourcerer:j", "zlotnik job", engine="outsourcerer", mtime=9))
        self.assertIsNone(voice.find_session("zlotnik job", t))

    def test_unknown_name(self):
        self.assertIsNone(voice.find_session("qwerty", tree(item("claude:a", "Speakers page"))))


if __name__ == "__main__":
    unittest.main()
