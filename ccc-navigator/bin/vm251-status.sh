#!/usr/bin/env python3
"""Runs ON server .251 as claude-agent, from cron every 3 min:
    */3 * * * * ~/bin/vm251-status.sh
Writes ~/agent-status.json atomically: the agent processes in each running
Incus container, plus host-level ones under "host". Never fails hard: an
error is written into the JSON instead."""
import json
import os
import subprocess
import time

OUT = os.path.expanduser("~/agent-status.json")
PATTERN = "claude|codex|devin|opencode|droid|hermes|paperclip"
NOISE = ("sshd:", "ttyd", "postgres", "pgrep", "lxc monitor", "vm251-status")  # claude-agent matches PATTERN too
seen = set()  # the host sees container processes too: list each command once, under its container


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout


def agents(prefix):
    out = []
    for line in run(prefix + ["pgrep", "-fa", PATTERN]).splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or any(n in cmd for n in NOISE) or cmd[:80] in seen:
            continue  # one row per distinct command: worker pools repeat the same line
        seen.add(cmd[:80])
        out.append({"pid": int(pid), "cmd": cmd[:120]})
    return out


data = {"generated_at": int(time.time()), "host": "251", "containers": []}
try:
    for line in run(["sudo", "-n", "incus", "list", "-f", "csv", "-c", "ns"]).splitlines():
        name, _, state = line.partition(",")
        if name and state.upper() == "RUNNING":
            data["containers"].append({"name": name, "state": state,
                                       "agents": agents(["sudo", "-n", "incus", "exec", name, "--"])})
    data["containers"].append({"name": "host", "state": "RUNNING", "agents": agents([])})
except Exception as exc:  # noqa: BLE001 - report, never crash
    data["error"] = str(exc)[:300]

tmp = OUT + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.replace(tmp, OUT)
