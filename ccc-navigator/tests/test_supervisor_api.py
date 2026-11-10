"""Supervisor in the navigator (docs/supervisor.md): GET/POST /api/supervisor and `nav supervisor`."""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nav  # noqa: E402
import navigator  # noqa: E402
import remote  # noqa: E402

ROW = {"project": "alpha", "owner_key": "claude:a1", "state": "working", "summary": "s", "flags": ["idle-2x"],
       "days_since_shipped": 1.5, "decisions_open": 2, "oldest_decision_days": 8.0, "idle_checkins": 2,
       "wakes_today": 1, "last_wake": {"reason": "keep-moving", "when": "2026-11-03T12:00:00+02:00"}, "paused": False}


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sup"
        # bin/supervisor.py really runs here: it must see no real owner folder and no real Outsourcerer job
        osrc = Path(self.tmp.name) / "outsourcerer.sh"
        osrc.write_text("#!/bin/sh\necho '{\"jobs\": []}'\n")
        osrc.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"SUPERVISOR_DIR": str(self.dir), "OWNER_DIR": str(Path(self.tmp.name) / "owners"),
                                                "SUPERVISOR_OSRC": str(osrc)})
        self.env.start()
        navigator.Handler.app = SimpleNamespace(args=SimpleNamespace(verbose=False), state=None)
        navigator.Handler.remote_cfg = remote.RemoteConfig("", Path(self.tmp.name))
        navigator.Handler.remote_store = remote.Store(Path(self.tmp.name))
        navigator.Handler.limiter = remote.RateLimiter()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": "127.0.0.1:%d" % self.port, "X-Nav-Actor": "copilot"}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def test_get_empty_then_with_files(self):
        st, out = self.req("GET", "/api/supervisor")
        self.assertEqual((st, out["register"], out["paused"], out["digest_at"], out["cycles_tail"]),
                         (200, [], False, None, []))
        self.dir.mkdir()
        (self.dir / "register.json").write_text(json.dumps({"rows": [ROW]}))
        (self.dir / "digest.json").write_text(json.dumps({"generated_at": "2026-11-03T12:00:00+02:00"}))
        (self.dir / "state.json").write_text(json.dumps({"paused": True, "paused_projects": ["beta"],
                                                         "last_cycle": "2026-11-03T12:00:00+02:00"}))
        (self.dir / "cycles.log").write_text("".join("line %d\n" % i for i in range(30)))
        st, out = self.req("GET", "/api/supervisor")
        self.assertEqual(out["register"], [ROW])
        self.assertEqual(out["digest_at"], "2026-11-03T12:00:00+02:00")
        self.assertTrue(out["paused"])
        self.assertEqual(out["paused_projects"], ["beta"])
        self.assertEqual(out["cycles_tail"][-1], "line 29")
        self.assertEqual(len(out["cycles_tail"]), 20)
        self.assertEqual((out["night"], out["engine_default"], out["jobs"]), (False, "session", None))
        jobs = {"running": 2, "max": 4, "held": "", "night": False, "providers": {"iceman": {"ok": 3, "bad": 1, "streak": 0, "cooling": False}}}
        (self.dir / "register.json").write_text(json.dumps({"rows": [ROW], "jobs": jobs}))
        (self.dir / "state.json").write_text(json.dumps({"night": True, "engine_default": "jobs"}))
        st, out = self.req("GET", "/api/supervisor")
        self.assertEqual((out["night"], out["engine_default"], out["jobs"]), (True, "jobs", jobs))

    def test_post_pause_and_resume_run_supervisor_py(self):
        st, out = self.req("POST", "/api/supervisor", {"action": "pause"})
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"] and out["paused"])
        self.assertTrue(out["supervisor"]["paused"])
        self.assertIn("pause all by copilot", (self.dir / "cycles.log").read_text())
        st, out = self.req("POST", "/api/supervisor", {"action": "resume"})
        self.assertEqual((st, out["paused"]), (200, False))

    def test_post_bad_input(self):
        self.assertEqual(self.req("POST", "/api/supervisor", {"action": "explode"})[0], 400)
        self.assertEqual(self.req("POST", "/api/supervisor", {"action": "wake"})[0], 400)
        for body in ({"action": "engine", "project": "alpha"}, {"action": "engine", "project": "alpha", "engine": "turbo"},
                     {"action": "engine", "engine": "jobs"}, {"action": "night"}, {"action": "night", "on": "yes"},
                     {"action": "feedback", "project": "alpha"}, {"action": "feedback", "project": "alpha", "text": "  "},
                     {"action": "feedback", "text": "x"}):
            self.assertEqual(self.req("POST", "/api/supervisor", body)[0], 400, body)

    def test_post_engine_night_feedback_and_stop_argv(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[1:])
            return SimpleNamespace(returncode=0, stdout='{"ok": true}\n', stderr="")
        with mock.patch.object(navigator.subprocess, "run", fake_run):
            for body in ({"action": "engine", "project": "all", "engine": "jobs"}, {"action": "night", "on": True},
                         {"action": "night", "on": False}, {"action": "stop"},
                         {"action": "feedback", "project": "alpha", "text": "--json  ship\n#12 first"}):
                st, out = self.req("POST", "/api/supervisor", body)
                self.assertEqual((st, out["ok"], out["action"]), (200, True, body["action"]))
        py = str(navigator.SUPERVISOR_PY)
        self.assertEqual(calls, [[py, "engine", "--json", "all", "jobs"], [py, "night", "--json", "on"],
                                 [py, "night", "--json", "off"], [py, "stop", "--json"],
                                 [py, "feedback", "--json", "--", "alpha", "--json ship #12 first"]])    # free text is never an option

    def test_post_night_and_feedback_reach_supervisor_py(self):
        st, out = self.req("POST", "/api/supervisor", {"action": "night", "on": True})
        self.assertEqual((st, out["night"], out["supervisor"]["night"]), (200, True, True))
        self.assertIn("night on by copilot", (self.dir / "cycles.log").read_text())
        st, out = self.req("POST", "/api/supervisor", {"action": "stop"})
        self.assertEqual((st, out["paused"], out["cancelled"], out["supervisor"]["paused"]), (200, True, [], True))

    def test_post_wake_argv_and_refusal(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=1, stdout='{"ok": false, "skip": "woken 5 min ago"}\n', stderr="")
        with mock.patch.object(navigator.subprocess, "run", fake_run):
            st, out = self.req("POST", "/api/supervisor", {"action": "wake", "project": "alpha", "reason": "go"})
        self.assertEqual((st, out["skip"]), (400, "woken 5 min ago"))
        argv, kw = calls[0]
        self.assertEqual(argv[1:], [str(navigator.SUPERVISOR_PY), "wake", "alpha", "--reason", "go", "--json"])
        self.assertEqual(kw["env"]["NAV_ACTOR"], "copilot")
        self.assertEqual(kw["env"]["CCC_NAV_URL"], "http://127.0.0.1:%d" % self.port)


class Cli(unittest.TestCase):
    class Client:
        def __init__(self, reply=None):
            self.posts, self.reply = [], reply or {"ok": True, "paused": True, "supervisor": {}}

        def get(self, path):
            assert path == "/api/supervisor"
            return {"register": [ROW, {"project": "beta", "error": "boom", "flags": ["error"]},
                                 dict(ROW, project="gamma", flags=[], engine="jobs", last_job=None,
                                      job={"id": "j-1", "provider": "iceman", "state": "running", "min": 12, "running": True}),
                                 dict(ROW, project="delta", flags=["turns-failing"], engine="jobs", job=None,
                                      last_job={"id": "j-0", "provider": "beam", "ended": "wedged", "delivered": False})],
                    "paused": False, "paused_projects": [], "last_cycle": "t", "digest_at": "t", "cycles_tail": [],
                    "night": True, "engine_default": "jobs",
                    "jobs": {"running": 1, "max": 4, "held": "load 45, limit 30", "night": False,
                             "providers": {"iceman": {"ok": 3, "bad": 1, "streak": 0, "cooling": False},
                                           "beam": {"ok": 0, "bad": 3, "streak": 3, "cooling": True}}}}

        def post(self, path, payload):
            self.posts.append((path, payload))
            return dict(self.reply)

    def run_nav(self, client, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = nav.cmd_supervisor(nav.build_parser().parse_args(["supervisor", *argv]), client)
        return code, out.getvalue()

    def test_show_json_and_table(self):
        code, out = self.run_nav(self.Client())
        self.assertEqual((code, json.loads(out)["register"][0]["project"]), (0, "alpha"))
        code, out = self.run_nav(self.Client(), "--table")
        self.assertIn("supervisor running", out)
        self.assertIn("[idle-2x]", out)
        self.assertIn("beta", out)
        self.assertIn("ERROR boom", out)
        self.assertIn("supervisor running; night mode on; last cycle t", out)
        self.assertIn("jobs: 1 running of 4; new turns held, load 45, limit 30; providers: iceman 3/4, beam 0/3 resting", out)
        self.assertIn("{job j-1 on iceman, 12 min}", out)
        self.assertIn("{last turn on beam: wedged, no status} [turns-failing]", out)

    def test_pause_resume_wake_post(self):
        c = self.Client()
        self.run_nav(c, "pause")
        self.run_nav(c, "resume", "alpha")
        code, out = self.run_nav(c, "wake", "alpha", "--reason", "rebase first", "--table")
        self.assertEqual(c.posts, [("/api/supervisor", {"action": "pause"}),
                                   ("/api/supervisor", {"action": "resume", "project": "alpha"}),
                                   ("/api/supervisor", {"action": "wake", "project": "alpha",
                                                        "reason": "rebase first"})])
        self.assertEqual((code, out.strip()), (0, "supervisor wake alpha: ok"))

    def test_wake_needs_a_project(self):
        with self.assertRaises(nav.UsageError):
            self.run_nav(self.Client(), "wake")

    def test_engine_night_feedback_stop_post(self):
        c = self.Client({"ok": True, "paused": True, "cancelled": [{"project": "alpha", "job": "j-1"}], "failed": [], "supervisor": {}})
        self.run_nav(c, "engine", "all", "jobs")
        self.run_nav(c, "engine", "alpha", "session")
        self.run_nav(c, "night", "on")
        self.run_nav(c, "night", "off")
        self.run_nav(c, "feedback", "alpha", "Stop re-planning; ship #12")
        code, out = self.run_nav(c, "stop", "--table")
        self.assertEqual([b for _, b in c.posts], [
            {"action": "engine", "project": "all", "engine": "jobs"}, {"action": "engine", "project": "alpha", "engine": "session"},
            {"action": "night", "on": True}, {"action": "night", "on": False},
            {"action": "feedback", "project": "alpha", "text": "Stop re-planning; ship #12"}, {"action": "stop"}])
        self.assertEqual((code, out.strip()), (0, "supervisor stop: paused; cancelled 1 job(s)"))
        for bad in (("engine", "alpha"), ("engine", "alpha", "turbo"), ("engine",), ("night",), ("night", "maybe"),
                    ("feedback", "alpha"), ("feedback",)):
            with self.assertRaises(nav.UsageError, msg=bad):
                self.run_nav(self.Client(), *bad)


if __name__ == "__main__":
    unittest.main()
