"""Project owners in the navigator (docs/owners.md): registry, /api/owner, project.owner, tree field, Stop hook."""
import datetime
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions  # noqa: E402
import briefs  # noqa: E402
import navigator  # noqa: E402
import remote  # noqa: E402

CWD = "/Users/x/projects/boardsBridge"
REPO = "folder:boardsBridge"   # the project key a non-git folder gets
OWNER = "claude:0wner000-0000-4000-8000-000000000001"


def row(sid, cwd, prompt, mins=1, **kw):
    r = {"session_id": sid, "engine": "claude", "source": "interactive", "session_cwd": cwd,
         "folder_path": cwd, "first_message": prompt, "modified": time.time() - mins * 60}
    r.update(kw)
    return r


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class OwnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.owners = d / "owners"
        self.env = mock.patch.dict(os.environ, {
            "OWNER_DIR": str(self.owners), "CCC_NAV_CHANGES": str(d / "changes.jsonl"),
            "CCC_NAV_DECISIONS": str(d / "decisions.json"), "CCC_NAV_ANSWERS": str(d / "answers.json")})
        self.env.start()
        self.db = mock.patch.object(briefs, "DB_PATH", d / "briefs.db")
        self.db.start()
        navigator._owner_cache.clear()
        args = type("A", (), {})()
        args.ccc, args.state, args.pi_dir, args.orca_dir, args.verbose = (
            "http://127.0.0.1:9", str(d / "state.json"), str(d / "pi"), None, False)
        Path(args.pi_dir).mkdir()
        self.app = navigator.App(args)
        self.app.HOOK_SETTLE = 3600     # keep the brief refresher asleep: it would call CCC

    def tearDown(self):
        self.db.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write_status(self, ts, repo=REPO, **kw):
        d = self.owners / navigator.owner_slug(repo)
        d.mkdir(parents=True, exist_ok=True)
        st = dict({"updated_at": iso(ts), "session": OWNER, "state": "working", "summary": "merging #12",
                   "doing": "review", "next": "merge", "shipped": [], "needs_uri": [], "blocked": None}, **kw)
        p = d / "status.json"
        p.write_text(json.dumps(st))
        os.utime(p, (ts, ts))      # mtime moves with every write, so the cache sees it
        return st

    def log_lines(self, repo=REPO):
        try:
            return (self.owners / navigator.owner_slug(repo) / "log.md").read_text().splitlines()
        except OSError:
            return []


class Registry(OwnerBase):
    def test_set_clear_and_persist(self):
        st = self.app.state
        self.assertEqual(st.snapshot()["owners"], {})
        self.assertIsNone(st.set_owner(REPO, OWNER))
        self.assertEqual(st.set_owner(REPO, "claude:other"), OWNER)
        self.assertEqual(navigator.State(st.path).snapshot()["owners"], {REPO: "claude:other"})
        self.assertEqual(st.set_owner(REPO, ""), "claude:other")
        self.assertEqual(navigator.State(st.path).snapshot()["owners"], {})

    def test_owner_per_engine_is_remembered_for_the_switch_menu(self):
        st = self.app.state
        st.set_owner(REPO, "claude:a1")
        st.set_owner(REPO, "opencode:ses_b2")
        self.assertEqual(st.owner_per_engine(REPO), {"claude": "claude:a1", "opencode": "opencode:ses_b2"})
        snap = navigator.State(st.path).snapshot()
        self.assertEqual(snap["owner_by_engine"][REPO]["opencode"], "opencode:ses_b2")   # survives a restart
        self.assertEqual(st.set_owner(REPO, ""), "opencode:ses_b2")              # clear returns the last key
        self.assertEqual(st.owner_per_engine(REPO), {"claude": "claude:a1", "opencode": "opencode:ses_b2"})  # clear keeps the history

    def test_action_records_change_and_undo_restores(self):
        res = actions.dispatch(self.app, "project.owner", {"repo": REPO, "key": OWNER})
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["owner"]["problem"], "no status yet")
        c = self.app.changes.get(res["change"])
        self.assertEqual((c["action"], c["before"], c["after"]), ("project.owner", {"owners": {REPO: None}},
                                                                  {"owners": {REPO: OWNER}}))
        res2 = actions.dispatch(self.app, "project.owner", {"repo": REPO, "key": ""})     # clear
        self.assertEqual(self.app.state.snapshot()["owners"], {})
        actions.dispatch(self.app, "undo", {"change": res2["change"]})
        self.assertEqual(self.app.state.snapshot()["owners"], {REPO: OWNER})
        actions.dispatch(self.app, "undo", {"change": res["change"]})
        self.assertEqual(self.app.state.snapshot()["owners"], {})

    def test_action_rejects_bad_params(self):
        with self.assertRaises(actions.ActionError) as e:
            actions.dispatch(self.app, "project.owner", {"repo": REPO, "key": "not a key"})
        self.assertEqual(e.exception.field, "key")
        with self.assertRaises(actions.ActionError) as e:
            actions.dispatch(self.app, "project.owner", {"key": OWNER})
        self.assertEqual(e.exception.field, "repo")
        self.assertEqual(self.app.state.snapshot()["owners"], {})


class Http(OwnerBase):
    def setUp(self):
        super().setUp()
        navigator.Handler.app = self.app
        navigator.Handler.remote_cfg = remote.RemoteConfig("", Path(self.tmp.name))
        navigator.Handler.remote_store = remote.Store(Path(self.tmp.name))
        navigator.Handler.limiter = remote.RateLimiter()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=json.dumps(body), headers={
            "Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json", "X-Nav-Actor": "copilot"})
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def test_api_owner_set_and_clear(self):
        self.write_status(time.time() - 60)
        st, out = self.post("/api/owner", {"repo": REPO, "key": OWNER})
        self.assertEqual(st, 200)
        self.assertEqual((out["ok"], out["repo"], out["key"]), (True, REPO, OWNER))
        self.assertEqual(out["owner"]["summary"], "merging #12")
        self.assertFalse(out["owner"]["stale"])
        self.assertEqual(self.app.changes.get(out["change"])["actor"], "copilot")
        st, out = self.post("/api/owner", {"repo": REPO, "key": ""})
        self.assertEqual((st, out["key"], out["owner"]), (200, None, None))
        self.assertEqual(self.app.state.snapshot()["owners"], {})

    def test_api_owner_bad_input(self):
        self.assertEqual(self.post("/api/owner", {"repo": REPO, "key": "nope"})[0], 400)
        self.assertEqual(self.post("/api/owner", {"key": OWNER})[0], 400)

    def test_v1_action_route(self):
        st, out = self.post("/nav/v1/actions/project.owner", {"repo": REPO, "key": OWNER})
        self.assertEqual((st, out["ok"], out["action"]), (200, True, "project.owner"))
        self.assertEqual(self.app.state.snapshot()["owners"], {REPO: OWNER})


class TreeField(OwnerBase):
    def test_no_status_yet(self):
        o = navigator.owner_info(REPO, OWNER)
        self.assertEqual((o["key"], o["stale"], o["problem"], o["updated_at"]), (OWNER, True, "no status yet", None))

    def test_fresh_status(self):
        now = time.time()
        st = self.write_status(now - 600, state="blocked")
        o = navigator.owner_info(REPO, OWNER, now=now)
        self.assertEqual(o, {"key": OWNER, "summary": "merging #12", "state": "blocked",
                             "updated_at": st["updated_at"], "stale": False, "problem": ""})

    def test_old_status(self):
        now = time.time()
        self.write_status(now - 26 * 3600 - 60)
        o = navigator.owner_info(REPO, OWNER, now=now)
        self.assertEqual((o["stale"], o["problem"]), (True, "status 26h old"))

    def test_turn_ended_after_status(self):
        now = time.time()
        self.write_status(now - 600)
        o = navigator.owner_info(REPO, OWNER, turn=(now - 300, now - 10), now=now)
        self.assertEqual((o["stale"], o["problem"]), (True, "no status this turn"))
        o = navigator.owner_info(REPO, OWNER, turn=(now - 900, now - 10), now=now)   # written during the turn
        self.assertFalse(o["stale"])

    def test_status_cache_follows_the_file(self):
        now = time.time()
        self.write_status(now - 120, summary="first")
        self.assertEqual(navigator.owner_info(REPO, OWNER)["summary"], "first")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            self.assertEqual(navigator.owner_info(REPO, OWNER)["summary"], "first")     # same mtime: cached
        self.write_status(now - 60, summary="second")
        self.assertEqual(navigator.owner_info(REPO, OWNER)["summary"], "second")

    def test_tree_has_owner_field_and_always_keeps_owner_sessions(self):
        """An owner session is never hidden and never dropped by the window, like the co-pilot."""
        owner_sid = OWNER.split(":", 1)[1]
        rows = [row("live1111-aaaa", CWD, "fresh work"),
                row(owner_sid, CWD, "owner", mins=3 * 24 * 60, archived=True)]   # 3 days quiet, archived
        self.app.ccc.rows = lambda: (rows, {})
        self.app.state.set_hidden([OWNER], True)
        self.app.state.set_owner(REPO, OWNER)
        self.write_status(time.time() - 60)
        empty = ([], {})
        with mock.patch.object(navigator, "pi_sessions", return_value=empty), \
                mock.patch.object(navigator.sources, "outsourcerer_jobs", return_value=empty), \
                mock.patch.object(navigator.sources, "vm251_agents", return_value=empty), \
                mock.patch.object(navigator.orca, "load_workspaces", return_value=empty), \
                mock.patch.object(navigator, "desktop_titles", return_value={}), \
                mock.patch.object(navigator, "titler_titles", return_value={}), \
                mock.patch.object(navigator, "desktop_groups", return_value={}), \
                mock.patch.object(navigator, "desktop_archived", return_value=set()):
            t = self.app._tree("1d")
            self.app.state.set_owner(REPO, "")
            t2 = self.app._tree("1d")
        keys = [i["key"] for r in t["repos"] for g in r["groups"] for i in g["items"]]
        self.assertIn(OWNER, keys)
        repo = next(r for r in t["repos"] if r["key"] == REPO)
        self.assertEqual(repo["owner"]["summary"], "merging #12")
        self.assertEqual(t["owners"][REPO]["key"], OWNER)
        keys2 = [i["key"] for r in t2["repos"] for g in r["groups"] for i in g["items"]]
        self.assertNotIn(OWNER, keys2)                                 # no longer an owner: filtered again
        self.assertIsNone(next(r for r in t2["repos"] if r["key"] == REPO)["owner"])

    def test_always_keys_has_copilot_and_owners(self):
        self.app.state.set_copilot("claude:cop")
        self.app.state.set_owner(REPO, OWNER)
        self.app.state.set_owner("/r/other", "codex:o2")
        self.assertEqual(self.app.always_keys(), {"claude:cop", OWNER, "codex:o2"})

    def test_build_tree_accepts_a_set(self):
        rows = [row("1", "/r/A", "One"), dict(row("2", "/r/A", "Owner"), archived=True),
                row("3", "/r/A", "Old owner", mins=10 * 24 * 60)]
        base = {"nicknames": {}, "groups": {}, "assign": {}, "hidden": {"claude:2": 1}, "pinned": {}}
        keys = lambda t: sorted(i["key"] for r in t["repos"] for g in r["groups"] for i in g["items"])
        self.assertEqual(keys(navigator.build_tree(rows, base, "1d", [], "http://ccc")), ["claude:1"])
        self.assertEqual(keys(navigator.build_tree(rows, base, "1d", [], "http://ccc", always={"claude:2", "claude:3"})),
                         ["claude:1", "claude:2", "claude:3"])


class StopHook(OwnerBase):
    LINE = "[navigator] turn ended without a status update"

    def flagged(self):
        return sum(self.LINE in ln for ln in self.log_lines())

    def test_turn_without_status_is_logged_once(self):
        self.app.state.set_owner(REPO, OWNER)
        t = time.time() - 1000
        self.write_status(t)
        self.assertFalse(self.app.owner_turn_end(OWNER, now=t + 100))     # first turn seen, status present
        self.assertTrue(self.app.owner_turn_end(OWNER, now=t + 200))      # a whole turn, no status
        self.assertFalse(self.app.owner_turn_end(OWNER, now=t + 200.5))   # same Stop delivered twice
        self.assertEqual(self.flagged(), 1)
        self.write_status(t + 250)
        self.assertFalse(self.app.owner_turn_end(OWNER, now=t + 300))     # wrote its status this turn
        self.assertEqual(self.flagged(), 1)
        self.assertEqual(self.app.owner(REPO)["problem"], "")
        self.assertTrue(self.app.owner_turn_end(OWNER, now=t + 400))      # next turn skipped it again
        self.assertEqual(self.flagged(), 2)
        self.assertEqual(self.app.owner(REPO)["problem"], "no status this turn")
        self.assertRegex(self.log_lines()[-1], r"^- \d{4}-\d\d-\d\d \d\d:\d\d \[navigator\] turn ended")

    def test_missing_status_and_non_owner(self):
        self.assertFalse(self.app.owner_turn_end(OWNER))                   # not an owner: nothing
        self.assertEqual(self.log_lines(), [])
        self.app.state.set_owner(REPO, OWNER)
        self.assertTrue(self.app.owner_turn_end(OWNER))                    # owner without any status
        self.assertEqual(self.flagged(), 1)

    def test_hook_endpoint_uses_previous_stop_from_the_hook_log(self):
        sid = OWNER.split(":", 1)[1]
        now = time.time()
        db = briefs.connect()
        briefs.record_hook(db, {"hook_event_name": "Stop", "session_id": sid}, now=now - 100)   # previous turn ended
        db.close()
        self.write_status(now - 200)                                       # written before that
        self.app.state.set_owner(REPO, OWNER)
        self.assertTrue(self.app.hook({"hook_event_name": "Stop", "session_id": sid})["ok"])
        self.assertEqual(self.flagged(), 1)
        self.app.hook({"hook_event_name": "SessionEnd", "session_id": sid})  # not a turn end
        self.assertEqual(self.flagged(), 1)


if __name__ == "__main__":
    unittest.main()
