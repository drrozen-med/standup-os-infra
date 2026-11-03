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
        raise AssertionError("unexpected navigator call: " + path)


FakeNav.now = NOON


class FakeRun:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=self.code, stdout='{"sent": true}', stderr="")

    def sends(self):
        return [a for a, _ in self.calls if len(a) > 2 and a[2] == "send"]


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
        for p in (mock.patch.object(sup, "get", self.nav), mock.patch.object(sup.subprocess, "run", self.runs)):
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

    def test_waiting_on_uri_without_new_answer(self):
        self.add_owner(P1, K1, status(state="waiting-on-uri", needs_uri=["d1"]), idle_min=200,
                       problem="no status this turn")
        self.run_cycle()
        self.assertEqual(self.runs.sends(), [])
        self.assertIn("waiting on Uri", self.cycles()[-1])

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


if __name__ == "__main__":
    unittest.main()
