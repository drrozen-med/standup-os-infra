"""Tests for bin/alert-alarm.py: rings only for open alerts, outside quiet hours, when not muted.

unittest only; the Navigator call and subprocess.run are mocked, so nothing makes a sound.
Run: python3 bin/test_alert_alarm.py
"""
import datetime
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("alert_alarm", os.path.join(HERE, "alert-alarm.py"))
_spec = importlib.util.spec_from_loader("alert_alarm", _loader)
aa = importlib.util.module_from_spec(_spec)
_loader.exec_module(aa)

NOON = datetime.datetime(2026, 11, 3, 12, 0).timestamp()


class Alarm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        aa.STATE = os.path.join(self.tmp.name, "alarm.json")
        self.run = mock.patch.object(aa.subprocess, "run").start()
        self.alerts = mock.patch.object(aa, "open_alerts", return_value=[]).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_no_alerts_no_sound(self):
        self.assertEqual(aa.check(NOON), "no open alerts")
        self.run.assert_not_called()

    def test_open_alert_rings_and_speaks_the_newest(self):
        self.alerts.return_value = [{"title": "Old one", "raised_at": 1}, {"title": "Hide stations?", "raised_at": 2}]
        self.assertEqual(aa.check(NOON), "rang for 2 alert(s)")
        cmds = [c.args[0] for c in self.run.call_args_list]
        self.assertEqual([c[0] for c in cmds], ["afplay"] * aa.RINGS + ["say"])
        self.assertIn("Hide stations? and 1 more", cmds[-1][1])

    def test_quiet_hours(self):
        self.alerts.return_value = [{"title": "x"}]
        self.assertEqual(aa.check(datetime.datetime(2026, 11, 3, 6, 59).timestamp()), "quiet hours")
        self.run.assert_not_called()

    def test_mute_and_unmute(self):
        self.alerts.return_value = [{"title": "x"}]
        with mock.patch.object(aa.time, "time", return_value=NOON):
            aa.main(["mute", "30"])
        self.assertEqual(aa.check(NOON + 29 * 60), "muted")
        self.assertEqual(aa.check(NOON + 31 * 60), "rang for 1 alert(s)")
        aa.main(["unmute"])
        self.assertNotIn("mute_until", aa.load())

    def test_navigator_down_is_quiet(self):
        self.alerts.side_effect = OSError("refused")
        self.assertTrue(aa.check(NOON).startswith("navigator unreachable"))
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
