"""engines.py (GHI #103): the registry and the opencode adapter's verbs, with the
CLI subprocesses faked. The pi adapter is exercised through pi_run's own tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engines  # noqa: E402


def _fake_run(stdout, code=0):
    def _r(argv, directory=None, timeout=20):
        return (True, stdout if code == 0 else None) if code == 0 else (False, "boom")
    return _r


class Registry(unittest.TestCase):
    def test_engines_are_registered(self):
        self.assertIn("opencode", engines.names())
        self.assertIn("pi", engines.names())
        self.assertEqual(engines.get("OpenCode"), engines.get("opencode"))   # names are lower-case
        self.assertIsNone(engines.get("nope"))

    def test_opencode_sid_shape(self):
        self.assertTrue(engines.opencode_sid_ok("ses_e462a5dc0ffewz6a3IvrXMWr1O"))
        self.assertFalse(engines.opencode_sid_ok("0335f9ef"))   # a claude uuid is not an opencode id
        self.assertFalse(engines.opencode_sid_ok("ses; drop table"))


class OpenCodeLS(unittest.TestCase):
    def setUp(self):
        self.oc = engines.OpenCode()

    def test_ls_filters_by_directory_and_sorts_newest_first(self):
        home = str(Path.home().resolve())
        rows = [
            {"id": "ses_c", "title": "old in repo", "updated": 1000, "directory": home + "/projects/airline"},
            {"id": "ses_b", "title": "new in repo", "updated": 5000, "directory": home + "/projects/airline"},
            {"id": "ses_a", "title": "other repo", "updated": 9999, "directory": home + "/projects/standup-os"},
        ]
        with mock.patch.object(engines, "_run") as run:
            run.return_value = (True, rows)
            out, err = self.oc.ls_sessions(directory=home + "/projects/airline", limit=10)
        self.assertIsNone(err)
        self.assertEqual([r["id"] for r in out], ["ses_b", "ses_c"])   # other repo filtered, newest first
        self.assertEqual(out[0]["updated"], 5.0)                      # epoch-ms -> seconds

    def test_ls_reports_a_broken_cli_as_an_error(self):
        with mock.patch.object(engines, "_run", return_value=(False, "opencode is not installed")):
            out, err = self.oc.ls_sessions()
        self.assertEqual(out, [])
        self.assertIn("not installed", err)

    def test_ls_passes_the_format_flag(self):
        with mock.patch.object(engines, "_run") as run:
            run.return_value = (True, [])
            self.oc.ls_sessions(directory="/x")
        argv = run.call_args[0][0]
        self.assertIn("--format", argv)
        self.assertIn("json", argv)


class OpenCodeTranscript(unittest.TestCase):
    def test_transcript_maps_messages_and_finds_the_newest(self):
        data = {"info": {"id": "ses_x"}, "messages": [
            {"id": "m1", "type": "user", "time": {"created": 2000}},
            {"id": "m2", "type": "agent", "time": {"created": 1000}},
        ]}
        oc = engines.OpenCode()
        with mock.patch.object(engines, "_run", return_value=(True, data)):
            t = oc.transcript(None, "ses_x")
        self.assertEqual(t["updated"], 2.0)                       # newest message, ms -> s
        self.assertEqual([m["type"] for m in t["messages"]], ["agent", "user"])   # sorted by time
        self.assertEqual(len(t["messages"]), 2)

    def test_transcript_returns_none_when_the_cli_fails(self):
        oc = engines.OpenCode()
        with mock.patch.object(engines, "_run", return_value=(False, "bad")):
            self.assertIsNone(oc.transcript(None, "ses_x"))


class OpenCodeDeliver(unittest.TestCase):
    def setUp(self):
        self.oc = engines.OpenCode()
        engines._OC_RUNS.clear()   # isolation from other tests
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = mock.patch.dict("os.environ", {"CCC_NAV_ENGINE_RUNS": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_refuses_a_second_live_run_for_one_session(self):
        class P:                            # minimal fake process: still running
            def poll(self):
                return None
        engines._OC_RUNS["r1"] = {"proc": P(), "sid": "ses_x", "started": 0, "log": ""}
        with mock.patch.object(engines.OpenCode, "_sid_dir", return_value="/repo"):
            status = self.oc.deliver(None, "ses_x", "hello")
        engines._OC_RUNS.clear()
        self.assertIn("already in progress", status)

    def test_deliver_starts_a_detached_logged_run(self):
        class P:
            def __init__(self):
                self.poll = lambda: 0
        created = {}

        def fake_popen(argv, ** kw):
            created["argv"], created["kw"] = argv, kw
            return P()
        with mock.patch.object(engines.OpenCode, "_sid_dir", return_value="/repo"), \
                mock.patch("subprocess.Popen", side_effect=fake_popen):
            status = self.oc.deliver(mock.Mock(), "ses_x", "hello there")
        self.assertEqual(status, "sent")
        self.assertEqual(created["argv"][0], "opencode")
        self.assertEqual(created["argv"][1:4], ["run", "--agent", "build"])
        self.assertIn("-s", created["argv"])
        self.assertIn("hello there", created["argv"])
        self.assertEqual(created["kw"]["cwd"], "/repo")          # runs in the session's workspace
        self.assertTrue(created["kw"]["start_new_session"])       # detached: never blocks the navigator

    def test_model_env_adds_the_model_flag(self):
        class P:
            def poll(self):
                return 0
        created = {}

        def fake_popen(argv, ** kw):
            created["argv"] = argv
            return P()
        with mock.patch.dict("os.environ", {"OPENCODE_MODEL": "a/b"}), \
                mock.patch.object(engines.OpenCode, "_sid_dir", return_value="/repo"), \
                mock.patch("subprocess.Popen", side_effect=fake_popen):
            self.oc.deliver(None, "ses_y", "hi")
        i = created["argv"].index("-m")
        self.assertEqual(created["argv"][i + 1], "a/b")


if __name__ == "__main__":
    unittest.main()


class NavEnginesCmd(unittest.TestCase):
    def _args(self, **kw):
        import argparse
        a = argparse.Namespace(command="engines", table=False, eng_cmd=kw.get("eng_cmd", "ls"), **{k: v for k, v in kw.items() if k != "eng_cmd"})
        return a

    def test_installed_prints_the_tree_map(self):
        import io, contextlib, nav
        tree = {"engines": {"claude": True, "opencode": True, "pi": True}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            class T:
                def tree(self): return tree
            rc = nav.cmd_engines(self._args(eng_cmd="installed", tree=tree), T())
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), {"claude": True, "opencode": True, "pi": True})

    def test_ls_calls_the_endpoint_with_dir_and_prints_rows(self):
        import io, contextlib, nav

        class Client:
            def get(self, path):
                self.path = path
                return {"ok": True, "rows": [{"id": "ses_x", "title": "t", "updated": 1700000000, "directory": "/repo"}]}
        c = Client()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = nav.cmd_engines(self._args(eng_cmd="ls", engine="opencode", dir="/repo"), c)
        self.assertEqual(rc, 0)
        self.assertIn("engine=opencode", c.path)
        self.assertIn("dir=/repo", c.path)
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]["id"], "ses_x")
