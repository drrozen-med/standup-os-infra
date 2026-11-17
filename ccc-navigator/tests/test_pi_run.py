import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pi_run  # noqa: E402

SID = "01a15d3e-2361-7e7f-ae85-799889da3027"


class PiRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        d = root / "sessions" / "--work--"
        d.mkdir(parents=True)
        self.file = d / ("2026-10-21T06-55-15-809Z_%s.jsonl" % SID)
        self.file.write_text(json.dumps({"type": "session", "id": SID, "cwd": str(self.work)}) + "\n")
        stub = root / "fake-pi"
        stub.write_text('#!/bin/sh\necho "args: $@" > "%s/args.txt"\necho REPLY\n' % root)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.old = dict(os.environ)
        os.environ["PI_BIN"] = str(stub)
        pi_run.RUN_DIR = root / "runs"
        self.sessions = root / "sessions"
        for v in list(pi_run._runs.values()):      # a run left over from another test
            try:
                v["proc"].kill()
            except OSError:
                pass
        pi_run._runs.clear()
        pi_run._pending_model.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def test_rejects_bad_input(self):
        self.assertFalse(pi_run.send(self.sessions, "../etc", "hi")["ok"])
        self.assertFalse(pi_run.send(self.sessions, SID, "   ")["ok"])
        self.assertFalse(pi_run.send(self.sessions, SID, "x" * (pi_run.MAX_TEXT + 1))["ok"])
        self.assertIn("not found", pi_run.send(self.sessions, "abcdef12-0000", "hi")["error"])

    def test_missing_folder_is_reported(self):
        self.file.write_text(json.dumps({"type": "session", "id": SID, "cwd": "/no/such/dir"}) + "\n")
        self.assertIn("no longer exists", pi_run.send(self.sessions, SID, "hi")["error"])

    def test_runs_pi_on_the_session_file_and_reports_status(self):
        res = pi_run.send(self.sessions, SID, "hello there")
        self.assertTrue(res["ok"], res)
        for _ in range(50):
            st = pi_run.status(res["run"])
            if st["done"]:
                break
            time.sleep(0.1)
        self.assertTrue(st["done"])
        self.assertEqual(st["rc"], 0)
        args = (Path(self.tmp.name) / "args.txt").read_text()
        self.assertIn("--session %s" % self.file, args)
        self.assertIn("-p hello there", args)
        self.assertIn("REPLY", Path(res["log"]).read_text())

    def test_unknown_run(self):
        self.assertFalse(pi_run.status("nope")["ok"])

    def test_set_model_appends_a_model_change_entry(self):
        res = pi_run.set_model(self.sessions, SID, "minimax", "MiniMax-M3")
        self.assertTrue(res["ok"], res)
        entry = json.loads(self.file.read_text().strip().split("\n")[-1])
        self.assertEqual(entry["type"], "model_change")
        self.assertEqual(entry["provider"], "minimax")
        self.assertEqual(entry["modelId"], "MiniMax-M3")
        self.assertEqual(len(entry["id"]), 8)
        self.assertTrue(entry["timestamp"].endswith("Z"))
        self.assertIsNone(entry["parentId"])              # no entries yet: the header is not part of the tree
        self.assertEqual(pi_run.session_model(self.sessions, SID), ("minimax", "MiniMax-M3"))

    def test_set_model_parents_the_new_entry_to_the_last_entry(self):
        with self.file.open("a") as f:
            f.write(json.dumps({"type": "message", "id": "abc12345", "parentId": None,
                                "timestamp": "2026-11-05T00:00:00.000Z",
                                "message": {"role": "user", "content": "hi"}}) + "\n")
        res = pi_run.set_model(self.sessions, SID, "minimax", "MiniMax-M3")
        self.assertTrue(res["ok"], res)
        entry = json.loads(self.file.read_text().strip().split("\n")[-1])
        self.assertEqual(entry["parentId"], "abc12345")

    def test_set_model_needs_provider_and_model(self):
        self.assertFalse(pi_run.set_model(self.sessions, SID, "", "MiniMax-M3")["ok"])
        self.assertFalse(pi_run.set_model(self.sessions, SID, "minimax", "")["ok"])

    def test_set_model_on_a_missing_session(self):
        self.assertFalse(pi_run.set_model(self.sessions, "abcdef12-0000", "minimax", "MiniMax-M3")["ok"])

    def test_set_model_is_refused_while_a_run_is_live(self):
        slow = Path(self.tmp.name) / "slow-pi"
        slow.write_text('#!/bin/sh\nsleep 30\n')
        slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
        os.environ["PI_BIN"] = str(slow)
        first = pi_run.send(self.sessions, SID, "hello there")
        self.assertTrue(first["ok"], first)
        res = pi_run.set_model(self.sessions, SID, "minimax", "MiniMax-M3")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res.get("pending"), res)      # queued: the file is being written right now
        self.assertIn("when it finishes", res["note"])
        self.assertIsNone(pi_run.session_model(self.sessions, SID))
        try:
            os.kill(first["pid"], 9)
        except OSError:
            pass

    def test_a_queued_model_switch_is_applied_before_the_next_run(self):
        slow = Path(self.tmp.name) / "slow-pi"
        slow.write_text('#!/bin/sh\nsleep 30\n')
        slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
        os.environ["PI_BIN"] = str(slow)
        first = pi_run.send(self.sessions, SID, "hello there")
        self.assertTrue(first["ok"], first)
        queued = pi_run.set_model(self.sessions, SID, "minimax", "MiniMax-M3")
        self.assertTrue(queued.get("pending"), queued)
        try:
            os.kill(first["pid"], 9)
        except OSError:
            pass
        for _ in range(50):
            if not pi_run._live_run(SID):
                break
            time.sleep(0.1)
        os.environ["PI_BIN"] = str(Path(self.tmp.name) / "fake-pi")
        second = pi_run.send(self.sessions, SID, "and again")
        self.assertTrue(second["ok"], second)
        self.assertEqual(pi_run.session_model(self.sessions, SID), ("minimax", "MiniMax-M3"))

    def test_a_second_run_for_the_same_session_is_refused(self):
        slow = Path(self.tmp.name) / "slow-pi"
        slow.write_text('#!/bin/sh\nsleep 30\n')
        slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
        os.environ["PI_BIN"] = str(slow)
        first = pi_run.send(self.sessions, SID, "hello there")
        self.assertTrue(first["ok"], first)
        second = pi_run.send(self.sessions, SID, "and again")
        self.assertFalse(second["ok"])
        self.assertIn("already in progress", second["error"])
        try:
            os.kill(first["pid"], 9)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
