"""Tests for switching Uri's co-pilot: pick a session and the model it runs on."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import navigator
import pi_run
import remote

SID = "01a1ad4a-b80e-7686-8d13-0369ab8e6c3a"


class CopilotSwitchTests(unittest.TestCase):
    """POST /api/copilot {key, model}: the co-pilot moves and a Pi session's model follows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.pi_dir = d / "pi"
        sess = self.pi_dir / "--work--"
        sess.mkdir(parents=True)
        self.file = sess / ("2026-11-05T19-58-37-583Z_%s.jsonl" % SID)
        self.file.write_text(json.dumps({"type": "session", "version": "3", "id": SID, "cwd": str(d)}) + "\n")
        args = type("A", (), {})()
        args.ccc = "http://127.0.0.1:1"
        args.state = str(d / "state.json")
        args.pi_dir = str(self.pi_dir)
        args.orca_dir = None
        args.verbose = False
        for env, path in (("CCC_NAV_CHANGES", d / "changes.jsonl"), ("CCC_NAV_DECISIONS", d / "decisions.json"),
                          ("CCC_NAV_ANSWERS", d / "answers.json")):
            os.environ[env] = str(path)
            self.addCleanup(os.environ.pop, env, None)
        navigator.Handler.app = navigator.App(args)
        navigator.Handler.remote_cfg = remote.RemoteConfig("", d)
        navigator.Handler.remote_store = remote.Store(d)
        navigator.Handler.limiter = remote.RateLimiter()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/copilot", body=json.dumps(body).encode(),
                     headers={"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, json.loads(raw or b"{}")

    def test_set_copilot_with_a_model_switches_the_session(self):
        status, data = self.post({"key": "pi:" + SID, "model": "minimax/MiniMax-M3"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["copilot"]["key"], "pi:" + SID)
        self.assertEqual(data["copilot"]["model"], "minimax/MiniMax-M3")
        entry = json.loads(self.file.read_text().strip().split("\n")[-1])
        self.assertEqual(entry["type"], "model_change")
        self.assertEqual(entry["provider"], "minimax")
        self.assertEqual(entry["modelId"], "MiniMax-M3")

    def test_set_copilot_without_a_model_leaves_the_model_alone(self):
        self.post({"key": "pi:" + SID, "model": "minimax/MiniMax-M3"})
        status, data = self.post({"key": "pi:" + SID})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["copilot"]["model"], "minimax/MiniMax-M3")

    def test_bad_model_is_refused(self):
        status, data = self.post({"key": "pi:" + SID, "model": "not a model"})
        self.assertEqual(status, 400)
        self.assertIn("provider", data["error"])

    def test_bad_key_is_refused(self):
        status, _ = self.post({"key": "nonsense"})
        self.assertEqual(status, 400)

    def test_options_list_the_models_to_switch_to(self):
        self.post({"key": "pi:" + SID})
        status, data = self.post({"key": "pi:" + SID, "model": "minimax/MiniMax-M3"})
        self.assertEqual(status, 200, data)
        models = [o["model"] for o in data["copilot"]["options"] if o["engine"] == "pi"]
        self.assertIn("minimax/MiniMax-M3", models)
        for o in data["copilot"]["options"]:
            self.assertEqual(o["key"], "pi:" + SID)

    def test_clearing_the_copilot(self):
        self.post({"key": "pi:" + SID})
        status, data = self.post({"key": ""})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertIsNone(data["copilot"])


class SessionModelTests(unittest.TestCase):
    """pi_run.session_model reads the model a Pi session resumes with."""

    def test_reads_the_last_model_change(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "--w--"
            sess.mkdir()
            f = sess / ("x_%s.jsonl" % SID)
            f.write_text(json.dumps({"type": "session", "id": SID, "cwd": d}) + "\n")
            self.assertIsNone(pi_run.session_model(d, SID))
            pi_run.set_model(d, SID, "minimax", "MiniMax-M3")
            self.assertEqual(pi_run.session_model(d, SID), ("minimax", "MiniMax-M3"))
            pi_run.set_model(d, SID, "opencode-go", "iceman-contributor-free")
            self.assertEqual(pi_run.session_model(d, SID), ("opencode-go", "iceman-contributor-free"))


if __name__ == "__main__":
    unittest.main()
