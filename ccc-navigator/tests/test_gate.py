"""The gate in front of the owner (decisions.py, docs/supervisor.md "The gate"): every question says what kind it is,
approvals go ahead on their default after a day of notice (never for money, real people, clinical content, deleting,
access or legal), the same problem from several projects is one item, and problems wait with the supervisor while
one is on duty. HTTP routes, the change log, `nav ask/sort/defaults/decisions/decide --undo` and the page."""
import contextlib
import datetime
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import briefs  # noqa: E402
import decisions  # noqa: E402
import nav  # noqa: E402
import navigator  # noqa: E402
import remote  # noqa: E402
import voice  # noqa: E402
from decisions import DecisionError  # noqa: E402

DAY = 86400
NOW = datetime.datetime(2026, 11, 9, 12, 0).timestamp()      # noon, so "the end of today" is 12 hours away
HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


def day(offset=0, now=NOW):
    return (datetime.date.fromtimestamp(now) + datetime.timedelta(days=offset)).isoformat()


class Kinds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.wt = d / "wt.json"
        self.wt.write_text(json.dumps([
            {"id": "old-one", "lane": "aba", "priority": 2, "question": "Which landing page?", "options": ["A", "B"],
             "status": "open"},
            {"id": "old-price", "lane": "aba", "priority": 1, "question": "Which price?", "options": ["$349", "$79"],
             "status": "open"}]))
        self.inbox = decisions.Inbox(d / "own.json", d / "answers.json", self.wt)

    def ask(self, question="Turn the new onboarding on for everyone?", options=("turn on", "hold"), **kw):
        kw.setdefault("now", NOW)
        return self.inbox.ask(question, list(options), **kw)

    def approval(self, did="ap", **kw):
        kw.setdefault("default", "turn on")
        kw.setdefault("undo", "switch the flag off")
        return self.ask(decision_id=did, kind="approval", **kw)

    def code(self, fn, *a, **kw):
        with self.assertRaises(DecisionError) as cm:
            fn(*a, **kw)
        return cm.exception

    # ---- asking
    def test_a_question_without_a_kind_is_refused_with_the_way_to_fix_it(self):
        e = self.code(self.ask)
        self.assertEqual(e.code, "missing_kind")
        for word in ("--kind", "choice", "errand", "approval", "problem", "--default", "--undo", "--topic", "Example:"):
            self.assertIn(word, str(e))
        self.assertEqual(self.code(self.ask, kind="maybe").code, "bad_kind")
        self.assertEqual(self.inbox.list(now=NOW)["open"][0]["origin"], "watchtower")      # nothing was stored

    def test_choice_and_errand_wait_on_the_owner(self):
        self.ask(decision_id="c1", kind="choice", default="hold")
        self.ask("Log in to the store and press publish.", ["done"], decision_id="e1", kind="errand")
        by_id = {d["id"]: d for d in self.inbox.list(now=NOW + 30 * DAY)["open"]}
        for i in ("c1", "e1", "old-one"):
            self.assertEqual((by_id[i]["holder"], by_id[i]["for_uri"], by_id[i]["auto_at"]), ("uri", True, None), i)
        self.assertEqual(by_id["c1"]["default"], "hold")            # shown as the asker's suggestion only
        self.assertEqual(by_id["old-one"]["kind"], "")              # not sorted: his, as before
        self.assertEqual(self.inbox.due_defaults(NOW + 30 * DAY), [])

    def test_an_approval_needs_a_default_and_an_undo(self):
        self.assertEqual(self.code(self.ask, kind="approval", undo="switch it off").code, "bad_default")
        self.assertEqual(self.code(self.ask, kind="approval", default="ship", undo="switch it off").code, "bad_default")
        self.assertEqual(self.code(self.ask, kind="approval", default="turn on").code, "bad_undo")
        self.assertEqual(self.code(self.ask, kind="approval", default="turn on", undo="no").code, "bad_undo")
        d = self.approval()
        self.assertEqual((d["kind"], d["default"], d["undo"], d["deadline"]),
                         ("approval", "turn on", "switch the flag off", day(1)))     # no deadline given: tomorrow

    def test_an_approval_about_what_only_the_owner_decides_is_refused(self):
        for q, opts, area in (("Raise the price to 99?", ["raise", "hold"], "money"),
                              ("Start the campaign?", ["spend $50 a day", "hold"], "money"),
                              ("Email the waiting list about the launch?", ["go", "hold"], "messages to real people"),
                              ("Send it to the first 20 doctors?", ["go", "hold"], "messages to real people"),
                              ("Publish the new dosing table?", ["go", "hold"], "clinical content"),
                              ("Delete the old leads?", ["go", "hold"], "deleting data"),
                              ("Rotate the API key now?", ["go", "hold"], "accounts and access"),
                              ("Accept the new terms of service?", ["go", "hold"], "legal terms")):
            e = self.code(self.ask, q, opts, kind="approval", default=opts[0], undo="put it back as it was")
            self.assertEqual(e.code, "needs_uri", q)
            self.assertIn(area, str(e), q)
            self.assertIn("--kind choice", str(e))
            self.ask(q, opts, kind="choice", default=opts[0])      # the same question is fine as his choice
        for q in ("Turn the new onboarding on for everyone?", "Merge the faster search and put it live?",
                  "Switch the test server to the bigger machine image?", "Repost the changelog page?"):
            self.assertIsNone(decisions.guard(q), q)

    def test_a_problem_needs_a_topic(self):
        self.assertEqual(self.code(self.ask, kind="problem").code, "bad_topic")
        self.assertEqual(self.code(self.ask, kind="problem", topic="!").code, "bad_topic")
        self.assertEqual(self.ask(kind="problem", topic="Test server: FULL")["topic"], "test-server-full")

    # ---- defaults going ahead
    def test_when_a_default_goes_ahead(self):
        d = self.approval(deadline=day(0))
        end_of_today = datetime.datetime.combine(datetime.date.fromtimestamp(NOW) + datetime.timedelta(days=1),
                                                 datetime.time.min).timestamp()
        self.assertEqual(decisions.auto_at(d), NOW + DAY)                    # never sooner than a day after it was asked
        self.assertLess(end_of_today, NOW + DAY)
        self.assertEqual(decisions.auto_at(dict(d, deadline=day(3))), end_of_today + 3 * DAY)   # else when its day is over
        for broken in ({"kind": "choice"}, {"default": "ship"}, {"undo": ""}, {"deadline": None}, {"deadline": "soon"},
                       {"asked_at": None}, {"question": "Raise the price?"}, {"options": ["turn on", "refund all"]}):
            self.assertIsNone(decisions.auto_at(dict(d, **broken)), broken)

    def test_the_default_is_recorded_only_when_due_and_can_be_undone(self):
        self.approval()
        self.assertEqual(self.inbox.due_defaults(NOW + DAY - 60), [])
        self.assertEqual(self.code(self.inbox.apply_default, "ap", NOW + DAY - 60).code, "not_due")
        self.assertEqual(self.code(self.inbox.apply_default, "old-one", NOW + 30 * DAY).code, "not_due")
        later = NOW + 2 * DAY
        self.assertEqual([d["id"] for d in self.inbox.due_defaults(later)], ["ap"])
        rec, before = self.inbox.apply_default("ap", later)
        self.assertIsNone(before)
        self.assertEqual((rec["choice"], rec["by"], rec["auto"], rec["undo"]), ("turn on", "default", True, "switch the flag off"))
        self.assertIn("default applies", decisions.message_for(self.inbox.get("ap"), rec))
        lst = self.inbox.list(now=later + 60)
        self.assertEqual([w["id"] for w in lst["went_ahead"]], ["ap"])
        self.assertEqual((lst["went_ahead"][0]["choice"], lst["went_ahead"][0]["undo"]), ("turn on", "switch the flag off"))
        self.assertNotIn("ap", [d["id"] for d in lst["open"]])
        self.assertEqual(self.inbox.due_defaults(later + 60), [])

        rec2, was = self.inbox.undo_default("ap", "we are not ready", by="you", now=later + 120)
        self.assertEqual(was["choice"], "turn on")
        self.assertEqual((rec2["choice"], rec2["auto"], rec2["undone"]["choice"]), ("other", False, "turn on"))
        for part in ("Undo this", "turn on", "switch the flag off", "we are not ready"):
            self.assertIn(part, rec2["note"])
        self.assertIn("undid a default", decisions.message_for(self.inbox.get("ap"), rec2))
        self.assertEqual(self.inbox.list(now=later + 180)["went_ahead"], [])
        self.assertEqual(self.code(self.inbox.undo_default, "ap").code, "not_auto")          # already undone
        self.assertEqual(self.code(self.inbox.undo_default, "old-one").code, "not_auto")

    def test_went_ahead_is_listed_for_a_week(self):
        self.approval()
        self.inbox.apply_default("ap", NOW + 2 * DAY)
        self.assertEqual(len(self.inbox.list(now=NOW + 8 * DAY)["went_ahead"]), 1)
        self.assertEqual(self.inbox.list(now=NOW + 10 * DAY)["went_ahead"], [])

    def test_an_answer_beats_the_default(self):
        self.approval()
        self.inbox.answer("ap", "hold", by="you", now=NOW + 3600)
        self.assertEqual(self.inbox.due_defaults(NOW + 5 * DAY), [])
        self.assertEqual(self.code(self.inbox.apply_default, "ap", NOW + 5 * DAY).code, "answered")
        self.assertEqual(self.inbox.answers()["ap"]["choice"], "hold")

    def test_a_reopened_approval_waits_for_an_answer(self):
        self.approval()
        self.inbox.apply_default("ap", NOW + 2 * DAY)
        self.inbox.reopen("ap", by="you", now=NOW + 2 * DAY + 60)
        d = next(x for x in self.inbox.list(now=NOW + 9 * DAY)["open"] if x["id"] == "ap")
        self.assertEqual((d["kind"], d["holder"], d["auto_at"], d["default"]), ("choice", "uri", None, "turn on"))
        self.assertEqual(self.inbox.due_defaults(NOW + 9 * DAY), [])

    def test_the_switch_stops_defaults_from_being_announced(self):
        self.approval()
        self.assertTrue(self.inbox.auto_on())
        d = next(x for x in self.inbox.list(now=NOW)["open"] if x["id"] == "ap")
        self.assertEqual((d["holder"], d["for_uri"]), ("auto", False))
        self.assertTrue(self.inbox.set_auto(False, by="you", now=NOW))
        lst = self.inbox.list(now=NOW)
        d = next(x for x in lst["open"] if x["id"] == "ap")
        self.assertEqual((lst["auto_defaults"], d["holder"], d["for_uri"]), (False, "uri", True))
        self.assertFalse(self.inbox.set_auto(True))
        self.assertEqual(self.inbox.list(now=NOW)["counts"], {"uri": 2, "auto": 1, "supervisor": 0, "dupe": 0})

    # ---- one item per problem, and the supervisor's desk
    def problems(self):
        self.ask("Test server is full, builds fail.", ["I'll look"], decision_id="p-bb", kind="problem",
                 topic="test-server-full", lane="boardsbridge")
        self.ask("No space left on the test server.", ["I'll look", "buy a bigger one"], decision_id="p-nb",
                 kind="problem", topic="test-server-full", lane="nursebridge", now=NOW + 60)
        self.ask("Cannot deploy: test server disk full.", ["ok"], decision_id="p-cdl", kind="problem",
                 topic="Test server full", lane="cdl", now=NOW + 120)
        self.ask("Groq is rate limiting us.", ["I'll look"], decision_id="p-groq", kind="problem", topic="groq-limits")

    def test_the_same_problem_is_one_item_and_goes_to_the_owner_without_a_supervisor(self):
        self.problems()
        lst = self.inbox.list(now=NOW + 600)
        by_id = {d["id"]: d for d in lst["open"]}
        lead = by_id["p-bb"]
        self.assertEqual((lead["holder"], lead["for_uri"], lead["escalated"]), ("uri", True, "no supervisor on duty"))
        self.assertEqual([m["id"] for m in lead["dupes"]], ["p-nb", "p-cdl"])
        self.assertEqual([m["lane"] for m in lead["dupes"]], ["nursebridge", "cdl"])
        for i in ("p-nb", "p-cdl"):
            self.assertEqual((by_id[i]["holder"], by_id[i]["for_uri"], by_id[i]["dupe_of"]), ("dupe", False, "p-bb"))
        self.assertEqual((by_id["p-groq"]["holder"], by_id["p-groq"]["dupes"]), ("uri", []))
        self.assertEqual(lst["counts"], {"uri": 4, "auto": 0, "supervisor": 0, "dupe": 2})
        self.assertFalse(lst["desk_open"])

    def test_the_ranked_question_stands_for_its_problem(self):
        self.problems()
        self.inbox.set_ranks(["p-cdl"], why={"p-cdl": "blocks three projects"})
        by_id = {d["id"]: d for d in self.inbox.list(now=NOW + 600)["open"]}
        self.assertEqual((by_id["p-cdl"]["holder"], by_id["p-cdl"]["rank"]), ("uri", 1))
        self.assertEqual(sorted(m["id"] for m in by_id["p-cdl"]["dupes"]), ["p-bb", "p-nb"])
        self.assertEqual(by_id["p-bb"]["dupe_of"], "p-cdl")

    def test_with_a_supervisor_on_duty_problems_wait_on_its_desk(self):
        self.problems()
        lst = self.inbox.list(now=NOW + 600, desk_open=True)
        by_id = {d["id"]: d for d in lst["open"]}
        self.assertEqual((by_id["p-bb"]["holder"], by_id["p-bb"]["for_uri"], by_id["p-bb"]["escalated"]), ("supervisor", False, ""))
        self.assertEqual(lst["counts"], {"uri": 2, "auto": 0, "supervisor": 2, "dupe": 2})
        self.assertTrue(lst["desk_open"])
        late = {d["id"]: d for d in self.inbox.list(now=NOW + 3 * DAY, desk_open=True)["open"]}
        self.assertEqual((late["p-bb"]["holder"], late["p-bb"]["escalated"]), ("uri", "3 days with the supervisor, not solved"))

    def test_an_overdue_problem_goes_to_the_owner(self):
        self.ask("Mail provider is down.", ["I'll look"], decision_id="p-due", kind="problem", topic="mail-down",
                 deadline=day(0))
        d = next(x for x in self.inbox.list(now=NOW + DAY + 3600, desk_open=True)["open"] if x["id"] == "p-due")
        self.assertEqual((d["holder"], d["escalated"]), ("uri", "past its deadline"))

    def test_same_problem_lists_the_other_open_questions(self):
        self.problems()
        self.assertEqual(sorted(q["id"] for q in self.inbox.same_problem("p-nb")), ["p-bb", "p-cdl"])
        self.inbox.answer("p-cdl", "ok")
        self.assertEqual([q["id"] for q in self.inbox.same_problem("p-nb")], ["p-bb"])
        self.assertEqual(self.inbox.same_problem("p-groq"), [])
        self.assertEqual(self.inbox.same_problem("old-one"), [])

    # ---- sorting a question that is already there
    def test_sort_labels_a_question_without_touching_its_source(self):
        before = self.wt.read_text()
        rec, was = self.inbox.sort("old-one", "problem", topic="landing page", by="supervisor", now=NOW)
        self.assertIsNone(was)
        self.assertEqual((rec["kind"], rec["topic"], rec["by"]), ("problem", "landing-page", "supervisor"))
        d = self.inbox.get("old-one")
        self.assertEqual((d["kind"], d["topic"], d["sorted_by"], d["sorted_at"]), ("problem", "landing-page", "supervisor", NOW))
        self.assertEqual(self.wt.read_text(), before)
        rec2, was2 = self.inbox.sort("old-one", "errand", now=NOW + 60)
        self.assertEqual((was2["kind"], rec2["kind"]), ("problem", "errand"))
        self.assertNotIn("topic", self.inbox.get("old-one"))                 # what the old kind carried is gone

    def test_sorting_as_an_approval_starts_a_day_of_notice(self):
        rec, _ = self.inbox.sort("old-one", "approval", default="A", undo="switch back to B", now=NOW)
        self.assertEqual(rec["deadline"], day(1))
        d = self.inbox.get("old-one")
        self.assertGreaterEqual(decisions.auto_at(d), NOW + DAY)
        self.assertEqual(self.inbox.due_defaults(NOW + DAY - 60), [])

    def test_sort_refusals(self):
        self.assertEqual(self.code(self.inbox.sort, "nope", "choice").code, "unknown_decision")
        self.assertEqual(self.code(self.inbox.sort, "old-one", "").code, "missing_kind")
        self.assertEqual(self.code(self.inbox.sort, "old-one", "approval", default="C", undo="put it back").code, "bad_default")
        self.assertEqual(self.code(self.inbox.sort, "old-price", "approval", default="$79", undo="put it back").code, "needs_uri")
        self.assertEqual(self.code(self.inbox.sort, "old-one", "choice", deadline="soon").code, "bad_deadline")
        self.inbox.answer("old-one", "A")
        self.assertEqual(self.code(self.inbox.sort, "old-one", "choice").code, "unknown_decision")

    def test_a_question_written_as_an_approval_elsewhere_is_still_checked(self):
        self.wt.write_text(json.dumps([{"id": "sneaky", "kind": "approval", "question": "Refund everyone?", "options": ["yes", "no"],
                                        "default": "yes", "undo": "charge them again", "deadline": day(-9), "asked_at": NOW - 9 * DAY,
                                        "status": "open"}]))
        d = self.inbox.list(now=NOW)["open"][0]
        self.assertEqual((d["holder"], d["auto_at"]), ("uri", None))
        self.assertEqual(self.inbox.due_defaults(NOW), [])

    def test_the_voice_brief_reads_only_what_is_the_owners(self):
        self.approval()
        self.problems()
        b = voice.briefing({"repos": []}, self.inbox.list(now=NOW, desk_open=True))
        ids = [i["id"] for i in b["items"] if i["kind"] == "decision"]
        self.assertEqual(sorted(ids), ["old-one", "old-price"])


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        (d / "sup").mkdir()
        env = mock.patch.dict(os.environ, {"CCC_NAV_CHANGES": str(d / "changes.jsonl"),
                                           "CCC_NAV_DECISIONS": str(d / "decisions.json"),
                                           "CCC_NAV_ANSWERS": str(d / "answers.json"),
                                           "CCC_NAV_WATCHTOWER_DECISIONS": str(d / "wt.json"),
                                           "CCC_NAV_ALERTS": str(d / "alerts.json"),
                                           "SUPERVISOR_DIR": str(d / "sup")})
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

    def req(self, method, path, body=None, actor="owner-bb"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port, "X-Nav-Actor": actor}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def nav(self, *argv, actor=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"NAV_ACTOR": actor} if actor else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = nav.main(["--url", "http://127.0.0.1:%d" % self.port] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def actions(self):
        return [c["action"] for c in self.app.changes.recent(limit=50)]

    def old_approval(self, did="ap", days=3):
        """An approval asked `days` ago whose day is over: its default is due now."""
        t = datetime.datetime.now().timestamp() - days * DAY
        return self.app.inbox.ask("Turn the new onboarding on for everyone?", ["turn on", "hold"], decision_id=did,
                                  kind="approval", default="turn on", undo="switch the flag off", now=t, by="owner-bb")

    def test_ask_needs_a_kind_and_stores_it(self):
        st, r = self.req("POST", "/api/decisions/ask", {"question": "Turn the new onboarding on?", "options": ["on", "hold"]})
        self.assertEqual((st, r["error"]["code"]), (400, "missing_kind"))
        self.assertIn("--kind approval", r["error"]["message"])
        st, r = self.req("POST", "/api/decisions/ask", {"question": "Raise the price?", "options": ["on", "hold"],
                                                        "kind": "approval", "default": "on", "undo": "set it back"})
        self.assertEqual((st, r["error"]["code"]), (400, "needs_uri"))
        st, r = self.req("POST", "/api/decisions/ask", {"question": "Turn the new onboarding on?", "options": ["on", "hold"],
                                                        "kind": "approval", "default": "on", "undo": "switch it off", "id": "a1"})
        self.assertEqual(st, 200, r)
        st, lst = self.req("GET", "/api/decisions")
        d = lst["open"][0]
        self.assertEqual((d["id"], d["kind"], d["holder"], d["for_uri"], d["default"]), ("a1", "approval", "auto", False, "on"))
        self.assertEqual((lst["counts"]["auto"], lst["auto_defaults"], lst["desk_open"], lst["went_ahead"]), (1, True, False, []))

    def test_a_due_default_goes_ahead_is_logged_and_can_be_undone(self):
        self.old_approval()
        st, r = self.req("POST", "/api/decisions/defaults", {"run": True, "dry_run": True})
        self.assertEqual((st, r["dry_run"], r["applied"]), (200, True, [{"id": "ap", "choice": "turn on"}]))
        self.assertEqual(self.app.inbox.answers(), {})
        r = self.app.apply_defaults()                      # what the timer in main() calls
        self.assertEqual([(a["id"], a["choice"], a["delivery"]) for a in r["applied"]], [("ap", "turn on", "recorded")])
        ch = self.app.changes.recent(limit=5)[0]
        self.assertEqual((ch["action"], ch["actor"], ch["undoable"]), ("decision.default", "default", False))
        self.assertEqual(self.app.apply_defaults()["applied"], [])           # once
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual([(w["id"], w["choice"], w["undo"]) for w in lst["went_ahead"]], [("ap", "turn on", "switch the flag off")])
        self.assertEqual(lst["open"], [])

        st, r = self.req("POST", "/api/decisions/undo-default", {"id": "ap", "note": "not yet"}, actor="you")
        self.assertEqual((st, r["answer"]["choice"], r["answer"]["undone"]["choice"], r["delivery"]), (200, "other", "turn on", "recorded"))
        self.assertEqual(self.actions()[0], "decision.undo_default")
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual(lst["went_ahead"], [])
        st, r = self.req("POST", "/api/decisions/undo-default", {"id": "ap"})
        self.assertEqual((st, r["error"]["code"]), (400, "not_auto"))

    def test_the_default_tells_the_asking_session(self):
        t = datetime.datetime.now().timestamp() - 3 * DAY
        self.app.inbox.ask("Turn the new onboarding on for everyone?", ["turn on", "hold"], decision_id="ap", kind="approval",
                           default="turn on", undo="switch the flag off", now=t, session="claude:abc")
        with mock.patch.object(self.app, "deliver", return_value="sent") as deliver:
            r = self.app.apply_defaults()
        self.assertEqual(r["applied"][0]["delivery"], "sent")
        (sid, text), _ = deliver.call_args
        self.assertEqual(sid, "claude:abc")
        for part in ("No answer from Uri by the deadline", "turn on", "switch the flag off", "he can still undo it"):
            self.assertIn(part, text)

    def test_switched_off_nothing_goes_ahead(self):
        self.old_approval()
        st, r = self.req("POST", "/api/decisions/defaults", {"on": False}, actor="you")
        self.assertEqual((st, r["on"], r["was"]), (200, False, True))
        self.assertEqual(self.app.apply_defaults(), {"ok": True, "on": False, "dry_run": False, "applied": []})
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual((lst["auto_defaults"], lst["open"][0]["holder"]), (False, "uri"))
        self.req("POST", "/api/decisions/defaults", {"on": True}, actor="you")
        self.assertEqual(self.actions()[:2], ["decision.defaults_on", "decision.defaults_off"])
        self.assertEqual(len(self.app.apply_defaults()["applied"]), 1)
        st, r = self.req("POST", "/api/decisions/defaults", {})
        self.assertEqual((st, r["error"]["code"]), (400, "bad_switch"))

    def test_an_answer_given_first_wins(self):
        self.old_approval()
        self.req("POST", "/api/decisions/answer", {"id": "ap", "choice": "hold"}, actor="you")
        self.assertEqual(self.app.apply_defaults()["applied"], [])
        self.assertEqual(self.app.inbox.answers()["ap"]["choice"], "hold")

    def test_reopen_after_a_default_does_not_fire_it_again(self):
        self.old_approval()
        self.app.apply_defaults()
        st, r = self.req("POST", "/api/decisions/reopen", {"id": "ap"}, actor="you")
        self.assertEqual(st, 200, r)
        self.assertEqual(self.app.apply_defaults()["applied"], [])
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual((lst["open"][0]["kind"], lst["open"][0]["holder"]), ("choice", "uri"))

    def test_one_answer_covers_the_same_problem_from_other_projects(self):
        for did, opts, lane in (("p1", ["I'll look"], "bb"), ("p2", ["I'll look", "bigger server"], "nb"), ("p3", ["ok"], "cdl")):
            self.app.inbox.ask("The test server is full.", opts, decision_id=did, kind="problem", topic="test-server-full", lane=lane)
        st, r = self.req("POST", "/api/decisions/answer", {"id": "p1", "choice": "I'll look", "note": "cleaning it now"}, actor="you")
        self.assertEqual((st, sorted(r["covered"])), (200, ["p2", "p3"]))
        a = self.app.inbox.answers()
        self.assertEqual((a["p2"]["choice"], a["p2"]["same_as"]), ("I'll look", "p1"))
        self.assertEqual(a["p3"]["choice"], "other")                 # "I'll look" is not one of its options
        for part in ("Same problem as p1", "I'll look", "cleaning it now"):
            self.assertIn(part, a["p3"]["note"])
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual(lst["open"], [])

    def test_sort_over_http_and_the_change_log(self):
        (self.d / "wt.json").write_text(json.dumps([{"id": "legacy", "question": "Which landing page?", "options": ["A", "B"], "status": "open"}]))
        st, r = self.req("POST", "/api/decisions/sort", {"id": "legacy", "kind": "errand"}, actor="copilot")
        self.assertEqual((st, r["sorted"]["kind"], r["sorted"]["by"], r["decision"]["kind"]), (200, "errand", "copilot", "errand"))
        ch = self.app.changes.recent(limit=5)[0]
        self.assertEqual((ch["action"], ch["actor"], ch["undoable"]), ("decision.sort", "copilot", False))
        st, r = self.req("POST", "/api/decisions/sort", {"id": "legacy", "kind": "approval"})
        self.assertEqual((st, r["error"]["code"]), (400, "bad_default"))

    def test_the_desk_is_open_only_while_a_supervisor_runs(self):
        state = self.d / "sup" / "state.json"
        now = datetime.datetime.now().astimezone()
        self.assertFalse(navigator.desk_open())                                          # no supervisor state at all
        state.write_text(json.dumps({"paused": True, "last_cycle": now.isoformat()}))
        self.assertFalse(navigator.desk_open())
        state.write_text(json.dumps({"paused": False, "last_cycle": (now - datetime.timedelta(hours=3)).isoformat()}))
        self.assertFalse(navigator.desk_open())                                          # it stopped cycling
        state.write_text(json.dumps({"paused": False, "last_cycle": "not a date"}))
        self.assertFalse(navigator.desk_open())
        state.write_text(json.dumps({"paused": False, "last_cycle": (now - datetime.timedelta(minutes=20)).isoformat()}))
        self.assertTrue(navigator.desk_open())
        self.app.inbox.ask("The test server is full.", ["I'll look"], decision_id="p1", kind="problem", topic="test-server-full")
        st, lst = self.req("GET", "/api/decisions")
        self.assertEqual((lst["desk_open"], lst["open"][0]["holder"], lst["counts"]["uri"]), (True, "supervisor", 0))

    def test_v1_actions(self):
        st, r = self.req("POST", "/nav/v1/actions/decision.ask", {"question": "Turn the new onboarding on?", "options": ["on", "hold"]})
        self.assertEqual(st, 400)
        self.assertIn("--kind approval", json.dumps(r))
        st, r = self.req("POST", "/nav/v1/actions/decision.ask", {"question": "Turn the new onboarding on?", "options": ["on", "hold"],
                                                                 "kind": "choice", "id": "c1"})
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/nav/v1/actions/decision.sort", {"id": "c1", "kind": "problem", "topic": "onboarding"})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.app.inbox.get("c1")["kind"], "problem")
        self.old_approval()
        self.app.apply_defaults()
        st, r = self.req("POST", "/nav/v1/actions/decision.undo-default", {"id": "ap"})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.app.inbox.answers()["ap"]["choice"], "other")

    # ---- the CLI (nav.py)
    def test_cli_ask_teaches_the_kinds(self):
        code, out, err = self.nav("ask", "--question", "Turn the new onboarding on?", "--option", "on", "--option", "hold")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        for part in ("--kind", "approval", "--default", "--undo", "Example: nav.py ask --kind approval"):
            self.assertIn(part, err)
        self.assertIn("\n  choice ", err)                         # the text keeps its lines: an agent can read it
        code, out, err = self.nav("ask", "--kind", "approval", "--question", "Turn the new onboarding on?", "--option", "on",
                                  "--option", "hold", "--default", "on", "--undo", "switch it off", "--id", "a1", actor="owner-bb")
        self.assertEqual(code, 0, err)
        d = self.app.inbox.get("a1")
        self.assertEqual((d["kind"], d["default"], d["undo"], d["asked_by"]), ("approval", "on", "switch it off", "owner-bb"))

    def test_cli_sort_decisions_and_defaults(self):
        (self.d / "wt.json").write_text(json.dumps([
            {"id": "legacy", "question": "Which landing page?", "options": ["A", "B"], "status": "open"},
            {"id": "srv-a", "lane": "bb", "question": "Test server is full.", "options": ["ok"], "status": "open"},
            {"id": "srv-b", "lane": "nb", "question": "No space on the test server.", "options": ["ok"], "status": "open"}]))
        for did in ("srv-a", "srv-b"):
            code, out, err = self.nav("sort", did, "--kind", "problem", "--topic", "test-server-full", actor="copilot")
            self.assertEqual(code, 0, err)
        self.old_approval()
        code, out, err = self.nav("decisions", "--for", "uri")
        got = json.loads(out)
        self.assertEqual((sorted(d["id"] for d in got["decisions"]), got["counts"]),
                         (["legacy", "srv-a"], {"uri": 2, "auto": 1, "supervisor": 0, "dupe": 1}))
        code, out, err = self.nav("decisions", "--table")
        for part in ("not sorted (nav sort legacy --kind ...)", "problem: waits on Uri (no supervisor on duty); also asked by: nb",
                     "same problem as srv-a", 'goes ahead with "turn on" on '):
            self.assertIn(part, out)
        code, out, err = self.nav("decisions", "--for", "auto")
        self.assertEqual([d["id"] for d in json.loads(out)["decisions"]], ["ap"])

        code, out, err = self.nav("defaults")
        st = json.loads(out)
        self.assertEqual((st["on"], [w["id"] for w in st["waiting"]], st["went_ahead"]), (True, ["ap"], []))
        code, out, err = self.nav("defaults", "run", "--dry-run")
        self.assertEqual(json.loads(out)["applied"], [{"id": "ap", "choice": "turn on"}])
        code, out, err = self.nav("defaults", "off", actor="you")
        self.assertEqual((code, self.app.inbox.auto_on()), (0, False))
        code, out, err = self.nav("defaults", "run")
        self.assertEqual(json.loads(out)["applied"], [])
        self.nav("defaults", "on", actor="you")
        code, out, err = self.nav("defaults", "run")
        self.assertEqual([a["id"] for a in json.loads(out)["applied"]], ["ap"])
        code, out, err = self.nav("decisions", "--went-ahead")
        self.assertEqual([w["id"] for w in json.loads(out)["went_ahead"]], ["ap"])
        code, out, err = self.nav("decisions", "--went-ahead", "--table")
        self.assertIn("nav decide ap --undo", out)
        code, out, err = self.nav("decide", "ap", "--undo", "--note", "not yet", actor="you")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.app.inbox.answers()["ap"]["undone"]["choice"], "turn on")
        code, out, err = self.nav("decisions", "--went-ahead", "--table")
        self.assertIn("no default went ahead", out)


class Page(unittest.TestCase):
    def test_only_the_owners_questions_are_counted(self):
        self.assertIn("const mine = () => (decs.open || []).filter(d => d.for_uri !== false);", HTML)
        self.assertIn("const n = mineN(),", HTML)                                   # the sidebar badge
        self.assertIn('<h2>Decisions <span class="n">${open.length}</span></h2>', HTML)
        self.assertIn("(ui.inbox || mineN()) ? inboxHtml() : overview()", HTML)
        self.assertNotIn("decs.open.length", HTML)

    def test_the_free_text_box_stays_closed_until_other_is_pressed(self):
        self.assertIn(".dother[hidden]{display:none}", HTML)       # display:flex on .dother used to beat `hidden`

    def test_the_sections_and_their_buttons(self):
        for part in ("Going ahead unless you object", "Went ahead without you, last 7 days", "On the supervisor's desk",
                     'data-undo-default="${esc(w.id)}"', 'data-defaults="off"', 'data-defaults="on"',
                     "/api/decisions/undo-default", "/api/decisions/defaults", "d.holder === 'auto'", "d.holder === 'supervisor'",
                     "Also raised by", "The owner suggests", "Yours because: "):
            self.assertIn(part, HTML)
        self.assertIn("undoDefault(ud.dataset.undoDefault)", HTML)
        self.assertIn("setDefaults(sw.dataset.defaults === 'on')", HTML)

    def test_an_alert_for_a_repeated_problem_opens_the_card_that_stands_for_it(self):
        op = HTML[HTML.index("async function openAlert"):]
        self.assertIn("dupe ? dupe.dupe_of : l.ref", op[:1200])

    def test_the_change_log_names_the_new_actions(self):
        for action in ("decision.sort", "decision.default", "decision.undo_default", "decision.defaults_on", "decision.defaults_off"):
            self.assertIn("action === '%s'" % action, HTML)


if __name__ == "__main__":
    unittest.main()
