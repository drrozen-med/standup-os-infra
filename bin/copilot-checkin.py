#!/usr/bin/env python3
"""Nudge Uri's Co-pilot session to do its periodic check-in.

Run by launchd (com.standup-os.copilot-checkin, every 30 min). Sends one
message into the Co-pilot session through the Navigator; the Co-pilot does the
work (closes stale decisions, files new ones). Skips when:
  - no Co-pilot is set,
  - it is quiet hours (00:00-07:00 local),
  - Uri paused check-ins (he answered the copilot-down decision with
    "Pause check-ins"): until he reopens it or clears "paused" in the state,
  - the Co-pilot was active in the last 10 minutes (Uri is talking to it, or it
    is still busy),
  - its last reply was an API/usage-limit error less than 4 hours ago,
  - the network is down (api.anthropic.com does not answer within 5 s),
  - the Mac just woke from sleep (the previous run's log line is over 45 min
    old; that run skips, the next one sends),
  - MAX_PER_DAY check-ins were already sent today.
While the Co-pilot's last reply is an error, one copilot-down decision sits in
Uri's inbox (a fresh alert at most every 12 h; an old one is reopened, never
duplicated) and closes itself with "recovered automatically" once a check-in
succeeds again. Every run appends one line to ~/.ccc-navigator/copilot-checkin.log.
Alert and counter state live in ~/.ccc-navigator/copilot-checkin.json; env
COPILOT_CHECKIN_STATE / COPILOT_CHECKIN_LOG override the paths (for tests).
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

NAV_URL = os.environ.get("CCC_NAV_URL", "http://127.0.0.1:8095")
NAV = os.path.expanduser("~/projects/standup-os/ccc-navigator/nav.py")
LOG = os.environ.get("COPILOT_CHECKIN_LOG", os.path.expanduser("~/.ccc-navigator/copilot-checkin.log"))
STATE = os.environ.get("COPILOT_CHECKIN_STATE", os.path.expanduser("~/.ccc-navigator/copilot-checkin.json"))
QUIET = (0, 7)          # local hours [start, end)
IDLE_MIN = 10           # minutes the Co-pilot must have been quiet
BACKOFF_MIN = 240       # after an API/credit error reply, wait this long before the next check-in
ALERT_ID = "copilot-down"            # decision that tells Uri check-ins are failing
ALERT_MIN = 720         # minutes between copilot-down alerts
PAUSE_CHOICE = "Pause check-ins"     # his answer that stops check-ins
MAX_PER_DAY = 16        # sent check-ins per local calendar day
NET_URL = "https://api.anthropic.com"
NET_TIMEOUT = 5         # seconds to wait for the network check
WAKE_MIN = 45           # previous log line older than this means the Mac slept through slots

PROMPT = """[Automatic check-in, every 30 min. Uri is not typing this.]
NAV=~/projects/standup-os/ccc-navigator/nav.py
1. Run `python3 $NAV decisions --table` and `python3 $NAV brief --table`.
2. Close decisions that are verifiably no longer needed (the work is done, the PR merged, the deadline passed, or a newer decision replaces it): `NAV_ACTOR=copilot python3 $NAV decide <id> other --note "closed by co-pilot: <reason>"`. Check before closing. Never pick an answer for Uri on a real choice.
3. If something new needs Uri and is not already in the inbox, add it: `NAV_ACTOR=copilot python3 $NAV ask --id <short-id> --question "..." --option "..." --option "..."`. One question each, tap-able options, only what truly needs him.
4. Rank what matters most for Uri: `NAV_ACTOR=copilot python3 $NAV rank <id1> <id2> ... --why <id>=<one short reason>` (at most 5, most important first).
5. Reply in at most 3 short lines: what you closed, added, or noticed. If nothing changed, reply exactly: Check-in: nothing new."""


def log(msg):
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    with open(LOG, "a") as f:
        f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))


def get(path):
    with urllib.request.urlopen(NAV_URL + path, timeout=20) as r:
        return json.load(r)


def nav(*args):
    """Run nav.py as copilot-checkin -> (exit code, first 200 chars of its output)."""
    r = subprocess.run([sys.executable, NAV] + list(args), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, NAV_ACTOR="copilot-checkin"))
    return r.returncode, (r.stdout or r.stderr or "").strip()[:200].replace("\n", " ")


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def decisions():
    """(ids of open decisions, {id: answer} of the recently answered ones)."""
    d = get("/api/decisions")
    return ({str(x.get("id") or "") for x in d.get("open") or []},
            {str(x.get("id") or ""): (x.get("answer") or {}) for x in d.get("answered") or []})


def last_activity(key):
    """(UTC datetime of the newest transcript event, whether the newest reply was an API/limit error, that reply's text)."""
    sid = key.split(":", 1)[1]
    ev = get("/api/conversations/" + sid).get("events") or []
    when = None
    for e in reversed(ev):
        ts = e.get("ts")
        if ts:
            when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            break
    failed, txt = False, ""
    for e in reversed(ev):
        if e.get("type") == "assistant":
            txt = " ".join(b.get("text") or "" for b in e.get("blocks") or [] if b.get("kind") == "text")
            failed = txt.startswith("API Error") or "out of usage credits" in txt
            break
    return when, failed, txt


def online():
    """True when the network is up: any HTTP answer from the API host counts; DNS/connection failure does not."""
    try:
        with urllib.request.urlopen(urllib.request.Request(NET_URL, method="HEAD"), timeout=NET_TIMEOUT):
            pass
    except urllib.error.HTTPError:      # an HTTP status came back: we are online
        return True
    except Exception:
        return False
    return True


def woke():
    """True when the previous run's log line is stale: with a run every 30 min, a gap means the Mac slept."""
    try:
        with open(LOG) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        when = datetime.datetime.fromisoformat(lines[-1].split(" ", 1)[0])
    except (OSError, IndexError, ValueError):
        return False
    return (datetime.datetime.now() - when).total_seconds() / 60 > WAKE_MIN


def paused(st, open_ids, answered):
    """True when Uri's "Pause check-ins" answer stands: skip the run (state saved on every change)."""
    ans = answered.get(ALERT_ID) or {}
    if st.get("paused"):
        if ALERT_ID in open_ids or (ans and ans.get("choice") != PAUSE_CHOICE):
            st.pop("paused", None)      # he reopened the decision (or answered differently): resume
            st.pop("pause_ts", None)
            save_state(st)
        else:
            return True
    elif ans.get("choice") == PAUSE_CHOICE and ans.get("ts") != st.get("pause_ts"):
        st["paused"] = True             # a pause answer we have not honoured yet (pause_ts keeps a hand-cleared state cleared)
        st["pause_ts"] = ans.get("ts")
        save_state(st)
        return True
    return False


def alert(st, open_ids, answered, failed, reason):
    """Keep exactly one copilot-down decision in Uri's inbox while check-ins fail; close it on recovery."""
    if ALERT_ID in open_ids:
        if not failed:
            nav("decide", ALERT_ID, "other", "--note", "recovered automatically")
        return
    if not failed:
        return
    if st.get("last_alert"):
        try:
            since = (datetime.datetime.now() - datetime.datetime.fromisoformat(st["last_alert"])).total_seconds() / 60
        except ValueError:
            since = ALERT_MIN           # unreadable timestamp: treat the last alert as stale
        if since < ALERT_MIN:
            return
    if ALERT_ID in answered:
        code, _ = nav("decide", ALERT_ID, "--reopen")     # the old alert is answered: surface it again (re-ask would hit "exists")
    else:
        code, _ = nav("ask", "--id", ALERT_ID,
                      "--question", "Co-pilot check-ins are failing: %s. Fix?" % reason,
                      "--option", "I'll look", "--option", PAUSE_CHOICE)
        if code:                        # the id already exists but is no longer listed as answered: bring that one back
            code, _ = nav("decide", ALERT_ID, "--reopen")
    if not code:
        st["last_alert"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_state(st)


def main():
    now = datetime.datetime.now()
    if QUIET[0] <= now.hour < QUIET[1]:
        return log("skip: quiet hours")
    st = load_state()
    open_ids, answered = decisions()
    if paused(st, open_ids, answered):
        return log("skip: paused by Uri")
    cp = (get("/api/tree?window=7d").get("copilot") or {}).get("key")
    if not cp:
        return log("skip: no co-pilot set")
    last, failed, txt = last_activity(cp)
    alert(st, open_ids, answered, failed, " ".join(txt.split())[:80] or "no reply")
    if last:
        ago = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60
        if ago < IDLE_MIN:
            return log("skip: co-pilot active %.0f min ago" % ago)
        if failed and ago < BACKOFF_MIN:     # its last reply was an API/credit error: do not pile more onto it
            return log("skip: last reply was an error %.0f min ago" % ago)
    if not online():
        return log("skip: offline")
    if woke():
        return log("skip: just woke")
    today = now.strftime("%Y-%m-%d")
    sent = st.get("sent_count", 0) if st.get("sent_day") == today else 0
    if sent >= MAX_PER_DAY:
        return log("skip: daily cap")
    code, out = nav("send", cp, PROMPT)
    st["sent_day"], st["sent_count"] = today, sent + 1
    save_state(st)
    log("sent to %s (exit %d) %s" % (cp, code, out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # launchd has no one to show a traceback to
        log("error: %s" % e)
