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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.owners_dir, self.sup_dir = os.path.join(d, "owners"), os.path.join(d, "sup")
        self.answers = os.path.join(d, "answers.json")
        self.proj_home = os.path.join(d, "home")
        os.makedirs(os.path.join(self.proj_home, "projects", "alpha"), exist_ok=True)
        os.makedirs(os.path.join(self.proj_home, "projects", "beta"), exist_ok=True)
        env = mock.patch.dict(os.environ, {"OWNER_DIR": self.owners_dir, "SUPERVISOR_DIR": self.sup_dir,
                                           "CCC_NAV_ANSWERS": self.answers,
                                           "WATCHTOWER_GITHUB_JSON": os.path.join(d, "github.json"),
                                           "HOME": self.proj_home})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SUPERVISOR_DRY_RUN", None)
        os.environ.pop("NAV_ACTOR", None)
        self.nav = FakeNav()
        self.runs = FakeRun()
        self.busy = set()       # owner keys whose session a process is running right now
        for p in (mock.patch.object(sup, "get", self.nav), mock.patch.object(sup.subprocess, "run", self.runs),
                  mock.patch.object(sup, "process_running", lambda key: key in self.busy)):
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
        self.assertIn("## alpha [claude/working] idle 40 min ago", md)
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
        self.assertEqual(set(v), {"register", "digest_at", "paused", "paused_projects", "last_cycle", "cycles_tail"})
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



class Engines(Base):
    """ENGINE-SPEC: Outsourcerer engine (opencode/devin) vs claude."""

    def setUp(self):
        super().setUp()
        self.osrc = "/fake/outsourcerer.sh"
        self.job_states = {}    # job_id -> status string
        self.canceled = []
        self._p_find = mock.patch.object(sup, "find_outsourcerer", return_value=self.osrc)
        self._p_find.start(); self.addCleanup(self._p_find.stop)
        self._p_js = mock.patch.object(sup, "job_status", side_effect=lambda jid, *a, **k: self.job_states.get(jid))
        self._p_js.start(); self.addCleanup(self._p_js.stop)
        self._p_cancel = mock.patch.object(sup, "cancel_job", side_effect=self._fake_cancel)
        self._p_cancel.start(); self.addCleanup(self._p_cancel.stop)
        # make briefs cheap/deterministic
        self._p_brief = mock.patch.object(sup, "brief_text", side_effect=lambda sl: "BRIEF:%s" % sl)
        self._p_brief.start(); self.addCleanup(self._p_brief.stop)
        self.runs.stdout = "job-abc123\n[outsourcerer] launched"

    def _fake_cancel(self, jid, *a, **k):
        self.canceled.append(jid)
        self.job_states[jid] = "canceled"
        return 0, "canceled"

    def set_engine(self, slug, eng):
        d = os.path.join(self.owners_dir, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "engine"), "w") as f:
            f.write(eng + "\n")

    def launches(self):
        # engine launches go straight to subprocess.run (not via nav.py send path argv[2]=="send")
        return [a for a, _ in self.runs.calls if self.osrc in a]

    def test_opencode_launch_command_and_job_tracking(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        self.run_cycle()
        self.assertEqual(self.sent_to(), [])  # no nav send
        ls = self.launches()
        self.assertEqual(len(ls), 1)
        argv = ls[0]
        self.assertEqual(argv[0], self.osrc)
        self.assertEqual(argv[1:7], ["--provider", "opencode", "bg", "edit", "-m", "opencode-go/iceman-contributor-free"])
        prompt = argv[7]
        self.assertIn("BRIEF:alpha", prompt)
        self.assertIn("[Supervisor wake: keep-moving]", prompt)
        self.assertIn(sup.ENGINE_CONSTRAINT, prompt)
        # cwd is the project folder when it exists; env has OSRC_REQUIRE_INTERACTIVE=0
        _, kw = [c for c in self.runs.calls if self.osrc in c[0]][0]
        self.assertEqual(kw["env"]["OSRC_REQUIRE_INTERACTIVE"], "0")
        st = sup.load_state()["projects"]["alpha"]
        self.assertEqual(st["job_id"], "job-abc123")
        self.assertEqual(st["engine"], "opencode")
        self.assertIn("started_at", st)
        self.assertEqual(self.reg()["engine"], "opencode")
        self.assertEqual(self.reg()["job"]["id"], "job-abc123")
        self.assertIn("woke owner (opencode, job job-abc123): keep-moving", self.log_md())

    def test_devin_launch_command(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "devin")
        self.run_cycle()
        ls = self.launches()
        self.assertEqual(len(ls), 1)
        argv = ls[0]
        self.assertEqual(argv[1:5], ["--provider", "devin", "bg", "-m"])
        self.assertEqual(argv[5], "swe-2-high")
        self.assertIn("BRIEF:alpha", argv[6])
        self.assertEqual(sup.load_state()["projects"]["alpha"]["engine"], "devin")

    def test_busy_while_job_running_then_free_after(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        self.run_cycle()
        self.assertEqual(len(self.launches()), 1)
        self.job_states["job-abc123"] = "running"
        self.run_cycle(NOON + 40 * MIN)
        self.assertEqual(len(self.launches()), 1)  # no second launch
        self.assertIn("job running", self.cycles()[-1])
        self.assertTrue(self.reg()["working_now"])
        self.job_states["job-abc123"] = "done"
        # status changed? still idle/wakeable by rules; owner may need wake depending on state
        self.run_cycle(NOON + 80 * MIN)
        # job done means not running; it may wake again (keep-moving still due) -> launches again
        self.assertGreaterEqual(len(self.launches()), 2)

    def test_hung_job_cancelled(self):
        self.add_owner(P1, K1, status(), idle_min=10)
        self.set_engine("alpha", "opencode")
        # seed a running job started 100 minutes ago
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"job_id": "job-hung", "started_at": NOON - 100 * MIN,
                                               "engine": "opencode"}}})
        self.job_states["job-hung"] = "running"
        self.run_cycle()
        self.assertEqual(self.canceled, ["job-hung"])
        ps = sup.load_state()["projects"]["alpha"]
        self.assertNotIn("job_id", ps)
        self.assertEqual(ps.get("last_job", {}).get("ended"), "hung-canceled")

    def test_launch_failure_counting_and_alert_on_third(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        self.runs.code = 1
        self.runs.stdout = ""
        self.runs.stderr = "" if False else ""
        # FakeRun always returns stdout/stderr attrs we set? it uses self.stdout
        self.runs.stdout = "boom"
        self.run_cycle()
        self.assertIn("Outsourcerer launch failed (1/3)", self.cycles()[-2] if "cycle" in self.cycles()[-1] else " ".join(self.cycles()))
        self.assertIn("[supervisor] Outsourcerer launch failed (1/3)", self.log_md())
        self.assertEqual(self.runs.raises(), [])
        self.run_cycle(NOON + 40 * MIN)
        self.assertEqual(self.runs.raises(), [])
        self.run_cycle(NOON + 80 * MIN)
        # 3rd consecutive -> loud alert
        raised = self.runs.raises()
        self.assertTrue(any("supervisor-launch-fail:alpha" in " ".join(a) for a in raised),
                        "expected launch-fail alert, got %s" % (raised,))
        self.assertEqual(sup.load_state()["projects"]["alpha"]["launch_failures"], 3)

    def test_launch_success_resets_failure_count(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"launch_failures": 2, "last_wake": {"ts": 0}}}})
        self.runs.stdout = "job-ok1"
        self.run_cycle()
        self.assertEqual(sup.load_state()["projects"]["alpha"]["launch_failures"], 0)

    def test_claude_engine_unchanged_nav_send(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "claude")
        self.run_cycle()
        self.assertEqual(self.sent_to(), [K1])
        self.assertEqual(self.launches(), [])

    def test_supervisor_engine_env_override(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "claude")  # file says claude
        with mock.patch.dict(os.environ, {"SUPERVISOR_ENGINE": "devin"}):
            self.assertEqual(sup.engine_of("alpha"), "devin")
            self.run_cycle()
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("--provider", self.launches()[0])
        self.assertEqual(self.launches()[0][self.launches()[0].index("--provider") + 1], "devin")

    def test_engine_cli_set_and_all(self):
        os.makedirs(os.path.join(self.owners_dir, "alpha"), exist_ok=True)
        os.makedirs(os.path.join(self.owners_dir, "beta"), exist_ok=True)
        with mock.patch.object(sup, "resolve_project", return_value=(P1, {"key": K1})), \
             mock.patch.object(sup, "get", self.nav):
            res = sup.engine_cmd("alpha", "opencode")
        self.assertTrue(res["ok"])
        with open(os.path.join(self.owners_dir, "alpha", "engine")) as f:
            self.assertEqual(f.read().strip(), "opencode")
        res = sup.engine_cmd("all", "devin")
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["count"], 2)
        for sl in ("alpha", "beta"):
            with open(os.path.join(self.owners_dir, sl, "engine")) as f:
                self.assertEqual(f.read().strip(), "devin")

class OutsourcererSafety(Base):
    """Safety fixes: fail-closed status/cancel, cwd, serialization, parsing."""

    def setUp(self):
        super().setUp()
        self.osrc = "/fake/outsourcerer.sh"
        self._p_find = mock.patch.object(sup, "find_outsourcerer", return_value=self.osrc)
        self._p_find.start(); self.addCleanup(self._p_find.stop)
        self._p_brief = mock.patch.object(sup, "brief_text", side_effect=lambda sl: "BRIEF:%s" % sl)
        self._p_brief.start(); self.addCleanup(self._p_brief.stop)
        self.runs.stdout = "job-abc123\n[outsourcerer] launched"

    def set_engine(self, slug, eng):
        d = os.path.join(self.owners_dir, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "engine"), "w") as f:
            f.write(eng + "\n")

    def launches(self):
        return [a for a, _ in self.runs.calls if self.osrc in a]

    def test_parse_actual_status_output_json_and_text_tokens(self):
        # JSON object forms
        self.assertEqual(sup.parse_job_status_output('{"status":"running"}', json_ok=True), "running")
        self.assertEqual(sup.parse_job_status_output('{"state":"done"}', json_ok=True), "done")
        self.assertEqual(sup.parse_job_status_output('{"job":{"status":"permission-blocked"}}', json_ok=True), "permission-blocked")
        self.assertEqual(sup.parse_job_status_output('{"status":"cancelled"}', json_ok=True), "canceled")
        self.assertEqual(sup.parse_job_status_output('{"status":"done?"}', json_ok=True), "done?")
        # scalar/string JSON
        self.assertEqual(sup.parse_job_status_output('"failed"', json_ok=True), "failed")
        # unknown/ambiguous JSON fails closed
        self.assertIsNone(sup.parse_job_status_output('{"id":"x","progress":1}', json_ok=True))
        self.assertIsNone(sup.parse_job_status_output('[1,2]', json_ok=True))
        self.assertIsNone(sup.parse_job_status_output('', json_ok=True))
        # text token parsing (non-JSON mode) - reliable whole-token match
        self.assertEqual(sup.parse_job_status_output('  Status: running  \n'), "running")
        self.assertEqual(sup.parse_job_status_output('job x canceled at 12:00'), "canceled")
        self.assertEqual(sup.parse_job_status_output('result=permission-blocked reason=denied'), "permission-blocked")
        self.assertIsNone(sup.parse_job_status_output('no known state here'))

    def test_job_status_one_query_json_only(self):
        calls = []
        def runner(args):
            calls.append(list(args))
            return 0, '{"status":"queued"}'
        self.assertEqual(sup.job_status("jid1", runner), "queued")
        self.assertEqual(calls, [["status", "--json", "jid1"]])

    def test_job_status_unknown_fails_closed_no_second_query(self):
        calls = []
        def runner(args):
            calls.append(list(args))
            return 0, '{"unexpected":"shape"}'
        self.assertIsNone(sup.job_status("jid2", runner))
        self.assertEqual(len(calls), 1)

    def test_unknown_job_status_blocks_launch_never_launches_second(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"job_id": "job-unk", "started_at": NOON - 10 * MIN,
                                               "engine": "opencode", "last_wake": {"ts": 0}}}})
        with mock.patch.object(sup, "job_status", return_value=None):
            self.run_cycle()
        self.assertEqual(self.launches(), [])
        reg = self.reg()
        self.assertEqual(reg["job"]["id"], "job-unk")
        self.assertTrue(reg["working_now"])
        # ps still tracks the job (fail closed)
        self.assertEqual(sup.load_state()["projects"]["alpha"]["job_id"], "job-unk")

    def test_cancel_failure_fails_closed_keeps_tracking(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"job_id": "job-hung", "started_at": NOON - 100 * MIN,
                                               "engine": "opencode"}}})
        with mock.patch.object(sup, "job_status", return_value="running"), \
             mock.patch.object(sup, "cancel_job", return_value=(False, "cancel denied")):
            self.run_cycle(NOON + 100 * MIN)
        self.assertEqual(self.launches(), [])
        ps = sup.load_state()["projects"]["alpha"]
        self.assertEqual(ps.get("job_id"), "job-hung")  # preserved: fail closed
        reg = self.reg()
        self.assertEqual(reg["job"]["id"], "job-hung")
        self.assertTrue(reg["working_now"])

    def test_project_cwd_fail_closed_no_supervisor_fallback(self):
        bad = {"project": "nope-slug-xyz", "project_key": "/nonexistent/path/xyz"}
        cwd, err = sup.project_cwd(bad)
        self.assertIsNone(cwd)
        self.assertIn("cwd unresolved", err)
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        # make both candidate paths invalid for alpha's resolved cwd
        with mock.patch.object(sup, "project_cwd", return_value=(None, "cwd unresolved for alpha")):
            self.run_cycle()
        self.assertEqual(self.launches(), [])
        self.assertIn("cwd unresolved", " ".join(self.cycles()))
        self.assertNotIn("job_id", sup.load_state().get("projects", {}).get("alpha", {}))

    def test_simultaneous_wake_preserves_job_id_across_race(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        # Simulate a race: durable state gets a different active job_id while cycle is in
        # check-launch-track (e.g. lock bypass / concurrent writer). It must not be lost.
        real_do = sup.do_wake
        injected = {"done": False}
        def racing_do(entry, reason, ps, now, dry, note=""):
            if not injected["done"] and not dry:
                st = sup.load_state()
                st.setdefault("projects", {}).setdefault("alpha", {})["job_id"] = "job-race"
                st["projects"]["alpha"]["started_at"] = now - 5
                st["projects"]["alpha"]["engine"] = "opencode"
                st["projects"]["alpha"]["last_wake"] = {"reason": "manual", "when": "x", "ts": now + 1}
                sup.write_json(sup.path("state.json"), st)
                injected["done"] = True
            return real_do(entry, reason, ps, now, dry, note)
        with mock.patch.object(sup, "do_wake", side_effect=racing_do):
            self.run_cycle()
        ps = sup.load_state()["projects"]["alpha"]
        # raced durable job_id/wake must be preserved
        self.assertEqual(ps.get("job_id"), "job-race")
        self.assertEqual((ps.get("last_wake") or {}).get("ts"), NOON + 1)

    def test_manual_wake_blocked_by_unknown_status(self):
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"job_id": "job-unk", "engine": "opencode"}}})
        with mock.patch.object(sup, "job_status", return_value=None), \
             mock.patch.object(sup.time, "time", return_value=NOON):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sup.main(["wake", "alpha", "--json"])
            res = json.loads(out.getvalue())
        self.assertNotEqual(rc, 0)
        self.assertFalse(res.get("ok"))
        self.assertIn("unknown", res.get("skip", ""))
        self.assertEqual(self.launches(), [])

    def test_terminal_job_clears_tracking_allows_rewake(self):
        # extends existing behavior: confirmed terminal clears durable id
        self.add_owner(P1, K1, status(), idle_min=40)
        self.set_engine("alpha", "opencode")
        os.makedirs(self.sup_dir, exist_ok=True)
        sup.write_json(os.path.join(self.sup_dir, "state.json"),
                       {"projects": {"alpha": {"job_id": "job-done", "started_at": NOON - 10 * MIN,
                                               "engine": "opencode", "last_wake": {"ts": 0}}}})
        with mock.patch.object(sup, "job_status", return_value="canceled"):
            self.run_cycle()
        # terminal cleared, so a launch may proceed
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(sup.load_state()["projects"]["alpha"]["job_id"], "job-abc123")



if __name__ == "__main__":
    unittest.main()
