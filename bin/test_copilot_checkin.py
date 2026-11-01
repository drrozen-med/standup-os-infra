"""Tests for bin/copilot-checkin.py: alert, pause, daily cap, offline and just-woke skips.

unittest only; the Navigator HTTP calls, the network probe and subprocess.run
are all mocked, so nothing here touches a real session or Uri's real state.
Run: python3 bin/test_copilot_checkin.py
"""
import datetime as dtmod
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))

_loader = importlib.machinery.SourceFileLoader("copilot_checkin", os.path.join(HERE, "copilot-checkin.py"))
_spec = importlib.util.spec_from_loader("copilot_checkin", _loader)
cc = importlib.util.module_from_spec(_spec)
_loader.exec_module(cc)

NOON = dtmod.datetime(2026, 10, 31, 12, 0, 0)
TODAY = NOON.strftime("%Y-%m-%d")


class FakeDatetime:
    """Stands in for the datetime module: now() is frozen at .fixed."""

    fixed = NOON

    class datetime:
        @classmethod
        def now(cls, tz=None):
            f = FakeDatetime.fixed
            return f if tz is None else f.replace(tzinfo=tz)

        fromisoformat = staticmethod(dtmod.datetime.fromisoformat)

    timezone = dtmod.timezone


def ts_before(minutes):
    """UTC ISO timestamp `minutes` before the frozen now, transcript style (...Z)."""
    t = FakeDatetime.fixed.replace(tzinfo=dtmod.timezone.utc) - dtmod.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(text, minutes):
    return {"type": "assistant", "ts": ts_before(minutes), "blocks": [{"kind": "text", "text": text}]}


def user_event(minutes):
    return {"type": "user", "ts": ts_before(minutes)}


HEALTHY = [reply("Check-in: nothing new.", 60)]
API_ERROR = [reply("API Error (500 upstream)", 60)]
NO_CREDITS = [reply("Sorry: you are out of usage credits right now.", 60)]
PAUSE_ANSWER = {"choice": "Pause check-ins", "note": "", "by": "you", "ts": 1793000000.0}
CLOSED_BY_US = {"choice": "other", "note": "recovered automatically", "by": "copilot-checkin", "ts": 1793000000.0}


class FakeNav:
    """get() router for the three Navigator endpoints the script reads."""

    def __init__(self, open_ids=(), answered=None, copilot="claude:s1", events=None):
        self.open_ids, self.answered, self.copilot, self.events = list(open_ids), dict(answered or {}), copilot, list(events or [])
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        if path == "/api/decisions":
            return {"ok": True,
                    "open": [{"id": i, "question": "q?", "options": ["I'll look", "Pause check-ins"]} for i in self.open_ids],
                    "answered": [dict(id=i, answer=a) for i, a in self.answered.items()]}
        if path.startswith("/api/tree"):
            return {"copilot": {"key": self.copilot} if self.copilot else {}}
        if path.startswith("/api/conversations/"):
            return {"events": self.events}
        raise AssertionError("unexpected navigator call: " + path)


class FakeRun:
    """subprocess.run stub: records (argv, kwargs); exit codes can be set per subcommand."""

    def __init__(self, codes=None):
        self.calls, self.codes = [], dict(codes or {})

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        code = self.codes.get(argv[2], 0) if len(argv) > 2 else 0
        return SimpleNamespace(returncode=code, stdout="ok", stderr="")

    def argvs(self, sub):
        return [a for a, _ in self.calls if len(a) > 2 and a[2] == sub]


class CheckinCase(unittest.TestCase):
    """Common scaffolding: temp log/state paths, mocked Navigator, network and subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "checkin.log")
        self.state = os.path.join(self.tmp.name, "checkin.json")
        self._log, self._state = cc.LOG, cc.STATE
        cc.LOG, cc.STATE = self.log, self.state
        self.addCleanup(setattr, cc, "LOG", self._log)
        self.addCleanup(setattr, cc, "STATE", self._state)
        self.nav = FakeNav(events=HEALTHY)
        self.runs = FakeRun()
        self.online = True
        self._fixed, FakeDatetime.fixed = FakeDatetime.fixed, NOON
        self.addCleanup(setattr, FakeDatetime, "fixed", self._fixed)
        for p in (mock.patch.object(cc, "get", self.nav),
                  mock.patch.object(cc, "online", lambda: self.online),
                  mock.patch.object(cc.subprocess, "run", self.runs),
                  mock.patch.object(cc, "datetime", FakeDatetime)):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self):
        cc.main()

    def log_lines(self):
        with open(self.log) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def last_line(self):
        lines = self.log_lines()
        return lines[-1].split(" ", 1)[1] if lines else ""

    def state_json(self):
        if not os.path.exists(self.state):
            return {}
        with open(self.state) as f:
            return json.load(f)

    def write_state(self, **kw):
        with open(self.state, "w") as f:
            json.dump(kw, f)

    def prewrite_log(self, ts, msg):
        with open(self.log, "a") as f:
            f.write("%s %s\n" % (ts, msg))


class RegressionTests(CheckinCase):
    """The pre-existing skips and the send keep working, one log line per run."""

    def test_quiet_hours(self):
        FakeDatetime.fixed = dtmod.datetime(2026, 10, 31, 3, 0, 0)
        self.run_main()
        self.assertEqual(self.last_line(), "skip: quiet hours")
        self.assertEqual(self.nav.paths, [])          # quiet hours: no Navigator call at all
        self.assertEqual(self.runs.calls, [])

    def test_no_copilot(self):
        self.nav.copilot = None
        self.run_main()
        self.assertEqual(self.last_line(), "skip: no co-pilot set")
        self.assertEqual(self.runs.argvs("send"), [])

    def test_copilot_active(self):
        self.nav.events = [reply("Check-in: nothing new.", 20), user_event(5)]
        self.run_main()
        self.assertEqual(self.last_line(), "skip: co-pilot active 5 min ago")
        self.assertEqual(self.runs.argvs("send"), [])

    def test_healthy_send(self):
        self.run_main()
        self.assertEqual(self.log_lines(), ["%s sent to claude:s1 (exit 0) ok" % NOON.isoformat(timespec="seconds")])
        self.assertEqual(self.runs.argvs("send"), [[sys.executable, cc.NAV, "send", "claude:s1", cc.PROMPT]])
        self.assertEqual(self.state_json(), {"sent_day": TODAY, "sent_count": 1})


class AlertTests(CheckinCase):
    """G5: one copilot-down decision while check-ins fail, closed on recovery."""

    def test_alert_raised_on_error_reply(self):
        self.nav.events = API_ERROR
        self.run_main()
        self.assertEqual(self.runs.argvs("ask"),
                         [[sys.executable, cc.NAV, "ask", "--id", "copilot-down",
                           "--question", "Co-pilot check-ins are failing: API Error (500 upstream). Fix?",
                           "--option", "I'll look", "--option", "Pause check-ins"]])
        argv, kwargs = self.runs.calls[0]
        self.assertEqual(kwargs["env"]["NAV_ACTOR"], "copilot-checkin")
        self.assertEqual(self.state_json()["last_alert"], NOON.isoformat(timespec="seconds"))
        self.assertEqual(self.log_lines(), ["%s skip: last reply was an error 60 min ago" % NOON.isoformat(timespec="seconds")])

    def test_alert_not_duplicated_within_12h(self):
        self.nav.events = API_ERROR
        self.run_main()
        self.run_main()                                # state now has last_alert from the first run
        self.assertEqual(len(self.runs.argvs("ask")), 1)
        self.assertEqual(len(self.log_lines()), 2)

    def test_alert_not_duplicated_while_open(self):
        self.nav.events, self.nav.open_ids = API_ERROR, ["copilot-down"]
        self.run_main()
        self.run_main()
        self.assertEqual(self.runs.argvs("ask"), [])
        self.assertEqual(self.runs.argvs("decide"), [])

    def test_alert_closed_on_recovery(self):
        self.nav.open_ids = ["copilot-down"]
        self.run_main()                                # last reply is healthy again
        self.assertEqual(self.runs.argvs("decide"),
                         [[sys.executable, cc.NAV, "decide", "copilot-down", "other",
                           "--note", "recovered automatically"]])
        self.assertTrue(self.last_line().startswith("sent to "))
        self.nav.open_ids, self.nav.answered = [], {"copilot-down": CLOSED_BY_US}
        self.run_main()                                # already answered: no second close
        self.assertEqual(len(self.runs.argvs("decide")), 1)

    def test_no_alert_when_healthy(self):
        self.run_main()
        self.assertEqual(self.runs.argvs("ask") + self.runs.argvs("decide"), [])

    def test_alert_reason_carries_credit_error(self):
        self.nav.events = NO_CREDITS
        self.run_main()
        self.assertIn("out of usage credits", self.runs.argvs("ask")[0][6])

    def test_new_outage_reopens_the_old_alert(self):
        self.nav.events, self.nav.answered = API_ERROR, {"copilot-down": CLOSED_BY_US}
        self.write_state(last_alert="2026-10-30T22:00:00")     # 14 h ago: past the 12 h window
        self.run_main()
        self.assertEqual(self.runs.argvs("ask"), [])            # re-ask would hit nav's "exists" error
        self.assertEqual(self.runs.argvs("decide"),
                         [[sys.executable, cc.NAV, "decide", "copilot-down", "--reopen"]])
        self.assertEqual(self.state_json()["last_alert"], NOON.isoformat(timespec="seconds"))

    def test_ask_failure_falls_back_to_reopen(self):
        self.nav.events = API_ERROR                      # id exists but is no longer listed as answered
        self.runs.codes = {"ask": 1}
        self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 1)
        self.assertEqual(self.runs.argvs("decide"),
                         [[sys.executable, cc.NAV, "decide", "copilot-down", "--reopen"]])
        self.assertEqual(self.state_json()["last_alert"], NOON.isoformat(timespec="seconds"))

    def test_failed_alert_retries_next_run(self):
        self.nav.events = API_ERROR
        self.runs.codes = {"ask": 1, "decide": 1}       # both fail: no alert, nothing remembered
        self.run_main()
        self.assertNotIn("last_alert", self.state_json())
        self.runs.codes = {}
        self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 2)
        self.assertIn("last_alert", self.state_json())


class PauseTests(CheckinCase):
    """Uri's "Pause check-ins" answer stops every run until he reopens it (or clears the state)."""

    def test_pause_answer_skips(self):
        self.nav.answered = {"copilot-down": PAUSE_ANSWER}
        self.run_main()
        self.assertEqual(self.last_line(), "skip: paused by Uri")
        self.assertEqual(self.runs.argvs("send"), [])
        self.assertEqual(self.nav.paths, ["/api/decisions"])     # skipped before anything else
        self.assertEqual(self.state_json(), {"paused": True, "pause_ts": PAUSE_ANSWER["ts"]})

    def test_pause_persists_without_answer_row(self):
        self.write_state(paused=True, pause_ts=PAUSE_ANSWER["ts"])
        self.run_main()                                          # answer rotated out of the recent list
        self.assertEqual(self.last_line(), "skip: paused by Uri")
        self.assertEqual(self.runs.argvs("send"), [])

    def test_reopen_resumes(self):
        self.write_state(paused=True, pause_ts=PAUSE_ANSWER["ts"])
        self.nav.open_ids = ["copilot-down"]                     # Uri reopened the decision
        self.run_main()
        self.assertNotEqual(self.last_line(), "skip: paused by Uri")
        self.assertTrue(self.last_line().startswith("sent to "))
        self.assertNotIn("paused", self.state_json())

    def test_cleared_state_stays_cleared(self):
        self.write_state(pause_ts=PAUSE_ANSWER["ts"])            # "paused" removed by hand
        self.nav.answered = {"copilot-down": PAUSE_ANSWER}
        self.run_main()
        self.assertTrue(self.last_line().startswith("sent to "))
        self.assertNotIn("paused", self.state_json())

    def test_new_pause_answer_pauses_again(self):
        self.write_state(pause_ts=PAUSE_ANSWER["ts"])            # an older pause answer we already honoured
        self.nav.answered = {"copilot-down": dict(PAUSE_ANSWER, ts=PAUSE_ANSWER["ts"] + 500)}
        self.run_main()
        self.assertEqual(self.last_line(), "skip: paused by Uri")
        self.assertEqual(self.state_json()["pause_ts"], PAUSE_ANSWER["ts"] + 500)


class CapTests(CheckinCase):
    """G7: at most MAX_PER_DAY sent check-ins per local day."""

    def test_skip_at_cap(self):
        self.write_state(sent_day=TODAY, sent_count=cc.MAX_PER_DAY)
        self.run_main()
        self.assertEqual(self.last_line(), "skip: daily cap")
        self.assertEqual(self.runs.argvs("send"), [])
        self.assertEqual(self.state_json(), {"sent_day": TODAY, "sent_count": cc.MAX_PER_DAY})

    def test_cap_resets_next_day(self):
        self.write_state(sent_day="2026-10-30", sent_count=cc.MAX_PER_DAY)
        self.run_main()
        self.assertTrue(self.last_line().startswith("sent to "))
        self.assertEqual(self.state_json(), {"sent_day": TODAY, "sent_count": 1})

    def test_cap_counts_only_sends(self):
        self.run_main()
        self.run_main()
        self.assertEqual(self.state_json()["sent_count"], 2)
        self.write_state(sent_day=TODAY, sent_count=cc.MAX_PER_DAY - 1)
        self.run_main()                                  # the 16th send of the day
        self.assertEqual(self.state_json()["sent_count"], cc.MAX_PER_DAY)
        self.run_main()
        self.assertEqual(self.last_line(), "skip: daily cap")

    def test_skipped_runs_do_not_count(self):
        self.write_state(sent_day=TODAY, sent_count=15)
        self.online = False
        self.run_main()
        self.assertEqual(self.last_line(), "skip: offline")
        st = self.state_json()
        self.assertEqual(st.get("sent_day"), TODAY)
        self.assertEqual(st.get("sent_count"), 15)


class OfflineTests(CheckinCase):
    """G8: no send without a network; the local Navigator is still read, so alerts still fire."""

    def test_offline_skip(self):
        self.online = False
        self.run_main()
        self.assertEqual(self.last_line(), "skip: offline")
        self.assertEqual(self.runs.argvs("send"), [])
        self.assertEqual(self.nav.paths, ["/api/decisions", "/api/tree?window=7d", "/api/conversations/s1"])

    def test_offline_does_not_block_the_alert(self):
        self.online = False
        self.nav.events = [reply("API Error (500 upstream)", 300)]   # backoff expired, still failing
        self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 1)             # the alert goes out anyway
        self.assertEqual(self.runs.argvs("send"), [])
        self.assertEqual([ln.split(" ", 1)[1] for ln in self.log_lines()], ["skip: offline"])

    def test_consec_offline_alert_fires_at_4(self):
        self.online = False
        for _ in range(3):
            self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 0)
        self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 1)
        self.assertIn("check-ins have been skipped for an hour (thinks it is offline)", self.runs.argvs("ask")[0][6])

    def test_consec_offline_resets_after_send(self):
        self.online = False
        for _ in range(3):
            self.run_main()
        self.online = True
        self.run_main()
        self.online = False
        self.run_main()
        self.assertEqual(len(self.runs.argvs("ask")), 0)


class WakeTests(CheckinCase):
    """G8: a stale previous log line means the Mac slept; skip once, send on the next run."""

    def test_just_woke_skips(self):
        self.prewrite_log("2026-10-31T11:05:00", "skip: quiet hours")   # 55 min old
        self.run_main()
        self.assertEqual(self.last_line(), "skip: just woke")
        self.assertEqual(self.runs.argvs("send"), [])

    def test_next_run_sends(self):
        self.prewrite_log("2026-10-31T11:05:00", "skip: quiet hours")
        self.run_main()
        self.run_main()
        self.assertEqual([ln.split(" ", 1)[1] for ln in self.log_lines()],
                         ["skip: quiet hours", "skip: just woke", "sent to claude:s1 (exit 0) ok"])

    def test_fresh_log_sends(self):
        self.prewrite_log("2026-10-31T11:45:00", "skip: quiet hours")   # 15 min old
        self.run_main()
        self.assertTrue(self.last_line().startswith("sent to "))

    def test_empty_log_sends(self):
        self.run_main()
        self.assertTrue(self.last_line().startswith("sent to "))


class _CM:
    def __init__(self, exc=None):
        self.exc = exc

    def __enter__(self):
        if self.exc:
            raise self.exc

    def __exit__(self, *a):
        return False


class OnlineTests(unittest.TestCase):
    """The network probe itself: TCP connect to api.anthropic.com:443."""

    def probe(self, exc=None, side_effect=None):
        seen = []

        class DummySock:
            def close(self):
                pass

        def fake_create_connection(addr, timeout=None):
            seen.append((addr, timeout))
            if side_effect is not None:
                raise side_effect
            if exc is not None:
                raise exc
            return DummySock()

        urlopen_seen = []

        def fake_urlopen(*args, **kwargs):
            urlopen_seen.append((args, kwargs))
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(cc.socket, "create_connection", fake_create_connection), \
             mock.patch.object(cc.urllib.request, "urlopen", fake_urlopen):
            return cc.online(), seen, urlopen_seen

    def test_reachable_is_online(self):
        online, seen, urlopen_seen = self.probe(None)
        self.assertTrue(online)
        self.assertEqual(seen, [(("api.anthropic.com", 443), cc.NET_TIMEOUT)])
        self.assertEqual(urlopen_seen, [])

    def test_dns_failure_is_offline(self):
        self.assertFalse(self.probe(exc=OSError("name resolution failed"))[0])

    def test_timeout_is_offline(self):
        self.assertFalse(self.probe(side_effect=TimeoutError("timed out"))[0])


if __name__ == "__main__":
    unittest.main()
