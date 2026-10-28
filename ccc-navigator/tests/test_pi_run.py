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


if __name__ == "__main__":
    unittest.main()
