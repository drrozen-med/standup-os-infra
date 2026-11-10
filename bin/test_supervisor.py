"""Tests for bin/supervisor.py (docs/supervisor.md): wake rules, never-wake rules, caps, quiet hours, pause,
dry run, register, digest, error isolation, atomic writes, manual wake, CLI.

unittest only; the Navigator HTTP calls and subprocess.run are mocked and every path is a temp dir,
so nothing here touches a real session or Uri's real state.
Run: python3 bin/test_supervisor.py
"""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("supervisor", os.path.join(HERE, "supervisor.py"))
_spec = importlib.util.spec_from_loader("supervisor", _loader)
sup = importlib.util.module_from_spec(_spec)
_loader.exec_module(sup)
owner = sup.owner

NOON = datetime.datetime(2026, 11, 3, 12, 0, 0).timestamp()     # local noon
TODAY = "2026-11-03"
MIN = 60
DAY = 86400
P1, P2 = "/Users/x/projects/alpha", "/Users/x/projects/beta"
K1, K2 = "claude:aaaa-1", "claude:bbbb-2"


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def status(**kw):
    s = {"updated_at": iso(NOON - 60 * MIN), "session": K1, "state": "working", "summary": "moving PR #12",
         "doing": "reviewing #12", "next": "merge #12", "shipped": [], "needs_uri": [], "blocked": None}
    s.update(kw)
    return s


class FakeNav:
    """get() router: /api/tree (owners + session items) and /api/decisions."""

    def __init__(self):
        self.owners = {}        # project key -> owner info (tree `owners`)
        self.items = {}         # session key -> minutes since last activity
        self.open = []          # open decisions
        self.answered = []      # [(id, answer)]
        self.alerts = {"open": [], "acked": []}     # GET /api/alerts
        self.down = False

    def __call__(self, path):
        if self.down:
            raise OSError("connection refused")
        if path.startswith("/api/tree"):
            now = FakeNav.now
            items = [{"key": k, "engine": "claude", "state": "resting", "live": False, "mtime": now - m * MIN}
                     for k, m in self.items.items()]
            return {"owners": self.owners, "repos": [{"key": "r", "name": "r", "groups": [{"items": items}]}]}
        if path == "/api/decisions":
            return {"ok": True, "open": self.open, "answered": [dict(id=i, answer=a) for i, a in self.answered]}
        if path == "/api/alerts":
            return dict(self.alerts, ok=True)
        raise AssertionError("unexpected navigator call: " + path)


FakeNav.now = NOON


class FakeRun:
    def __init__(self, code=0):
        self.calls, self.code, self.stdout = [], code, '{"sent": true}'

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=self.code, stdout=self.stdout, stderr="")

    def sends(self):
        return [a for a, _ in self.calls if len(a) > 2 and a[2] == "send"]

    def raises(self):
        return [a for a, _ in self.calls if a[2:4] == ["alert", "raise"]]


class FakeOsrc:
    """outsourcerer.sh: a launch gets the next id (job-1, job-2, ...); `status --json` answers from self.jobs."""

    def __init__(self):
        self.calls, self.jobs, self.n = [], {}, 0       # jobs: id -> {"status", "cwd", "started", "model"}
        self.fail = set()                               # providers whose launch fails
        self.slow = False                               # a launch runs into its time limit
        self.blind = False                              # the listing cannot be read

    def __call__(self, args, cwd=None, timeout=90):
        args = list(args)
        self.calls.append((args, cwd))
        if args[:2] == ["status", "--json"]:
            if len(args) == 2:
                return (1, "", "boom") if self.blind else (0, json.dumps({"jobs": [dict(j, job_id=i) for i, j in self.jobs.items()]}), "")
            j = self.jobs.get(args[2])
            return (0, json.dumps(dict(j, job_id=args[2])), "") if j and not self.blind else (1, "", "no such job")
        if args[0] == "cancel":
            if args[1] in self.jobs:
                self.jobs[args[1]]["status"] = "canceled"
            return 0, "", ""
        if args[0] == "--provider":
            if self.slow:
                return 124, "", "timed out after 120 s"
            if args[1] in self.fail:
                return 1, "", "[outsourcerer] %s: quota exceeded" % args[1]
            self.n += 1
            jid = "job-%d" % self.n
            self.jobs[jid] = {"status": "running", "cwd": cwd, "started": FakeNav.now, "model": args[5]}
            return 0, jid + "\n", "[outsourcerer] job %s launched (default lane: %s)." % (jid, args[1])
        raise AssertionError("unexpected outsourcerer call: %r" % args[:3])

    def launches(self):
        return [a for a, _ in self.calls if a[0] == "--provider"]

    def cancels(self):
        return [a[1] for a, _ in self.calls if a[0] == "cancel"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.owners_dir, self.sup_dir = os.path.join(d, "owners"), os.path.join(d, "sup")
        self.answers = os.path.join(d, "answers.json")
        env = mock.patch.dict(os.environ, {"OWNER_DIR": self.owners_dir, "SUPERVISOR_DIR": self.sup_dir,
                                           "CCC_NAV_ANSWERS": self.answers,
                                           "WATCHTOWER_GITHUB_JSON": os.path.join(d, "github.json")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SUPERVISOR_DRY_RUN", None)
        os.environ.pop("NAV_ACTOR", None)
        self.nav = FakeNav()
        self.runs = FakeRun()
        self.busy = set()       # owner keys whose session a process is running right now
        self.osrc = FakeOsrc()
        self.mac_busy = ""      # why the Mac cannot take one more job
        for p in (mock.patch.object(sup, "get", self.nav), mock.patch.object(sup.subprocess, "run", self.runs),
                  mock.patch.object(sup, "process_running", lambda key: key in self.busy),
                  mock.patch.object(sup, "osrc_run", self.osrc), mock.patch.object(sup, "overloaded", lambda: self.mac_busy)):
            p.start()
            self.addCleanup(p.stop)
        FakeNav.now = NOON

    # -- fixtures
    def add_owner(self, pkey, key, st=None, idle_min=45, problem=""):
        slug = owner.slug_of(pkey)
        if st is not None:
            os.makedirs(os.path.join(self.owners_dir, slug), exist_ok=True)
            with open(os.path.join(self.owners_dir, slug, "status.json"), "w") as f:
                json.dump(dict(st, session=key), f)
        self.nav.owners[pkey] = {"key": key, "summary": (st or {}).get("summary", ""), "state": (st or {}).get("state", ""),
                                 "updated_at": (st or {}).get("updated_at"), "stale": bool(problem) or st is None,
                                 "problem": problem or ("" if st else "no status yet")}
        if idle_min is not None:
            self.nav.items[key] = idle_min

    def run_cycle(self, now=NOON, dry=False):
        FakeNav.now = now
        return sup.cycle(dry, now=now)

    # -- readers
    def text(self, name):
        with open(os.path.join(self.sup_dir, name)) as f:
            return f.read()

    def jfile(self, name):
        with open(os.path.join(self.sup_dir, name)) as f:
            return json.load(f)

    def reg(self, slug="alpha"):
        return {r["project"]: r for r in self.jfile("register.json")["rows"]}[slug]

    def cycles(self):
        with open(os.path.join(self.sup_dir, "cycles.log")) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def sent_to(self):
        return [a[3] for a in self.runs.sends()]

    def sent_text(self, i=-1):
        return self.runs.sends()[i][4]

    def log_md(self, slug="alpha"):
        try:
            with open(os.path.join(self.owners_dir, slug, "log.md")) as f:
                return f.read()
        except OSError:
            return ""


class WakeRules(Base):
    def test_answered_quotes_choice_and_note_verbatim(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d-one"]), idle_min=10)
        ans = {"choice": "Ship it", "note": 'use the "blue" plan,\nnot the red one', "by": "you",
               "ts": NOON - 5 * MIN, "question": "Which plan?"}
        with open(self.answers, "w") as f:
            json.dump({"d-one": ans}, f)
        self.run_cycle()
        self.assertEqual(self.sent_to(), [K1])
        text = self.sent_text()
        self.assertTrue(text.startswith("[Supervisor wake: answered]"))
        self.assertIn("choice: Ship it", text)
        self.assertIn('note: use the "blue" plan,\nnot the red one', text)
        self.assertIn("answered by Uri", text)
        self.assertTrue(text.endswith("End with owner.py status set"))
        self.assertEqual(self.reg()["last_wake"]["reason"], "answered")
        self.assertIn("[supervisor] woke owner: answered", self.log_md())
        # the same answer is never passed on twice
        self.run_cycle(NOON + 40 * MIN)
        self.assertEqual(len(self.runs.sends()), 1)

    def test_answer_from_navigator_list_and_before_status_does_not_count(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d-old", "d-new"]), idle_min=10)
        self.nav.answered = [("d-old", {"choice": "A", "note": "", "by": "you", "ts": NOON - 3 * 60 * MIN}),
                             ("d-new", {"choice": "B", "note": "", "by": "copilot", "ts": NOON - MIN * 2})]
        self.run_cycle()
        text = self.sent_text()
        self.assertIn("d-new (answered by copilot)", text)
        self.assertNotIn("d-old", text)

    def test_no_status_this_turn(self):
        self.add_owner(P1, K1, status(), idle_min=10, problem="no status this turn")
        self.run_cycle()
        self.assertTrue(self.sent_text().startswith("[Supervisor wake: no-status]"))

    def test_no_status_file_at_all(self):
        self.add_owner(P1, K1, None, idle_min=10)
        self.run_cycle()
        self.assertTrue(self.sent_text().startswith("[Supervisor wake: no-status]"))
        self.assertIn("no-status", self.reg()["flags"])

    def test_blocked_due_today_or_past(self):
        b = {"what": "Play upload", "tried": ["a", "b"], "unblocker": "uri", "by": TODAY}
        self.add_owner(P1, K1, status(state="blocked", blocked=b), idle_min=10)
        self.add_owner(P2, K2, status(state="blocked", blocked=dict(b, by="2026-11-04")), idle_min=60)
        self.run_cycle()
        self.assertEqual(self.sent_to(), [K1])
        self.assertTrue(self.sent_text().startswith("[Supervisor wake: blocked-due]"))
        self.assertIn("Play upload", self.sent_text())
        self.assertIn("blocked-past-due", self.reg()["flags"])
        self.assertNotIn("blocked-past-due", self.reg("beta")["flags"])

    def test_keep_moving_after_30_idle_minutes(self):
        self.add_owner(P1, K1, status(state="working"), idle_min=31)
        self.add_owner(P2, K2, status(state="picking-next"), idle_min=20)
        self.run_cycle()
        self.assertEqual(self.sent_to(), [K1])
        self.assertTrue(self.sent_text().startswith("[Supervisor wake: keep-moving]"))
        self.assertIn('"merge #12"', self.sent_text())
        self.add_owner(P2, K2, status(state="picking-next"), idle_min=45)
        self.run_cycle(NOON + 5 * MIN)
        self.assertIn(K2, self.sent_to())

    def test_first_match_wins(self):
        self.add_owner(P1, K1, status(needs_uri=["d1"]), idle_min=90, problem="no status this turn")
        with open(self.answers, "w") as f:
            json.dump({"d1": {"choice": "yes", "note": "", "by": "you", "ts": NOON - MIN}}, f)
        self.run_cycle()
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertTrue(self.sent_text().startswith("[Supervisor wake: answered]"))


class NeverWake(Base):
    def test_working_now(self):
        self.add_owner(P1, K1, status(), idle_min=1, problem="no status this turn")
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("alpha: working now", self.cycles()[-1])

    def test_session_busy_needs_a_child_process(self):
        sid = "7cf2e511-78ff-4a3b-9f7e-dd87e2947b71"
        idle = "100 1 /x/claude -p --resume %s --input-format stream-json\n" % sid
        self.assertFalse(sup.session_busy(sid, idle))                      # persistent session waiting for input
        shell_only = idle + "200 5 /bin/zsh -c echo %s\n" % sid           # an unrelated shell mentioning the id
        self.assertFalse(sup.session_busy(sid, shell_only))
        self.assertTrue(sup.session_busy(sid, idle + "300 100 npm ci\n"))   # a tool is running under it
        self.assertFalse(sup.session_busy("deadbeef-0000", idle + "300 100 npm ci\n"))

    def test_hung_tool_over_90_minutes_is_not_busy(self):
        self.add_owner(P1, K1, status(state="working"), idle_min=100)
        self.busy.add(K1)
        self.run_cycle()
        self.assertEqual(len(self.runs.sends()), 1)

    def test_running_process_counts_as_working_even_with_an_old_transcript(self):
        self.add_owner(P1, K1, status(state="working"), idle_min=60)
        self.busy.add(K1)
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("working now", self.cycles()[-1])

    def test_waiting_on_uri_without_new_answer(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d1"]), idle_min=100,
                       problem="no status this turn")
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("waiting on Uri", self.cycles()[-1])

    def test_waiting_on_uri_long_idle_keeps_moving_on_the_rest(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d1"]), idle_min=130)
        self.run_cycle()
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertIn("[Supervisor wake: keep-moving]", self.runs.sends()[0][-1])
        self.assertIn("does not stop the rest of your backlog", self.runs.sends()[0][-1])

    def test_woken_in_last_30_minutes(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle()
        self.nav.items[K1] = 60
        self.run_cycle(NOON + 20 * MIN)
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertIn("woken 20 min ago", self.cycles()[-1])
        self.run_cycle(NOON + 31 * MIN)
        self.assertEqual(len(self.runs.sends()), 2)

    def test_24_wakes_a_day_then_next_day_resets(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        st = {"projects": {"alpha": {"wakes": {"day": TODAY, "count": 24},
                                     "last_wake": {"reason": "keep-moving", "when": "x", "ts": NOON - 2 * 60 * MIN}}}}
        os.makedirs(self.sup_dir)
        with open(os.path.join(self.sup_dir, "state.json"), "w") as f:
            json.dump(st, f)
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("24 wakes today", self.cycles()[-1])
        tomorrow = NOON + DAY
        self.run_cycle(tomorrow)
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertEqual(self.reg()["wakes_today"], 1)

    def test_paused_globally(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sup.main(["pause", "--json"]), 0)
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertTrue(self.cycles()[-1].endswith("skip: paused"))
        self.assertFalse(os.path.exists(os.path.join(self.sup_dir, "register.json")))
        with contextlib.redirect_stdout(io.StringIO()):
            sup.main(["resume"])
        self.run_cycle(NOON + MIN)
        self.assertEqual(self.sent_to(), [K1])

    def test_paused_one_project(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.add_owner(P2, K2, status(), idle_min=40)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sup.main(["pause", "alpha"]), 0)
        self.run_cycle()
        self.assertEqual(self.sent_to(), [K2])
        self.assertTrue(self.reg()["paused"])
        self.assertIn("alpha: paused", self.cycles()[-1])
        with contextlib.redirect_stdout(io.StringIO()):
            sup.main(["resume", P1])
        self.assertEqual(sup.load_state()["paused_projects"], [])

    def test_unknown_project_pause_is_an_error(self):
        self.add_owner(P1, K1, status())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sup.main(["pause", "nope", "--json"]), 1)
        self.assertIn("no registered owner", json.loads(out.getvalue())["error"])

    def test_quiet_hours(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle(datetime.datetime(2026, 11, 3, 3, 30).timestamp())
        self.assertEqual(self.runs.sends(), [])
        self.assertTrue(self.cycles()[-1].endswith("skip: quiet hours"))
        self.assertFalse(os.path.exists(os.path.join(self.sup_dir, "digest.md")))
        self.run_cycle(datetime.datetime(2026, 11, 3, 7, 0).timestamp())
        self.assertEqual(len(self.runs.sends()), 1)

    def test_session_missing_from_navigator(self):
        self.add_owner(P1, K1, status(), idle_min=None, problem="no status this turn")
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("owner session not in the Navigator", self.cycles()[-1])

    def test_send_failure_is_not_counted(self):
        self.runs.code = 1
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle()
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertIsNone(self.reg()["last_wake"])
        self.assertIn("send failed", self.cycles()[-1])
        self.assertEqual(self.log_md(), "")


class DryRun(Base):
    def check_dry(self):
        self.assertEqual(self.runs.calls, [])
        self.assertEqual(self.log_md(), "")
        self.assertFalse(os.path.exists(os.path.join(self.sup_dir, "state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.owners_dir, "alpha", "scoreboard.json")))
        self.assertIn("would wake alpha (keep-moving)", self.cycles()[-1])
        self.assertIn("Would wake this cycle: alpha (keep-moving)", self.text("digest.md"))
        self.assertTrue(self.jfile("digest.json")["dry_run"])

    def test_flag(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle(dry=True)
        self.check_dry()

    def test_env(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        with mock.patch.dict(os.environ, {"SUPERVISOR_DRY_RUN": "1"}):
            self.run_cycle()
        self.check_dry()

    def test_dry_manual_wake_sends_nothing(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        with mock.patch.dict(os.environ, {"SUPERVISOR_DRY_RUN": "1"}):
            res = sup.wake_cmd("alpha", "x", now=NOON)
        self.assertTrue(res["ok"] and res["dry_run"])
        self.assertEqual(self.runs.calls, [])


class Register(Base):
    def test_fields(self):
        st = status(updated_at=iso(NOON - 3 * DAY), shipped=["merged #9"], needs_uri=["d1", "d2", "gone"])
        self.add_owner(P1, K1, st, idle_min=10)
        self.nav.open = [{"id": "d1", "question": "q1", "asked_at": NOON - 9 * DAY, "deadline": "2026-11-01"},
                         {"id": "d2", "question": "q2", "asked_at": NOON - 2 * DAY, "deadline": None},
                         {"id": "other", "question": "q3", "asked_at": NOON - 20 * DAY}]
        self.run_cycle()
        r = self.reg()
        self.assertEqual(r["owner_key"], K1)
        self.assertEqual(r["state"], "working")
        self.assertEqual(r["last_shipped"], {"text": "merged #9", "when": st["updated_at"]})
        self.assertEqual(r["days_since_shipped"], 3.0)
        self.assertEqual(r["decisions_open"], 2)
        self.assertEqual(r["oldest_decision_days"], 9.0)
        self.assertIn("decision-7d", r["flags"])
        self.assertIn("decision-overdue", r["flags"])
        for k in ("project", "summary", "updated_at", "stale", "problem", "idle_checkins", "wakes_today", "last_wake"):
            self.assertIn(k, r)
        dg = self.jfile("digest.json")
        self.assertEqual(dg["decisions_overall"], {"open": 3, "oldest_days": 20.0})
        self.assertEqual([d["status"] for d in dg["owners"][0]["decisions"]], ["open", "open", "unknown"])

    def test_last_shipped_kept_until_a_new_shipment(self):
        self.add_owner(P1, K1, status(shipped=["merged #9"]), idle_min=10)
        self.run_cycle()
        later = iso(NOON + 10 * MIN)
        self.add_owner(P1, K1, status(updated_at=later, shipped=[]), idle_min=1)
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual(self.reg()["last_shipped"]["text"], "merged #9")
        self.add_owner(P1, K1, status(updated_at=iso(NOON + 20 * MIN), shipped=["merged #10"]), idle_min=1)
        self.run_cycle(NOON + 30 * MIN)
        self.assertEqual(self.reg()["last_shipped"]["text"], "merged #10")
        self.assertEqual(self.reg()["days_since_shipped"], 0.0)

    def test_idle_checkins_increment_hold_and_reset(self):
        self.add_owner(P1, K1, status(), idle_min=10)
        self.run_cycle()
        self.assertEqual(self.reg()["idle_checkins"], 0)
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual(self.reg()["idle_checkins"], 1)
        self.assertNotIn("idle-2x", self.reg()["flags"])
        self.run_cycle(NOON + 30 * MIN)
        self.assertEqual(self.reg()["idle_checkins"], 2)
        self.assertIn("idle-2x", self.reg()["flags"])
        self.nav.items[K1] = 1                  # mid-turn: held, not counted
        self.run_cycle(NOON + 45 * MIN)
        self.assertEqual(self.reg()["idle_checkins"], 2)
        self.add_owner(P1, K1, status(updated_at=iso(NOON + 50 * MIN)), idle_min=5)
        self.run_cycle(NOON + 60 * MIN)
        self.assertEqual(self.reg()["idle_checkins"], 0)
        self.assertNotIn("idle-2x", self.reg()["flags"])

    def test_waiting_owner_counts_idle_but_is_not_flagged(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d1"]), idle_min=10)
        for i in range(3):
            self.run_cycle(NOON + i * 15 * MIN)
        self.assertEqual(self.reg()["idle_checkins"], 2)
        self.assertNotIn("idle-2x", self.reg()["flags"])

    def test_wakes_today_and_last_wake(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle()
        r = self.reg()
        self.assertEqual(r["wakes_today"], 1)
        self.assertEqual(r["last_wake"]["reason"], "keep-moving")
        self.assertEqual(r["last_wake"]["when"], iso(NOON))

    def test_digest_md_and_cycles_log(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.add_owner(P2, K2, status(), idle_min=5)
        owner.append_log("alpha", "owner", "x" * 400)
        self.run_cycle()
        md = self.text("digest.md")
        self.assertIn("## alpha [working] idle 40 min ago", md)
        self.assertIn("Woke this cycle: alpha (keep-moving).", md)
        self.assertIn("...", md)                    # long log lines are cut in the md
        line = self.cycles()[-1]
        self.assertIn("woke alpha (keep-moving)", line)
        self.assertIn("beta: nothing due", line)


class Robustness(Base):
    def test_failing_owner_does_not_stop_others(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.add_owner(P2, K2, status(), idle_min=40)
        real = sup.observe

        def flaky(pkey, *a, **kw):
            if pkey == P1:
                raise RuntimeError("boom")
            return real(pkey, *a, **kw)
        with mock.patch.object(sup, "observe", flaky):
            self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.sent_to(), [K2])
        self.assertEqual(self.reg()["flags"], ["error"])
        self.assertIn("errors alpha: RuntimeError: boom", self.cycles()[-1])

    def test_navigator_down_logs_and_exits_0(self):
        self.nav.down = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn("error: OSError", self.cycles()[-1])

    def test_atomic_writes_keep_the_old_file(self):
        self.add_owner(P1, K1, status(), idle_min=10)
        self.run_cycle()
        before = self.text("register.json")
        with mock.patch.object(owner.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.run_cycle(NOON + 15 * MIN), 0)
        self.assertEqual(self.text("register.json"), before)
        self.assertEqual([n for n in os.listdir(self.sup_dir) if ".tmp" in n], [])
        self.assertIn("disk full", self.cycles()[-1])

    def test_scoreboard_refresh_writes_only_when_not_dry(self):
        with open(os.environ["WATCHTOWER_GITHUB_JSON"], "w") as f:
            json.dump({"fetched": 1.0, "lanes": {"a": [{"repo": "drrozen-med/vera", "default_ci": "red",
                                                        "open_prs": [], "flags": []}]}}, f)
        self.add_owner("/Users/x/projects/vera", K1, status(), idle_min=10)
        self.run_cycle(dry=True)
        sb = os.path.join(self.owners_dir, "vera", "scoreboard.json")
        self.assertFalse(os.path.exists(sb))
        self.assertIn("CI red on main", self.jfile("digest.json")["owners"][0]["scoreboard_problems"])
        self.run_cycle(NOON + 15 * MIN)
        self.assertTrue(os.path.exists(sb))

    def test_pause_during_a_cycle_is_kept(self):
        self.add_owner(P1, K1, status(), idle_min=10)
        real = sup.refresh_scoreboards

        def pause_meanwhile(dry):
            sup.set_pause("alpha", True)
            return real(dry)
        with mock.patch.object(sup, "refresh_scoreboards", pause_meanwhile):
            self.run_cycle()
        self.assertEqual(sup.load_state()["paused_projects"], ["alpha"])
        self.assertIn("alpha", sup.load_state()["projects"])


def dec(did, **kw):
    d = {"id": did, "question": "Pick the pricing for %s" % did, "options": ["A", "B"], "priority": 2,
         "deadline": None, "asked_at": NOON - DAY, "lane": "cdl"}
    d.update(kw)
    return d


class LoudAlerts(Base):
    """docs/supervisor.md, "Loud alerts": who raises, which decisions, dedupe, quiet hours, pause."""

    def raised(self):
        return [a[a.index("--decision") + 1] for a in self.runs.raises()]

    def blocked_owner(self, by=TODAY, unblocker="uri", needs=("d1",)):
        return status(state="blocked", needs_uri=list(needs), summary="blocked on Uri",
                      blocked={"what": "pricing", "tried": ["a", "b"], "unblocker": unblocker, "by": by})

    def test_priority_1_raises_with_key_link_and_actor(self):
        self.nav.open = [dec("d1", priority=1), dec("d2", priority=2)]
        self.run_cycle()
        self.assertEqual(self.raised(), ["d1"])
        argv, kw = [(a, k) for a, k in self.runs.calls if a[2:4] == ["alert", "raise"]][0]
        self.assertEqual(argv[:4], [sys.executable, sup.NAV, "alert", "raise"])
        self.assertEqual(argv[argv.index("--key") + 1], "decision:d1")
        self.assertIn("Decision for you: Pick the pricing for d1", argv[argv.index("--title") + 1])
        body = argv[argv.index("--body") + 1]
        self.assertIn("Why now: P1.", body)
        self.assertIn("Options: A / B.", body)
        self.assertEqual(kw["env"]["NAV_ACTOR"], "supervisor")
        dg = self.jfile("digest.json")
        self.assertEqual(dg["alerts"], [{"decision": "d1", "reasons": ["P1"], "raised": True}])
        self.assertIn("alerts d1 (P1)", self.cycles()[-1])
        self.assertIn("Loud alerts: d1 (P1) raised", self.text("digest.md"))

    def test_deadline_today_or_past_not_future(self):
        self.nav.open = [dec("past", deadline="2026-11-01"), dec("today", deadline=TODAY),
                         dec("future", deadline="2026-11-04"), dec("none")]
        self.run_cycle()
        self.assertEqual(self.raised(), ["past"])                    # one new alert per cycle
        reasons = {a["decision"]: a["reasons"] for a in self.jfile("digest.json")["alerts"]}
        self.assertEqual(reasons, {"past": ["overdue since 2026-11-01"], "today": ["due today"]})

    def test_owner_blocked_on_uri_due(self):
        for by in (TODAY, "2026-11-01"):
            with self.subTest(by=by):
                self.runs.calls.clear()
                self.nav.open = [dec("d1")]
                self.add_owner(P1, K1, self.blocked_owner(by=by), idle_min=5)
                self.run_cycle()
                self.assertEqual(self.raised(), ["d1"])
                self.assertEqual(self.jfile("digest.json")["alerts"][0]["reasons"], ["blocks alpha"])

    def test_owner_block_not_due_or_not_on_uri_or_not_blocked_does_not_raise(self):
        for st in (self.blocked_owner(by="2026-11-04"),                         # date in the future
                   self.blocked_owner(unblocker="supervisor"),                  # someone else unblocks
                   status(state="waiting-on-uri", needs_uri=["d1"]),           # waiting, not blocked
                   self.blocked_owner(needs=("other",))):                       # blocked on another decision
            with self.subTest(st=st):
                self.runs.calls.clear()
                self.nav.owners.clear()
                self.nav.open = [dec("d1")]
                self.add_owner(P1, K1, st, idle_min=5)
                self.run_cycle()
                self.assertEqual(self.raised(), [])

    def test_one_alert_per_decision_even_with_every_reason(self):
        self.nav.open = [dec("d1", priority=1, deadline="2026-11-01")]
        self.add_owner(P1, K1, self.blocked_owner(), idle_min=5)
        self.add_owner(P2, K2, self.blocked_owner(), idle_min=5)
        self.run_cycle()
        self.assertEqual(self.raised(), ["d1"])
        self.assertEqual(self.jfile("digest.json")["alerts"][0]["reasons"],
                         ["P1", "overdue since 2026-11-01", "blocks alpha", "blocks beta"])

    def test_dedupe_open_alert_and_12h_after_ack(self):
        self.nav.open = [dec("d1", priority=1), dec("d2", priority=1), dec("d3", priority=1)]
        self.nav.alerts = {"open": [{"id": "al-1", "key": "decision:d1"}],
                           "acked": [{"id": "al-2", "key": "decision:d2", "acked_at": NOON - 11 * 3600},
                                     {"id": "al-3", "key": "decision:d3", "acked_at": NOON - 13 * 3600}]}
        self.run_cycle()
        self.assertEqual(self.raised(), ["d3"])
        held = {a["decision"]: a.get("skip") for a in self.jfile("digest.json")["alerts"]}
        self.assertEqual(held, {"d1": "alert already open", "d2": "acked 11.0 h ago", "d3": None})

    def test_p1_gets_loud_only_after_a_day_and_one_new_alert_per_cycle(self):
        self.nav.open = [dec("d1", priority=1, asked_at=NOON - 3600), dec("d2", priority=1), dec("d3", priority=1)]
        self.run_cycle()
        self.assertEqual(self.raised(), ["d2"])
        held = {a["decision"]: a.get("skip") for a in self.jfile("digest.json")["alerts"]}
        self.assertNotIn("d1", held)                                  # asked an hour ago: not loud yet
        self.assertEqual(held["d3"], "one new alert per cycle")

    def test_navigator_dedupe_reply_is_recorded_as_held(self):
        self.nav.open = [dec("d1", priority=1)]
        self.runs.stdout = '{"ok": true, "raised": false, "reason": "already open"}'
        self.run_cycle()
        self.assertEqual(self.jfile("digest.json")["alerts"][0],
                         {"decision": "d1", "reasons": ["P1"], "raised": False, "skip": "navigator: already open"})

    def test_quiet_hours_and_global_pause_raise_nothing(self):
        self.nav.open = [dec("d1", priority=1, asked_at=NOON - 2 * DAY)]
        self.run_cycle(now=datetime.datetime(2026, 11, 3, 6, 59).timestamp())
        self.assertEqual(self.raised(), [])
        sup.set_pause(None, True)
        self.run_cycle()
        self.assertEqual(self.raised(), [])
        sup.set_pause(None, False)
        self.run_cycle(now=datetime.datetime(2026, 11, 3, 7, 0).timestamp())
        self.assertEqual(self.raised(), ["d1"])

    def test_paused_project_holds_its_decisions(self):
        self.nav.open = [dec("d1", priority=1), dec("d2", priority=1)]
        self.add_owner(P1, K1, self.blocked_owner(needs=("d1",)), idle_min=5)
        sup.set_pause("alpha", True)
        self.run_cycle()
        self.assertEqual(self.raised(), ["d2"])

    def test_dry_run_decides_but_raises_nothing(self):
        self.nav.open = [dec("d1", priority=1)]
        self.run_cycle(dry=True)
        self.assertEqual(self.raised(), [])
        self.assertEqual(self.jfile("digest.json")["alerts"], [{"decision": "d1", "reasons": ["P1"], "raised": True}])
        self.assertIn("would alert d1 (P1)", self.cycles()[-1])

    def test_alert_failure_does_not_stop_the_cycle(self):
        self.nav.open = [dec("d1", priority=1)]
        self.add_owner(P1, K1, status(), idle_min=40)
        self.runs.code = 1
        self.run_cycle()
        self.assertTrue(self.jfile("digest.json")["alerts"][0]["skip"].startswith("raise failed (exit 1)"))
        self.assertEqual(self.reg()["state"], "working")

    def test_navigator_without_alerts_still_raises(self):
        self.nav.open = [dec("d1", priority=1)]
        real = self.nav.__call__

        def no_alerts(path):
            if path == "/api/alerts":
                raise OSError("HTTP 404")
            return real(path)
        with mock.patch.object(sup, "get", no_alerts):
            self.run_cycle()
        self.assertEqual(self.raised(), ["d1"])

    def test_owner_wake_texts_never_ask_for_alerts(self):
        """Only the cycle raises: no wake tells an owner to raise one."""
        self.add_owner(P1, K1, self.blocked_owner(by="2026-11-01"), idle_min=40)
        self.run_cycle()
        self.assertEqual(self.raised(), [])                 # d1 is not an open decision in the inbox
        self.assertNotIn("alert", self.sent_text().lower())


class Gate(Base):
    """docs/supervisor.md, "The gate": Uri is counted and alerted only for what is his; the desk is the supervisor's."""

    def test_only_uris_questions_are_counted_and_alerted(self):
        self.nav.open = [
            dec("his", priority=1, asked_at=NOON - 3 * DAY, for_uri=True, holder="uri"),
            dec("auto", priority=1, asked_at=NOON - 9 * DAY, for_uri=False, holder="auto", kind="approval"),
            dec("lead", priority=1, asked_at=NOON - 5 * DAY, for_uri=False, holder="supervisor", kind="problem",
                topic="test-server", lane="alpha", question="No test server",
                dupes=[{"id": "twin", "lane": "beta"}]),
            dec("twin", priority=1, asked_at=NOON - 5 * DAY, for_uri=False, holder="dupe", kind="problem",
                topic="test-server", lane="beta", dupe_of="lead")]
        self.run_cycle()
        self.assertEqual(self.raised(), ["his"])
        dg = self.jfile("digest.json")
        self.assertEqual([a["decision"] for a in dg["alerts"]], ["his"])
        self.assertEqual(dg["decisions_overall"], {"open": 1, "oldest_days": 3.0})
        self.assertEqual(dg["desk"], [{"id": "lead", "topic": "test-server", "question": "No test server",
                                       "raised_by": ["alpha", "beta"], "days": 5.0}])
        md = self.text("digest.md")
        self.assertIn("Uri's open decisions: 1, oldest 3.0d.", md)
        self.assertIn("On YOUR desk, not Uri's (1)", md)
        self.assertIn("- lead [test-server] raised by alpha, beta, 5.0d on your desk: No test server", md)
        self.assertIn('decide <id> other --note "solved:', md)

    def raised(self):
        return [a[a.index("--decision") + 1] for a in self.runs.raises()]

    def test_an_older_navigator_without_the_gate_counts_everything(self):
        self.nav.open = [dec("d1", priority=1), dec("d2")]
        self.run_cycle()
        dg = self.jfile("digest.json")
        self.assertEqual((dg["decisions_overall"]["open"], dg["desk"]), (2, []))
        self.assertEqual(self.raised(), ["d1"])
        self.assertNotIn("On YOUR desk", self.text("digest.md"))

    def test_an_owner_blocked_on_a_desk_problem_still_resolves_the_question(self):
        """needs_uri lookups use every open question, so an owner waiting on a desk problem is not 'unknown id'."""
        self.nav.open = [dec("lead", for_uri=False, holder="supervisor", kind="problem", topic="t")]
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["lead"]), idle_min=45)
        self.run_cycle()
        self.assertEqual(self.raised(), [])
        self.assertEqual(self.jfile("digest.json")["errors"], [])


class Manual(Base):
    def test_manual_wake_sends_note_and_obeys_caps(self):
        self.add_owner(P1, K1, status(), idle_min=1)       # working now does not block a manual wake
        res = sup.wake_cmd("alpha", "Rebase #12 first", now=NOON)
        self.assertTrue(res["ok"])
        text = self.sent_text()
        self.assertTrue(text.startswith("[Supervisor wake: manual] Rebase #12 first"))
        self.assertTrue(text.endswith("End with owner.py status set"))
        self.assertIn("[supervisor] woke owner: manual", self.log_md())
        res = sup.wake_cmd("alpha", now=NOON + 10 * MIN)
        self.assertEqual((res["ok"], res["skip"]), (False, "woken 10 min ago"))
        self.assertEqual(len(self.runs.sends()), 1)
        self.assertIn("manual wake alpha by cli: woken 10 min ago", self.cycles()[-1])

    def test_manual_wake_paused(self):
        self.add_owner(P1, K1, status())
        sup.set_pause("alpha", True)
        self.assertEqual(sup.wake_cmd(P1, now=NOON)["skip"], "paused")
        sup.set_pause("alpha", False)
        sup.set_pause(None, True)
        self.assertEqual(sup.wake_cmd(P1, now=NOON)["skip"], "paused")
        self.assertEqual(self.runs.sends(), [])

    def test_cli_wake_and_status(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sup.time, "time", return_value=NOON):
            self.assertEqual(sup.main(["wake", "alpha", "--reason", "go", "--json"]), 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.run_cycle(NOON + 40 * MIN)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup.main(["status", "--json"])
        v = json.loads(out.getvalue())
        self.assertEqual(set(v), {"register", "digest_at", "paused", "paused_projects", "last_cycle", "cycles_tail",
                                  "night", "engine_default", "jobs"})
        self.assertEqual((v["night"], v["engine_default"], v["jobs"]), (False, "session", None))
        self.assertEqual(v["register"][0]["project"], "alpha")
        self.assertTrue(any("manual wake alpha" in ln for ln in v["cycles_tail"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup.main(["status"])
        self.assertIn("alpha", out.getvalue())
        self.assertIn("running", out.getvalue())

    def test_send_uses_nav_py_as_supervisor(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.run_cycle()
        argv, kw = self.runs.calls[0]
        self.assertEqual(argv[:4], [sys.executable, sup.NAV, "send", K1])
        self.assertTrue(sup.NAV.endswith(os.path.join("ccc-navigator", "nav.py")))
        self.assertEqual(kw["env"]["NAV_ACTOR"], "supervisor")


P3 = "/Users/x/projects/gamma"
NIGHT = datetime.datetime(2026, 11, 3, 3, 0, 0).timestamp()      # 03:00 local: quiet hours
ICEMAN = ["--provider", "opencode", "bg", "edit", "-m", "opencode-go/iceman-contributor-free"]
MINIMAX = ["--provider", "droid", "bg", "yolo", "-m", "custom:MiniMax-M3-direct"]


class Jobs(Base):
    """Engine jobs (docs/supervisor.md, "Engines"): an owner's turn is one free-model job, not a message."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SUPERVISOR_ENGINE": "jobs"})
        env.start()
        self.addCleanup(env.stop)

    def folder(self, slug="alpha"):
        return os.path.join(self.owners_dir, slug)

    def write_status(self, at, slug="alpha", **kw):
        with open(os.path.join(self.folder(slug), "status.json"), "w") as f:
            json.dump(status(updated_at=iso(at), **kw), f)

    def seed(self, **st):
        os.makedirs(self.sup_dir, exist_ok=True)
        with open(os.path.join(self.sup_dir, "state.json"), "w") as f:
            json.dump(st, f)

    def ps(self, slug="alpha"):
        return self.jfile("state.json")["projects"][slug]

    def skip_of(self, slug="alpha"):
        return {x["project"]: x["skip"] for x in self.jfile("digest.json")["skips"]}.get(slug)

    def cli(self, *argv, now=NOON):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sup.time, "time", return_value=now):
            code = sup.main(list(argv) + ["--json"])
        return code, json.loads(out.getvalue())

    def test_a_wake_starts_one_job_in_the_owner_folder_with_the_whole_turn(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])                         # no message into any session
        (args, cwd), = [(a, c) for a, c in self.osrc.calls if a[0] == "--provider"]
        self.assertEqual((args[:6], cwd), (ICEMAN, self.folder()))
        text = args[6]
        for part in ("You are the owner of the project alpha", "HOW THIS TURN RUNS", self.folder() + "/wt/<name>",
                     "git -C %s worktree add" % P1, "[Supervisor wake: keep-moving]", "status set alpha --session " + K1):
            self.assertIn(part, text)
        self.assertNotIn("NIGHT RULES", text)
        self.assertTrue(text.endswith("End with owner.py status set"))
        self.assertEqual(self.ps()["job"], {"id": "job-1", "started": NOON, "provider": "iceman"})
        self.assertEqual(self.ps()["last_wake"]["reason"], "keep-moving")
        r = self.reg()
        self.assertEqual((r["engine"], r["job"]["id"], r["job"]["provider"], r["job"]["running"], r["working_now"]),
                         ("jobs", "job-1", "iceman", True, True))
        self.assertIn("[supervisor] woke owner: keep-moving (job job-1 on iceman)", self.log_md())
        self.assertIn("woke alpha (keep-moving)", self.cycles()[-1])
        md = self.text("digest.md")
        self.assertIn("Owner turns run as free-model jobs: 1 running, at most 4 at once", md)
        self.assertIn("turn running (job job-1 on iceman, 0 min)", md)
        self.assertIn("supervisor feedback <project>", md)
        self.assertEqual(self.jfile("register.json")["jobs"]["running"], 1)

    def test_a_running_job_is_never_doubled(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.run_cycle(NOON + 40 * MIN)
        self.assertEqual(len(self.osrc.launches()), 1)
        self.assertEqual(self.skip_of(), "its turn is running (job job-1 on iceman, 40 min)")
        self.assertEqual(self.osrc.cancels(), [])

    def test_a_turn_that_wrote_its_status_counts_for_the_provider_and_the_owner_stays_on_it(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.write_status(NOON + 10 * MIN)
        self.osrc.jobs["job-1"]["status"] = "done"
        self.run_cycle(NOON + 15 * MIN)
        lj = self.ps()["last_job"]
        self.assertEqual((lj["id"], lj["provider"], lj["ended"], lj["delivered"]), ("job-1", "iceman", "done", True))
        self.assertNotIn("job", self.ps())
        self.assertEqual(self.jfile("state.json")["providers"]["iceman"], {"ok": 1, "streak": 0, "last_ok": NOON + 15 * MIN})
        self.assertEqual((len(self.osrc.launches()), self.skip_of(), self.reg()["flags"]), (1, "nothing due", []))
        self.run_cycle(NOON + 50 * MIN)                                 # idle again: the next turn, same provider
        self.assertEqual([a[:6] for a in self.osrc.launches()], [ICEMAN, ICEMAN])

    def test_a_turn_without_a_status_moves_the_owner_to_the_next_provider_within_minutes(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.osrc.jobs["job-1"]["status"] = "wedged"
        self.run_cycle(NOON + 15 * MIN)                                 # 15 min after the wake, not the usual 30
        self.assertEqual(self.osrc.cancels(), ["job-1"])                # nothing is left running in the owner's folder
        self.assertEqual([a[:6] for a in self.osrc.launches()], [ICEMAN, MINIMAX])
        self.assertIn('[Supervisor wake: no-status] Your last turn (on iceman) ended "wedged" after 15 min without a status',
                      self.osrc.launches()[1][6])
        h = self.jfile("state.json")["providers"]["iceman"]
        self.assertEqual((h["bad"], h["streak"]), (1, 1))
        self.assertEqual((self.ps()["provider_idx"], self.ps()["turn_fails"], self.ps()["job"]["provider"]), (1, 1, "minimax"))

    def test_a_launch_that_fails_counts_against_the_provider_and_the_next_one_gets_the_try(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.osrc.fail = {"opencode"}
        self.run_cycle()
        self.assertEqual(self.skip_of(), "could not start a job on iceman: [outsourcerer] opencode: quota exceeded")
        self.assertNotIn("job", self.ps())
        self.assertNotIn("last_wake", self.ps())
        self.assertEqual(self.jfile("state.json")["providers"]["iceman"]["bad"], 1)
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual(self.osrc.launches()[-1][:6], MINIMAX)
        self.assertEqual(self.ps()["job"]["provider"], "minimax")

    def test_a_launch_that_runs_out_of_time_is_not_the_providers_failure(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.osrc.slow = True
        self.run_cycle()
        self.assertEqual(self.skip_of(), "could not start a job: timed out after 120 s")
        self.assertEqual((self.jfile("state.json").get("providers"), self.ps().get("provider_idx"), self.ps().get("last_job")),
                         ({}, None, None))
        self.osrc.slow = False
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual(self.osrc.launches()[-1][:6], ICEMAN)

    def test_only_so_many_jobs_at_once_and_the_owner_that_waited_longest_goes_first(self):
        for pk, k in ((P1, K1), (P2, K2), (P3, "claude:cccc-3")):
            self.add_owner(pk, k, status(), idle_min=None)
        self.seed(projects={"alpha": {"last_wake": {"reason": "keep-moving", "ts": NOON - 40 * MIN}},
                            "beta": {"last_wake": {"reason": "keep-moving", "ts": NOON - 200 * MIN}}})
        with mock.patch.dict(os.environ, {"SUPERVISOR_JOBS_MAX": "2"}):
            self.run_cycle()
        self.assertEqual([c for _, c in self.osrc.calls if _[0] == "--provider"], [self.folder("gamma"), self.folder("beta")])
        self.assertEqual(self.skip_of("alpha"), "no free place: 2 jobs running")
        self.assertEqual([w["project"] for w in self.jfile("digest.json")["wakes"]], ["beta", "gamma"])     # listed in project order
        j = self.jfile("register.json")["jobs"]
        self.assertEqual((j["running"], j["max"], j["held"]), (2, 2, ""))

    def test_no_new_job_while_the_mac_is_overloaded(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.mac_busy = "load 45, limit 30"
        self.run_cycle()
        self.assertEqual((self.osrc.launches(), self.skip_of()), ([], "the Mac is busy: load 45, limit 30"))
        self.assertEqual(self.jfile("register.json")["jobs"]["held"], "load 45, limit 30")
        self.assertIn("new turns are held, load 45, limit 30", self.text("digest.md"))

    def test_a_job_that_hangs_is_cancelled_and_the_next_provider_takes_over(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.run_cycle(NOON + 95 * MIN)
        self.assertEqual(self.osrc.cancels(), ["job-1"])
        self.assertEqual(self.jfile("state.json")["providers"]["iceman"]["bad"], 1)
        self.assertEqual([a[:6] for a in self.osrc.launches()], [ICEMAN, MINIMAX])
        self.assertEqual(self.ps()["job"]["id"], "job-2")

    def test_a_job_still_running_after_its_status_is_ended_as_delivered(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.write_status(NOON + 5 * MIN)
        self.run_cycle(NOON + 12 * MIN)                                 # 7 min after the status: let it finish
        self.assertEqual((self.osrc.cancels(), self.reg()["job"]["running"], self.reg()["working_now"]), ([], True, True))
        self.run_cycle(NOON + 20 * MIN)                                 # 15 min after it: the turn is over
        self.assertEqual(self.osrc.cancels(), ["job-1"])
        self.assertEqual((self.ps()["last_job"]["ended"], self.ps()["last_job"]["delivered"]), ("done", True))
        self.assertEqual(self.jfile("state.json")["providers"]["iceman"]["ok"], 1)

    def test_a_stalled_job_gets_twenty_minutes(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.osrc.jobs["job-1"]["status"] = "stalled?"
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual((self.osrc.cancels(), len(self.osrc.launches())), ([], 1))
        self.run_cycle(NOON + 25 * MIN)
        self.assertEqual(self.osrc.cancels(), ["job-1"])
        self.assertEqual(self.ps()["last_job"]["ended"], "stalled")

    def test_a_job_whose_state_cannot_be_read_counts_as_running_until_it_is_too_old(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.osrc.blind = True
        self.run_cycle(NOON + 40 * MIN)
        self.assertEqual((len(self.osrc.launches()), self.skip_of()), (1, "its turn is running (job job-1 on iceman, 40 min)"))
        self.assertEqual(self.reg()["job"]["state"], "unknown")
        self.run_cycle(NOON + 95 * MIN)
        self.assertEqual(self.osrc.cancels(), ["job-1"])
        self.assertEqual(self.ps()["last_job"]["ended"], "lost")

    def test_a_live_job_in_the_owner_folder_that_nobody_recorded_is_taken_over(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.osrc.jobs["x-9"] = {"status": "running", "cwd": self.folder(), "started": NOON - 10 * MIN,
                                 "model": "custom:MiniMax-M3-direct"}
        self.osrc.jobs["x-8"] = {"status": "running", "cwd": "/somewhere/else", "started": NOON - 10 * MIN, "model": "m"}
        self.run_cycle()
        self.assertEqual(self.osrc.launches(), [])
        self.assertEqual(self.ps()["job"], {"id": "x-9", "started": NOON - 10 * MIN, "provider": "minimax", "adopted": True})
        self.assertEqual(self.skip_of(), "its turn is running (job x-9 on minimax, 10 min)")
        self.osrc.jobs["x-9"]["status"] = "failed"                      # it was not the supervisor's turn: nobody is scored
        self.run_cycle(NOON + 15 * MIN)
        self.assertEqual((self.ps()["last_job"]["ended"], self.jfile("state.json")["providers"], self.ps().get("provider_idx")),
                         ("failed", {}, None))

    def test_a_dry_run_starts_and_cancels_nothing(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.add_owner(P2, K2, status(), idle_min=None)
        self.seed(projects={"beta": {"job": {"id": "old-1", "started": NOON - 200 * MIN, "provider": "iceman"}}})
        self.osrc.jobs["old-1"] = {"status": "running", "cwd": self.folder("beta"), "started": NOON - 200 * MIN, "model": "m"}
        before = self.text("state.json")
        self.run_cycle(dry=True)
        self.assertEqual((self.osrc.launches(), self.osrc.cancels(), self.text("state.json")), ([], [], before))
        self.assertIn("would wake alpha (keep-moving)", self.cycles()[-1])
        self.assertEqual(self.log_md(), "")

    def test_quiet_hours_keep_the_supervisor_on_duty_and_night_mode_keeps_it_working(self):
        self.add_owner(P1, K1, status(updated_at=iso(NIGHT - 60 * MIN)), idle_min=None)
        self.nav.open = [{"id": "d1", "question": "q", "priority": 1, "asked_at": NIGHT - 3 * DAY, "deadline": "2026-11-01"}]
        self.run_cycle(NIGHT)
        self.assertIn("skip: quiet hours", self.cycles()[-1])
        self.assertEqual(self.jfile("state.json"), {"heartbeat": iso(NIGHT)})    # asleep, not gone
        self.assertEqual(self.cli("night", "on")[1], {"ok": True, "night": True})
        self.run_cycle(NIGHT + 15 * MIN)
        text = self.osrc.launches()[0][6]
        self.assertIn("NIGHT RULES (Uri is asleep until about 07:00)", text)
        self.assertLess(text.index("NIGHT RULES"), text.index("[Supervisor wake"))
        self.assertEqual(self.runs.raises(), [])                        # no loud alert while he sleeps
        j = self.jfile("register.json")["jobs"]
        self.assertEqual((j["max"], j["night"]), (2, True))
        self.assertEqual(self.cli("night", "off")[1]["night"], False)
        self.run_cycle(NIGHT + 60 * MIN)
        self.assertIn("skip: quiet hours", self.cycles()[-1])

    def test_a_paused_supervisor_is_not_on_duty_at_night(self):
        self.seed(paused=True)
        self.run_cycle(NIGHT)
        self.assertNotIn("heartbeat", self.jfile("state.json"))

    def test_feedback_opens_the_owners_next_turn(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        code, res = self.cli("feedback", "alpha", "Stop re-planning.\n  Ship #12 first")
        self.assertEqual((code, res), (0, {"ok": True, "project": "alpha", "engine": "jobs"}))
        with open(os.path.join(self.folder(), "supervisor-feedback.md")) as f:
            self.assertEqual(f.read(), "- 2026-11-03 12:00 [cli] Stop re-planning. Ship #12 first\n")
        self.run_cycle()
        text = self.osrc.launches()[0][6]
        self.assertIn("SUPERVISOR FEEDBACK (act on this FIRST):\n- 2026-11-03 12:00 [cli] Stop re-planning. Ship #12 first", text)
        self.assertLess(text.index("SUPERVISOR FEEDBACK"), text.index("[Supervisor wake"))
        self.assertEqual(self.cli("feedback", "alpha", "  ")[0], 1)
        self.assertEqual(self.cli("feedback", "nobody", "x")[0], 1)

    def test_stop_pauses_and_cancels_the_owner_jobs_and_their_workers(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        self.osrc.jobs["w-1"] = {"status": "running", "cwd": os.path.join(self.folder(), "wt", "fix"), "started": NOON, "model": "m"}
        self.osrc.jobs["w-2"] = {"status": "done", "cwd": os.path.join(self.folder(), "wt", "old"), "started": NOON, "model": "m"}
        self.osrc.jobs["u-1"] = {"status": "running", "cwd": "/somewhere/else", "started": NOON, "model": "m"}
        code, res = self.cli("stop")
        self.assertEqual((code, res["paused"], res["cancelled"], res["failed"]),
                         (0, True, [{"project": "alpha", "job": "job-1"}, {"project": "alpha", "job": "w-1"}], []))
        self.assertEqual(self.osrc.cancels(), ["job-1", "w-1"])
        self.assertTrue(self.jfile("state.json")["paused"])
        self.assertIn("stopped by cli: the running job was cancelled", self.log_md())
        r = self.reg()                                                  # the board shows it at once: no cycle runs while paused
        self.assertEqual((r["job"]["running"], r["job"]["state"], r["working_now"], self.jfile("register.json")["jobs"]["running"]),
                         (False, "canceled", False, 0))
        self.run_cycle(NOON + 15 * MIN)
        self.assertIn("skip: paused", self.cycles()[-1])
        sup.main(["resume"])
        self.run_cycle(NOON + 30 * MIN)
        self.assertEqual((self.ps()["last_job"]["ended"], self.ps()["job"]["provider"]), ("canceled", "iceman"))
        self.assertEqual(self.jfile("state.json")["providers"], {})     # a job cancelled by hand is not the provider's failure

    def test_a_resting_provider_is_skipped_and_all_of_them_down_is_one_loud_alert(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        bad = {"bad": 3, "streak": 3, "last_bad": NOON - 10 * MIN}
        self.seed(providers={"iceman": dict(bad)})
        self.run_cycle()
        self.assertEqual(self.osrc.launches()[0][:6], MINIMAX)          # iceman rests for an hour
        self.assertEqual(self.runs.raises(), [])
        self.assertIn("iceman 0 delivered / 3 failed (resting: 3 failed in a row)", self.text("digest.md"))
        self.seed(providers={k: dict(bad) for k in ("iceman", "minimax", "beam", "devin")},
                  projects={"alpha": {"job": {"id": "job-1", "started": NOON, "provider": "minimax"}}})
        self.run_cycle(NOON + 15 * MIN)
        (argv,) = self.runs.raises()
        self.assertEqual(argv[argv.index("--key") + 1], "supervisor:providers-down")
        self.assertIn("No AI provider is working", argv[argv.index("--title") + 1])
        self.nav.alerts["open"] = [{"key": "supervisor:providers-down"}]
        self.run_cycle(NOON + 30 * MIN)
        self.assertEqual(len(self.runs.raises()), 1)                    # not raised twice

    def test_manual_wake_starts_a_job_and_refuses_a_second_one(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        code, res = self.cli("wake", "alpha", "--reason", "Rebase #12 first")
        self.assertEqual((code, res["ok"], res["output"]), (0, True, "job job-1 on iceman"))
        self.assertIn("[Supervisor wake: manual] Rebase #12 first", self.osrc.launches()[0][6])
        self.assertEqual(self.ps()["job"]["id"], "job-1")
        code, res = self.cli("wake", "alpha", now=NOON + 40 * MIN)
        self.assertEqual((code, res["skip"]), (1, "its turn is running (job job-1 on iceman, 40 min)"))
        self.assertEqual(len(self.osrc.launches()), 1)

    def test_env_files_are_moved_out_of_the_owner_folder_before_a_launch(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        wt = os.path.join(self.folder(), "wt", "fix")
        os.makedirs(os.path.join(wt, "node_modules"))
        for name in (".env", ".env.example", os.path.join("node_modules", ".env")):
            with open(os.path.join(wt, name), "w") as f:
                f.write("SECRET=1\n")
        self.run_cycle()
        self.assertFalse(os.path.exists(os.path.join(wt, ".env")))
        with open(os.path.join(self.tmp.name, "owners-env-stash", "alpha", "wt", "fix", ".env")) as f:
            self.assertEqual(f.read(), "SECRET=1\n")                    # moved, never deleted
        self.assertTrue(os.path.exists(os.path.join(wt, ".env.example")))
        self.assertTrue(os.path.exists(os.path.join(wt, "node_modules", ".env")))
        self.assertEqual(len(self.osrc.launches()), 1)

    def test_status_table_shows_the_turns(self):
        self.add_owner(P1, K1, status(), idle_min=None)
        self.run_cycle()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup.main(["status"])
        self.assertIn("jobs: 1 running of 4; providers: iceman 0/0, minimax 0/0, beam 0/0, devin 0/0", out.getvalue())
        self.assertIn("{job job-1 on iceman, 0 min}", out.getvalue())


class Engines(Base):
    """Which engine an owner runs on: its own file, else the fleet default, else session."""

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sup.main(list(argv) + ["--json"])
        return code, json.loads(out.getvalue())

    def test_one_owner_or_all_and_owners_registered_later(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.add_owner(P2, K2, status(), idle_min=40)
        self.assertEqual((sup.engine_of("alpha"), sup.engine_of("beta")), ("session", "session"))
        self.assertEqual(self.cli("engine", "alpha", "jobs"), (0, {"ok": True, "engine": "jobs", "projects": ["alpha"]}))
        self.assertEqual((sup.engine_of("alpha"), sup.engine_of("beta")), ("jobs", "session"))
        self.run_cycle()                                                # a mixed fleet: one job, one message
        self.assertEqual(([c for a, c in self.osrc.calls if a[0] == "--provider"], self.sent_to()),
                         ([os.path.join(self.owners_dir, "alpha")], [K2]))
        self.assertEqual((self.reg("alpha")["engine"], self.reg("beta")["engine"], self.reg("beta")["job"]), ("jobs", "session", None))
        self.assertEqual(self.cli("engine", "all", "jobs")[1]["projects"], ["alpha", "beta"])
        st = self.jfile("state.json")
        self.assertEqual((st["engine_default"], sup.engine_of("beta", st), sup.engine_of("later", st)), ("jobs", "jobs", "jobs"))
        self.assertEqual(self.cli("engine", "alpha", "claude")[1]["engine"], "session")    # the old name of session
        self.assertEqual(sup.engine_of("alpha", st), "session")                           # its own file wins over the default
        self.assertEqual(self.cli("engine", "nobody", "jobs")[0], 1)

    def test_night_mode_keeps_only_the_jobs_going(self):
        self.add_owner(P1, K1, status(updated_at=iso(NIGHT - 60 * MIN)), idle_min=40)
        self.add_owner(P2, K2, status(updated_at=iso(NIGHT - 60 * MIN)), idle_min=40)
        sup.main(["engine", "alpha", "jobs"])
        sup.main(["night", "on"])
        self.run_cycle(NIGHT)
        self.assertEqual((len(self.osrc.launches()), self.sent_to()), (1, []))
        self.assertEqual({x["project"]: x["skip"] for x in self.jfile("digest.json")["skips"]}, {"beta": "resting until 07:00"})

    def test_the_chain_can_be_narrowed(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_CHAIN": "devin, nope, iceman"}):
            self.assertEqual([p[0] for p in sup.chain()], ["devin", "iceman"])
        with mock.patch.dict(os.environ, {"SUPERVISOR_CHAIN": "nope"}):
            self.assertEqual(len(sup.chain()), 4)


if __name__ == "__main__":
    unittest.main()
