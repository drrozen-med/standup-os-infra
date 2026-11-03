#!/usr/bin/env python3
"""Mac-level alarm for open loud alerts: the Mac itself rings and speaks until Uri acknowledges.

The Navigator banner and chime depend on Safari (tab open, sound unlocked by a click). This does not:
run by launchd (com.standup-os.alert-alarm, every 30 s), it reads the Navigator's open alerts and,
while any is open, plays a loud system sound and says the alert out loud. It stops as soon as the
alert is acknowledged anywhere ("I'm on it" in the Navigator, `nav alert ack`, or answering the
linked decision). Skips quiet hours (00:00-07:00 local) and while muted.

  alert-alarm.py            one check (what launchd runs)
  alert-alarm.py mute [MIN] mute the alarm for MIN minutes (default 60)
  alert-alarm.py unmute

State: ~/.ccc-navigator/alarm.json ({"mute_until": epoch}); env ALERT_ALARM_STATE overrides (tests).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

NAV_URL = os.environ.get("CCC_NAV_URL", "http://127.0.0.1:8095")
STATE = os.environ.get("ALERT_ALARM_STATE", os.path.expanduser("~/.ccc-navigator/alarm.json"))
QUIET = (0, 7)          # local hours [start, end)
SOUND = "/System/Library/Sounds/Sosumi.aiff"
RINGS = 3               # sound plays per check
VOLUME = "3"            # afplay volume multiplier (1 = normal)
SAY_CHARS = 120


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def open_alerts():
    with urllib.request.urlopen(NAV_URL + "/api/alerts", timeout=5) as r:
        return json.load(r).get("open") or []


def ring(alerts):
    newest = max(alerts, key=lambda a: a.get("raised_at") or 0)
    title = " ".join(str(newest.get("title") or "a decision needs you").split())[:SAY_CHARS]
    more = " and %d more" % (len(alerts) - 1) if len(alerts) > 1 else ""
    for _ in range(RINGS):
        subprocess.run(["afplay", "-v", VOLUME, SOUND], timeout=10, check=False)
    subprocess.run(["say", "Uri. %s%s. Open the Navigator." % (title, more)], timeout=30, check=False)


def check(now=None):
    now = now or time.time()
    hour = time.localtime(now).tm_hour
    if QUIET[0] <= hour < QUIET[1]:
        return "quiet hours"
    if (load().get("mute_until") or 0) > now:
        return "muted"
    try:
        alerts = open_alerts()
    except Exception as e:      # navigator down: nothing to ring about, try again next run
        return "navigator unreachable: %s" % e
    if not alerts:
        return "no open alerts"
    ring(alerts)
    return "rang for %d alert(s)" % len(alerts)


def main(argv):
    if argv[:1] == ["mute"]:
        minutes = int(argv[1]) if len(argv) > 1 else 60
        st = load()
        st["mute_until"] = time.time() + minutes * 60
        save(st)
        print("alarm muted for %d min" % minutes)
        return 0
    if argv[:1] == ["unmute"]:
        st = load()
        st.pop("mute_until", None)
        save(st)
        print("alarm unmuted")
        return 0
    print(check())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
