"""Who is looking at the Navigator, and the Mac alarm that follows them.

The rule Uri asked for (2026-11-15): the Navigator does not keep going without
a real Navigator open. The page polls every 10 s, the server counts the
polling clients, and the Mac alarm only speaks when that count is above zero.
"""
import importlib.util
import unittest
from pathlib import Path

import presence

ALARM = Path(__file__).resolve().parent.parent.parent / "bin" / "alert-alarm.py"


def load_alarm():
    spec = importlib.util.spec_from_file_location("alert_alarm", ALARM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
           "Host": "127.0.0.1:8095"}
PHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Version/18.0 Mobile Safari",
         "Cf-Connecting-Ip": "1.2.3.4", "Host": "cmd-m110rk.vera.baby"}
SCRIPT = {"User-Agent": "Python-urllib/3.14", "Host": "127.0.0.1:8095"}


class PresenceTests(unittest.TestCase):
    def test_a_browser_and_the_phone_are_views(self):
        p = presence.Presence()
        self.assertTrue(p.is_view(BROWSER))
        self.assertTrue(p.is_view(PHONE))

    def test_scripts_are_not_views(self):
        p = presence.Presence()
        self.assertFalse(p.is_view(SCRIPT))
        self.assertFalse(p.is_view({"User-Agent": "curl/8.4"}))
        self.assertFalse(p.is_view({"User-Agent": "python-requests/2.32"}))
        self.assertFalse(p.is_view({"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}))
        self.assertFalse(p.is_view({}))

    def test_only_views_are_recorded(self):
        p = presence.Presence()
        self.assertFalse(p.note(SCRIPT))
        self.assertEqual(p.count(), 0)
        self.assertTrue(p.note(BROWSER))
        self.assertEqual(p.count(), 1)

    def test_the_window_closes_when_the_tab_closes(self):
        p = presence.Presence(window=45.0)
        p.note(BROWSER, now=1000.0)
        self.assertEqual(p.count(now=1030.0), 1)
        self.assertEqual(p.count(now=1060.0), 0)   # the tab is gone

    def test_the_phone_counts_as_a_view(self):
        p = presence.Presence()
        p.note(PHONE, now=1000.0)
        self.assertEqual(p.count(now=1001.0), 1)

    def test_the_server_says_so(self):
        p = presence.Presence()
        p.note(BROWSER, now=1000.0)
        self.assertTrue(p.last_seen())


class AlarmGateTests(unittest.TestCase):
    def setUp(self):
        self.alarm = load_alarm()

    def test_the_alarm_asks_the_server_who_is_looking(self):
        src = ALARM.read_text()
        self.assertIn("/api/presence", src)
        self.assertIn("def navigator_open(", src)

    def test_no_navigator_means_no_voice(self):
        alarm = self.alarm
        alarm.open_alerts = lambda: [{"id": "a1", "title": "Decision for you", "raised_at": 1}]
        alarm.navigator_open = lambda: False
        rung = []
        alarm.ring = lambda alerts: rung.append(alerts)
        out = alarm.check()
        self.assertEqual(rung, [])                       # nothing was said
        self.assertIn("no Navigator open", out)

    def test_an_open_navigator_means_the_alarm_rings(self):
        alarm = self.alarm
        alarm.open_alerts = lambda: [{"id": "a1", "title": "Decision for you", "raised_at": 1}]
        alarm.navigator_open = lambda: True
        rung = []
        alarm.ring = lambda alerts: rung.append(alerts)
        out = alarm.check()
        self.assertEqual(len(rung), 1)
        self.assertIn("rang for 1 alert", out)

    def test_an_unknown_presence_keeps_the_mac_quiet(self):
        alarm = self.alarm
        alarm.open_alerts = lambda: [{"id": "a1", "title": "Decision for you", "raised_at": 1}]

        def boom():
            raise OSError("404")
        alarm.navigator_open = boom
        rung = []
        alarm.ring = lambda alerts: rung.append(alerts)
        out = alarm.check()
        self.assertEqual(rung, [])
        self.assertIn("presence unknown", out)


if __name__ == "__main__":
    unittest.main()
