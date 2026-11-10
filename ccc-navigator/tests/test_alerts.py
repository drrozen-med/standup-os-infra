"""Loud alerts (alerts.py, docs/supervisor.md "Loud alerts"): store, dedupe, GET/POST /api/alerts,
auto-ack when the linked decision is answered, change log, and `nav alerts` / `nav alert`."""
import contextlib
import http.client
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerts  # noqa: E402
import briefs  # noqa: E402
import nav  # noqa: E402
import navigator  # noqa: E402
import remote  # noqa: E402

NOW = 2_000_000_000
H = 3600


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "alerts.json"
        self.s = alerts.Alerts(self.path)

    def test_raise_shape_and_private_file(self):
        r = self.s.raise_alert("  Decide   pricing ", "why", {"kind": "decision", "ref": "d1"}, by="supervisor", now=NOW)
        a = r["alert"]
        self.assertTrue(r["raised"])
        self.assertTrue(a["id"].startswith("al-"))
        self.assertEqual({k: a[k] for k in ("title", "body", "link", "key", "raised_by", "raised_at", "acked_at", "acked_by")},
                         {"title": "Decide pricing", "body": "why", "link": {"kind": "decision", "ref": "d1"},
                          "key": "decision:d1", "raised_by": "supervisor", "raised_at": NOW, "acked_at": None,
                          "acked_by": None})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_default_key_from_link_else_title(self):
        self.assertEqual(self.s.raise_alert("t", link={"kind": "session", "ref": "claude:x"}, now=NOW)["alert"]["key"],
                         "session:claude:x")
        self.assertEqual(self.s.raise_alert("Hello There", now=NOW)["alert"]["key"], "title:hello there")
        self.assertEqual(self.s.raise_alert("t", key="k1", now=NOW)["alert"]["key"], "k1")

    def test_dedupe_while_open(self):
        a = self.s.raise_alert("one", key="k", now=NOW)["alert"]
        r = self.s.raise_alert("two", key="k", now=NOW + 60)
        self.assertEqual((r["raised"], r["alert"]["id"], r["reason"]), (False, a["id"], "already open"))
        self.assertEqual(len(self.s.list()["open"]), 1)

    def test_acked_key_raises_again_only_after_12h(self):
        a = self.s.raise_alert("one", key="k", now=NOW)["alert"]
        self.s.ack(a["id"], now=NOW + H)
        r = self.s.raise_alert("again", key="k", now=NOW + 12 * H)          # 11 h after the ack
        self.assertFalse(r["raised"])
        self.assertIn("acked 11.0 h ago", r["reason"])
        r = self.s.raise_alert("again", key="k", now=NOW + 13 * H)          # exactly 12 h after
        self.assertTrue(r["raised"])
        self.assertNotEqual(r["alert"]["id"], a["id"])

    def test_ack_one_all_link_unknown_and_twice(self):
        a = self.s.raise_alert("a", key="a", now=NOW)["alert"]
        b = self.s.raise_alert("b", link={"kind": "decision", "ref": "d1"}, now=NOW + 1)["alert"]
        c = self.s.raise_alert("c", key="c", now=NOW + 2)["alert"]
        self.assertEqual([x["id"] for x in self.s.ack(a["id"], by="you", now=NOW + 5)], [a["id"]])
        self.assertEqual(self.s.ack(a["id"], now=NOW + 6), [])                 # already acked: nothing
        self.assertEqual([x["id"] for x in self.s.ack(link={"kind": "decision", "ref": "d1"}, by="you", now=NOW + 7)],
                         [b["id"]])
        self.assertEqual([x["id"] for x in self.s.ack(all_open=True, now=NOW + 8)], [c["id"]])
        with self.assertRaises(alerts.AlertError) as e:
            self.s.ack("al-nope")
        self.assertEqual(e.exception.code, "unknown_alert")
        with self.assertRaises(alerts.AlertError):
            self.s.ack()
        acked = self.s.list()["acked"]
        self.assertEqual([x["id"] for x in acked], [c["id"], b["id"], a["id"]])     # newest ack first
        self.assertEqual(acked[-1]["acked_by"], "you")

    def test_list_open_newest_first_and_last_20_acked(self):
        for i in range(25):
            x = self.s.raise_alert("a%d" % i, key="k%d" % i, now=NOW + i)["alert"]
            self.s.ack(x["id"], now=NOW + 100 + i)
        self.s.raise_alert("old", key="o1", now=NOW + 200)
        self.s.raise_alert("new", key="o2", now=NOW + 300)
        v = self.s.list()
        self.assertEqual([x["title"] for x in v["open"]], ["new", "old"])
        self.assertEqual(len(v["acked"]), 20)
        self.assertEqual(v["acked"][0]["title"], "a24")

    def test_validation(self):
        for title, link, code in (("", None, "bad_title"), ("x" * 161, None, "bad_title"),
                                  ("t", {"kind": "mail", "ref": "x"}, "bad_link"),
                                  ("t", {"kind": "url", "ref": "javascript:alert(1)"}, "bad_link"),
                                  ("t", {"kind": "session", "ref": "no colon"}, "bad_link"),
                                  ("t", {"kind": "decision", "ref": ""}, "bad_link"), ("t", "d1", "bad_link")):
            with self.subTest(title=title, link=link), self.assertRaises(alerts.AlertError) as e:
                self.s.raise_alert(title, link=link)
            self.assertEqual(e.exception.code, code)
        self.assertFalse(self.path.exists())

    def test_a_report_link_is_validated(self):
        ok = self.s.raise_alert("Morning report", link={"kind": "report", "ref": "morning-report-2026-11-10"}, now=NOW)["alert"]
        self.assertEqual((ok["link"], ok["key"]), ({"kind": "report", "ref": "morning-report-2026-11-10"},
                                                    "report:morning-report-2026-11-10"))
        for ref in ("../../etc/passwd", "morning-report-2026-11-10.md", "morning-report-x", "report", "morning-report-2026-11-10/../x"):
            with self.assertRaises(alerts.AlertError, msg=ref) as cm:
                self.s.raise_alert("x", link={"kind": "report", "ref": ref}, now=NOW)
            self.assertEqual(cm.exception.code, "bad_link")

    def test_an_alert_keyed_as_a_report_is_shown_with_its_link(self):
        self.s.raise_alert("Morning report is ready", "/some/path.md", key="morning-report-2026-11-10", by="cli", now=NOW)
        self.s.raise_alert("Other", key="morning-report-soon", by="cli", now=NOW)
        self.s.raise_alert("Decision", link={"kind": "decision", "ref": "d1"}, now=NOW)
        by_key = {a["key"]: a for a in self.s.list()["open"]}
        self.assertEqual(by_key["morning-report-2026-11-10"]["link"], {"kind": "report", "ref": "morning-report-2026-11-10"})
        self.assertIsNone(by_key["morning-report-soon"]["link"])                 # not a report name: no link
        self.assertEqual(by_key["decision:d1"]["link"], {"kind": "decision", "ref": "d1"})
        self.assertIsNone(self.s._read()[0]["link"])                              # shown, not stored
        self.s.ack(by_key["morning-report-2026-11-10"]["id"], now=NOW + 5)
        self.assertEqual(self.s.list()["acked"][0]["link"]["kind"], "report")

    def test_env_overrides_default_path(self):
        with mock.patch.dict(os.environ, {"CCC_NAV_ALERTS": str(self.path)}):
            self.assertEqual(alerts.Alerts().path, self.path)


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.d = d
        env = mock.patch.dict(os.environ, {"CCC_NAV_CHANGES": str(d / "changes.jsonl"),
                                           "CCC_NAV_DECISIONS": str(d / "decisions.json"),
                                           "CCC_NAV_ANSWERS": str(d / "answers.json"),
                                           "CCC_NAV_WATCHTOWER_DECISIONS": str(d / "wt.json"),
                                           "CCC_NAV_ALERTS": str(d / "alerts.json")})
        env.start()
        self.addCleanup(env.stop)
        db = mock.patch.object(briefs, "DB_PATH", d / "briefs.db")
        db.start()
        self.addCleanup(db.stop)
        args = type("A", (), {})()
        args.ccc, args.state, args.pi_dir, args.orca_dir, args.verbose, args.port = (
            "http://127.0.0.1:9", str(d / "state.json"), str(d / "pi"), None, False, 0)
        Path(args.pi_dir).mkdir()
        self.app = navigator.App(args)
        navigator.Handler.app = self.app
        navigator.Handler.remote_cfg = remote.RemoteConfig("", d)
        navigator.Handler.remote_store = remote.Store(d)
        navigator.Handler.limiter = remote.RateLimiter()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, actor="supervisor"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port, "X-Nav-Actor": actor}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def changes(self):
        return [c for c in self.app.changes.recent(limit=50)]

    def sup_dir(self):
        d = self.d / "sup"
        d.mkdir(exist_ok=True)
        return mock.patch.dict(os.environ, {"SUPERVISOR_DIR": str(d)}), d

    def test_reports_are_listed_and_read_from_the_supervisor_folder_only(self):
        env, d = self.sup_dir()
        with env:
            self.assertEqual(self.req("GET", "/api/reports")[1]["reports"], [])
            self.assertEqual(self.req("GET", "/api/report")[0], 404)               # none yet
            (d / "morning-report-2026-11-09.md").write_text("# Morning report 2026-11-09\nold\n")
            (d / "morning-report-2026-11-10.md").write_text("# Morning report 2026-11-10\n\n## Needs you first\n- **x**: y\n")
            (d / "digest.md").write_text("secret digest")
            (d / "morning-report-notes.md").write_text("not a report name")
            (d / "state.json").write_text("{}")
            (d.parent / "outside.md").write_text("outside")
            (d / "morning-report-2026-11-11.md").symlink_to(d.parent / "outside.md")
            st, out = self.req("GET", "/api/reports")
            self.assertEqual((st, [r["name"] for r in out["reports"]]), (200, ["morning-report-2026-11-10", "morning-report-2026-11-09"]))
            st, out = self.req("GET", "/api/report")                                # no name: the newest
            self.assertEqual((st, out["name"], out["ok"]), (200, "morning-report-2026-11-10", True))
            self.assertIn("## Needs you first", out["text"])
            self.assertEqual(self.req("GET", "/api/report?name=morning-report-2026-11-09")[1]["text"].splitlines()[1], "old")
            for bad in ("digest", "../outside", "..%2Foutside", "morning-report-2026-11-10.md", "state", "morning-report-2026-1-1"):
                self.assertEqual(self.req("GET", "/api/report?name=" + bad)[0], 400, bad)
            self.assertEqual(self.req("GET", "/api/report?name=morning-report-2020-01-01")[0], 404)
            self.assertEqual(self.req("GET", "/api/report?name=morning-report-2026-11-11")[0], 404)   # a link is never followed

    def test_a_report_is_cut_at_the_size_limit(self):
        env, d = self.sup_dir()
        with env, mock.patch.object(navigator, "REPORT_BYTES", 50):
            (d / "morning-report-2026-11-10.md").write_text("x" * 500)
            self.assertEqual(len(self.req("GET", "/api/report")[1]["text"]), 50)

    def test_the_alert_the_night_watchdog_raises_gets_its_open_button(self):
        env, d = self.sup_dir()
        with env:
            (d / "morning-report-2026-11-10.md").write_text("# r\n")
            st, out = self.req("POST", "/api/alerts/raise", {"title": "Morning report is ready", "body": str(d), "key": "morning-report-2026-11-10"})
            self.assertEqual((st, out["raised"]), (200, True))
            st, out = self.req("GET", "/api/alerts")
            self.assertEqual(out["open"][0]["link"], {"kind": "report", "ref": "morning-report-2026-11-10"})
            st, out = self.req("POST", "/api/alerts/raise", {"title": "T", "link": {"kind": "report", "ref": "morning-report-2026-11-12"}})
            self.assertEqual(out["alert"]["key"], "report:morning-report-2026-11-12")
            self.assertEqual(self.req("POST", "/api/alerts/raise", {"title": "T", "link": {"kind": "report", "ref": "../x"}})[0], 400)

    def test_raise_get_ack_with_change_log(self):
        st, out = self.req("GET", "/api/alerts")
        self.assertEqual((st, out["open"], out["acked"]), (200, [], []))
        st, out = self.req("POST", "/api/alerts/raise", {"title": "Decide pricing", "body": "P1",
                                                         "link": {"kind": "decision", "ref": "d1"}})
        self.assertEqual((st, out["ok"], out["raised"]), (200, True, True))
        aid = out["alert"]["id"]
        self.assertEqual(out["alert"]["raised_by"], "supervisor")
        st, again = self.req("POST", "/api/alerts/raise", {"title": "Decide pricing", "link": {"kind": "decision", "ref": "d1"}})
        self.assertEqual((st, again["raised"], again["alert"]["id"]), (200, False, aid))
        st, out = self.req("GET", "/api/alerts")
        self.assertEqual([a["id"] for a in out["open"]], [aid])
        st, out = self.req("POST", "/api/alerts/ack", {"id": aid}, actor="you")
        self.assertEqual((st, out["acked"]), (200, [aid]))
        st, out = self.req("GET", "/api/alerts")
        self.assertEqual((out["open"], out["acked"][0]["acked_by"]), ([], "you"))
        log = self.changes()
        self.assertEqual([(c["action"], c["actor"]) for c in log], [("alert.ack", "you"), ("alert.raise", "supervisor")])
        self.assertEqual(log[1]["after"], {"alerts": {aid: "open"}})      # one change per real raise, none for the dupe

    def test_ack_all_and_errors(self):
        for t in ("a", "b"):
            self.req("POST", "/api/alerts/raise", {"title": t})
        st, out = self.req("POST", "/api/alerts/ack", {"all": True})
        self.assertEqual((st, len(out["acked"])), (200, 2))
        self.assertEqual(self.req("POST", "/api/alerts/ack", {"id": "al-nope"})[0], 404)
        self.assertEqual(self.req("POST", "/api/alerts/ack", {})[0], 400)
        st, out = self.req("POST", "/api/alerts/raise", {"title": "x", "link": {"kind": "url", "ref": "file:///etc"}})
        self.assertEqual((st, out["error"]["code"]), (400, "bad_link"))

    def test_answering_the_decision_acks_its_alerts(self):
        d = self.app.inbox.ask("Which price for CDL?", ["A", "B"], decision_id="d1", priority=1, kind="choice")
        self.req("POST", "/api/alerts/raise", {"title": "Decide", "link": {"kind": "decision", "ref": d["id"]}})
        other = self.req("POST", "/api/alerts/raise", {"title": "Other", "key": "other"})[1]["alert"]["id"]
        st, out = self.req("POST", "/api/decisions/answer", {"id": "d1", "choice": "A"}, actor="you")
        self.assertEqual(st, 200)
        self.assertEqual(len(out["alerts_acked"]), 1)
        v = self.req("GET", "/api/alerts")[1]
        self.assertEqual([a["id"] for a in v["open"]], [other])
        self.assertEqual(v["acked"][0]["acked_by"], "you")
        ack = [c for c in self.changes() if c["action"] == "alert.ack"][0]
        self.assertEqual(ack["params"]["why"], "decision answered")

    def test_v1_decision_answer_acks_too(self):
        self.app.inbox.ask("Which price for CDL?", ["A", "B"], decision_id="d2", kind="choice")
        self.req("POST", "/api/alerts/raise", {"title": "Decide", "link": {"kind": "decision", "ref": "d2"}})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/nav/v1/actions/decision.answer", body=json.dumps({"id": "d2", "choice": "B"}),
                  headers={"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual(self.req("GET", "/api/alerts")[1]["open"], [])


class Cli(unittest.TestCase):
    class Client:
        def __init__(self, reply=None):
            self.posts, self.reply = [], reply or {"ok": True, "raised": True, "alert": {"id": "al-1", "title": "t"},
                                                   "acked": ["al-1"]}

        def get(self, path):
            assert path == "/api/alerts"
            return {"ok": True, "open": [{"id": "al-2", "title": "Decide", "raised_at": 1,
                                          "link": {"kind": "decision", "ref": "d1"}, "body": "P1"}],
                    "acked": [{"id": "al-1", "title": "Old", "raised_at": 1, "acked_at": 2, "acked_by": "you"}]}

        def post(self, path, payload):
            self.posts.append((path, payload))
            return dict(self.reply)

    def run_nav(self, client, *argv):
        out = io.StringIO()
        args = nav.build_parser().parse_args(list(argv))
        with contextlib.redirect_stdout(out):
            code = (nav.cmd_alerts if args.command == "alerts" else nav.cmd_alert)(args, client)
        return code, out.getvalue()

    def test_list_json_and_table(self):
        code, out = self.run_nav(self.Client(), "alerts")
        self.assertEqual((code, json.loads(out)["open"][0]["id"]), (0, "al-2"))
        code, out = self.run_nav(self.Client(), "alerts", "--table")
        self.assertIn("1 open alert", out)
        self.assertIn("OPEN", out)
        self.assertIn("-> decision d1", out)
        self.assertIn("acked", out)

    def test_raise_with_each_link_kind(self):
        c = self.Client()
        self.run_nav(c, "alert", "raise", "--title", "T", "--decision", "d1")
        self.run_nav(c, "alert", "raise", "--title", "T", "--session", "claude:x", "--body", "b", "--key", "k")
        code, out = self.run_nav(c, "alert", "raise", "--title", "T", "--url", "https://example.com", "--table")
        self.run_nav(c, "alert", "raise", "--title", "T")
        self.assertEqual([p for _, p in c.posts], [
            {"title": "T", "body": "", "link": {"kind": "decision", "ref": "d1"}, "key": ""},
            {"title": "T", "body": "b", "link": {"kind": "session", "ref": "claude:x"}, "key": "k"},
            {"title": "T", "body": "", "link": {"kind": "url", "ref": "https://example.com"}, "key": ""},
            {"title": "T", "body": "", "link": None, "key": ""}])
        self.assertEqual((code, out.strip()), (0, "raised al-1: t"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            nav.build_parser().parse_args(["alert", "raise", "--title", "T", "--decision", "d", "--session", "claude:x"])

    def test_raise_with_a_report_link_and_the_report_command(self):
        c = self.Client()
        self.run_nav(c, "alert", "raise", "--title", "T", "--report", "morning-report-2026-11-10")
        self.assertEqual(c.posts[-1][1]["link"], {"kind": "report", "ref": "morning-report-2026-11-10"})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            nav.build_parser().parse_args(["alert", "raise", "--title", "T", "--report", "r", "--decision", "d"])

        class Reports:
            def __init__(self):
                self.gets = []

            def get(self, path):
                self.gets.append(path)
                if path == "/api/reports":
                    return {"reports": [{"name": "morning-report-2026-11-10", "size": 1234, "mtime": time.time() - 7200}]}
                return {"ok": True, "name": "morning-report-2026-11-10", "text": "# Morning report\n- **x**: y\n"}
        r = Reports()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nav.cmd_report(nav.build_parser().parse_args(["report", "--table"]), r)
            nav.cmd_report(nav.build_parser().parse_args(["report", "2026-11-09", "--table"]), r)
            nav.cmd_report(nav.build_parser().parse_args(["report", "morning-report-2026-11-08"]), r)
            nav.cmd_report(nav.build_parser().parse_args(["report", "--list", "--table"]), r)
        self.assertEqual(r.gets, ["/api/report", "/api/report?name=morning-report-2026-11-09",
                                  "/api/report?name=morning-report-2026-11-08", "/api/reports"])
        text = out.getvalue()
        self.assertIn("- **x**: y", text)
        self.assertIn('"name": "morning-report-2026-11-10"', text)
        self.assertIn("morning-report-2026-11-10    1234 bytes  2h ago", text)

    def test_navigator_url_still_settable_before_the_command(self):
        a = nav.build_parser().parse_args(["--url", "http://127.0.0.1:8099", "alert", "raise", "--title", "T",
                                           "--url", "https://example.com"])
        self.assertEqual((a.url, a.link_url), ("http://127.0.0.1:8099", "https://example.com"))

    def test_ack_id_or_all(self):
        c = self.Client()
        self.run_nav(c, "alert", "ack", "al-1")
        code, out = self.run_nav(c, "alert", "ack", "--all", "--table")
        self.assertEqual([p for _, p in c.posts], [{"id": "al-1"}, {"all": True}])
        self.assertEqual(out.strip(), "acked: al-1")
        with self.assertRaises(nav.UsageError):
            self.run_nav(c, "alert", "ack")
        with self.assertRaises(nav.UsageError):
            self.run_nav(c, "alert", "ack", "al-1", "--all")


if __name__ == "__main__":
    unittest.main()


class AlarmMute(unittest.TestCase):
    """POST /api/alerts/mute writes the file the Mac voice (bin/alert-alarm.py) reads; GET /api/alerts reports it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(navigator, "ALARM_STATE", str(Path(self.tmp.name) / "alarm.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_mute_then_unmute(self):
        self.assertEqual(navigator.alarm_mute_until(), 0)
        with mock.patch.object(navigator.time, "time", return_value=NOW):
            r = navigator.alarm_mute(60)
        self.assertEqual(r["mute_until"], NOW + H)
        self.assertEqual(navigator.alarm_mute_until(), NOW + H)
        self.assertEqual(json.loads(Path(navigator.ALARM_STATE).read_text())["mute_until"], NOW + H)
        navigator.alarm_mute(0)
        self.assertEqual(navigator.alarm_mute_until(), 0)

    def test_bad_minutes_default_to_an_hour_and_cap_at_a_day(self):
        with mock.patch.object(navigator.time, "time", return_value=NOW):
            self.assertEqual(navigator.alarm_mute("x")["mute_until"], NOW + H)
            self.assertEqual(navigator.alarm_mute(10**6)["mute_until"], NOW + 24 * H)

    def test_mac_alarm_honours_the_same_file(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
        import importlib
        with mock.patch.dict(os.environ, {"ALERT_ALARM_STATE": navigator.ALARM_STATE}):
            aa = importlib.reload(importlib.import_module("alert-alarm"))
            navigator.alarm_mute(60)
            with mock.patch.object(aa.time, "localtime", return_value=aa.time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, -1))):
                self.assertEqual(aa.check(), "muted")
