#!/usr/bin/env python3
"""Agent hook (Stop, SessionEnd, SubagentStop): tell the navigator a turn or session ended.

Works for Claude Code, Codex and Devin, which share the hook format; pass the
engine as the first argument (default claude): claude_hook.py codex

The navigator logs it in the running log at once and refreshes that session's
brief a few seconds later. Sends only the event name, session id, folder and
end reason, never transcript text. Always exits 0 and never prints, so it can
not slow or change a Claude session; if the navigator is down nothing happens
and the 2-minute briefer pass catches up.

Register in ~/.claude/settings.json (async, so the turn never waits on it):
  {"type": "command", "async": true, "timeout": 5,
   "command": "/usr/bin/python3 ~/projects/standup-os/ccc-navigator/hooks/claude_hook.py"}
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("CCC_NAV_HOOK_URL", "http://127.0.0.1:8095/api/hook")


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        body = {k: data.get(k) for k in ("hook_event_name", "session_id", "cwd", "reason")}
        body["engine"] = sys.argv[1] if len(sys.argv) > 1 else "claude"
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
