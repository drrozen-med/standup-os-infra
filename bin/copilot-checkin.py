#!/usr/bin/env python3
"""Nudge Uri's Co-pilot session to do its periodic check-in.

Run by launchd (com.standup-os.copilot-checkin, every 30 min). Sends one
message into the Co-pilot session through the Navigator; the Co-pilot does the
work (closes stale decisions, files new ones). Skips when:
  - no Co-pilot is set,
  - it is quiet hours (00:00-07:00 local),
  - the Co-pilot was active in the last 10 minutes (Uri is talking to it, or it
    is still busy).
Every run appends one line to ~/.ccc-navigator/copilot-checkin.log.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.request

NAV_URL = os.environ.get("CCC_NAV_URL", "http://127.0.0.1:8095")
NAV = os.path.expanduser("~/projects/standup-os/ccc-navigator/nav.py")
LOG = os.path.expanduser("~/.ccc-navigator/copilot-checkin.log")
QUIET = (0, 7)          # local hours [start, end)
IDLE_MIN = 10           # minutes the Co-pilot must have been quiet

PROMPT = """[Automatic check-in, every 30 min. Uri is not typing this.]
NAV=~/projects/standup-os/ccc-navigator/nav.py
1. Run `python3 $NAV decisions --table` and `python3 $NAV brief --table`.
2. Close decisions that are verifiably no longer needed (the work is done, the PR merged, the deadline passed, or a newer decision replaces it): `NAV_ACTOR=copilot python3 $NAV decide <id> other --note "closed by co-pilot: <reason>"`. Check before closing. Never pick an answer for Uri on a real choice.
3. If something new needs Uri and is not already in the inbox, add it: `NAV_ACTOR=copilot python3 $NAV ask --id <short-id> --question "..." --option "..." --option "..."`. One question each, tap-able options, only what truly needs him.
4. Reply in at most 3 short lines: what you closed, added, or noticed. If nothing changed, reply exactly: Check-in: nothing new."""


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))


def get(path):
    with urllib.request.urlopen(NAV_URL + path, timeout=20) as r:
        return json.load(r)


def last_activity(key):
    """UTC datetime of the Co-pilot's newest transcript event, or None."""
    sid = key.split(":", 1)[1]
    ev = get("/api/conversations/" + sid).get("events") or []
    for e in reversed(ev):
        ts = e.get("ts")
        if ts:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return None


def main():
    now = datetime.datetime.now()
    if QUIET[0] <= now.hour < QUIET[1]:
        return log("skip: quiet hours")
    cp = (get("/api/tree?window=7d").get("copilot") or {}).get("key")
    if not cp:
        return log("skip: no co-pilot set")
    last = last_activity(cp)
    if last:
        ago = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60
        if ago < IDLE_MIN:
            return log("skip: co-pilot active %.0f min ago" % ago)
    r = subprocess.run([sys.executable, NAV, "send", cp, PROMPT],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, NAV_ACTOR="copilot-checkin"))
    log("sent to %s (exit %d) %s" % (cp, r.returncode, (r.stdout or r.stderr).strip()[:200].replace("\n", " ")))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # launchd has no one to show a traceback to
        log("error: %s" % e)
