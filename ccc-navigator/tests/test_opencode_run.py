"""The Navigator's OpenCode adapter: deliver to a session, and read it back.

CCC reads the legacy `session` table of OpenCode's store, so it cannot see a
current session (it answers "repo_required"). The navigator talks to OpenCode
directly instead (opencode_run.py), the same way it already talks to Pi.
"""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

import opencode_run

MODULE = Path(__file__).resolve().parent.parent / "opencode_run.py"


def make_store(path):
    """A tiny OpenCode-shaped store: session_v2 + session_message."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, title TEXT, "
                "time_created INTEGER, time_updated INTEGER)")
    con.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
                "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
    con.execute("INSERT INTO session_v2 VALUES ('ses_abc12345', '/tmp/work', 'A session', 1, 2)")
    con.execute("INSERT INTO session_message VALUES ('m1', 'ses_abc12345', 'user', 1, 10, 10, ?)",
                ('{"time":{"created":10},"content":[{"type":"text","text":"hello there"}]}',))
    con.execute("INSERT INTO session_message VALUES ('m2', 'ses_abc12345', 'assistant', 2, 11, 11, ?)",
                ('{"time":{"created":11},"content":[{"type":"reasoning","text":"thinking"},'
                 '{"type":"text","text":"hi back"}]}',))
    con.execute("INSERT INTO session_message VALUES ('m3', 'ses_abc12345', 'tool', 3, 12, 12, ?)",
                ('{"time":{"created":12},"content":[{"type":"tool","text":"ignored"}]}',))
    con.commit()
    con.close()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "opencode.db")
        make_store(self.db)
        self.old_db = opencode_run.DB_PATH
        opencode_run.DB_PATH = Path(self.db)

    def tearDown(self):
        opencode_run.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_a_session_is_found_and_its_folder_read(self):
        self.assertTrue(opencode_run.known("ses_abc12345"))
        self.assertEqual(opencode_run.session_cwd("ses_abc12345"), "/tmp/work")

    def test_an_unknown_session_is_unknown(self):
        self.assertFalse(opencode_run.known("ses_nope"))

    def test_the_transcript_has_the_text_messages_only(self):
        msgs = opencode_run.transcript("ses_abc12345")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["text"], "hello there")
        self.assertEqual(msgs[1]["text"], "hi back")

    def test_events_take_the_shape_the_chat_pane_wants(self):
        evs = opencode_run.events("ses_abc12345")
        self.assertEqual(evs[0]["type"], "user")     # nav.py reads `type` as the role
        self.assertEqual(evs[0]["text"], "hello there")
        self.assertEqual(evs[1]["type"], "assistant")

    def test_send_refuses_a_bogus_session_id(self):
        res = opencode_run.send("not-a-session", "hello")
        self.assertFalse(res.get("ok"))
        self.assertIn("not an opencode session id", res.get("error") or "")

    def test_send_refuses_empty_text(self):
        res = opencode_run.send("ses_abc12345", "   ")
        self.assertFalse(res.get("ok"))


class WiringTests(unittest.TestCase):
    def test_the_navigator_has_an_opencode_route(self):
        nav = (Path(__file__).resolve().parent.parent / "navigator.py").read_text()
        self.assertIn('"/api/opencode/send"', nav)
        self.assertIn('"/api/opencode/transcript"', nav)
        self.assertIn("opencode_run.send(", nav)

    def test_the_cli_sends_to_the_opencode_route(self):
        cli = (Path(__file__).resolve().parent.parent / "nav.py").read_text()
        self.assertIn('"/api/opencode/send"', cli)
        self.assertIn('"/api/opencode/transcript?sid="', cli)


if __name__ == "__main__":
    unittest.main()
