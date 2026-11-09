"""The Co-pilot send reports unread Codex messages instead of a bare 'queued'."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import navigator  # noqa: E402


class QueuePending(unittest.TestCase):
    def test_counts_rows_for_thread(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "q.sqlite")
            con = sqlite3.connect(db)
            con.execute("create table queued_items (id text, thread_id text)")
            con.executemany("insert into queued_items values (?,?)", [("1", "a"), ("2", "a"), ("3", "b")])
            con.commit()
            con.close()
            self.assertEqual(navigator.codex_queue_pending("a", db), 2)
            self.assertEqual(navigator.codex_queue_pending("zzz", db), 0)

    def test_unreadable_is_none(self):
        self.assertIsNone(navigator.codex_queue_pending("a", "/nonexistent/q.sqlite"))


if __name__ == "__main__":
    unittest.main()
