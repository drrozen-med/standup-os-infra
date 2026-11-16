#!/usr/bin/env python3
"""runner-admission.py: keep the .251 CI runners from thrashing the container.

All the .251 runners live in one 5GB container (vera-ci). When every runner is
active at once, eleven heavy jobs (Android JVM, vite build, tsc) share that
memory: the kernel swaps, jobs crawl for hours and GitHub marks the runner
offline. The fix is admission control - only as many runners as the box can
carry are awake at a time.

For every repo with a runner on .251 this script asks GitHub whether that repo
has queued or running work, then it stops the runners whose repo is idle and
wakes the ones whose repo is waiting. It never stops a runner whose repo has a
job in flight, and it never has more than MAX_ACTIVE runners awake.

Run it from a timer (every 3 minutes). It is safe to run at any time: the worst
case is that a runner is woken a minute later than GitHub would have woken it.

Usage: runner-admission.py [--dry-run] [--max N]
"""

import argparse
import json
import os
import subprocess
import sys

HOST = "root@187.124.181.251"
CONTAINER = "vera-ci"

# repo -> the systemd unit of its runner inside the container
RUNNERS = {
    "airline": "actions.runner.drrozen-med-airline.airline-ci-251.service",
    "boardsBridge": "actions.runner.drrozen-med-boardsBridge.hermes-bb.service",
    "cdl-bridge": "actions.runner.drrozen-med-cdl-bridge.cdl-ci-251.service",
    "nurse_job_interview": "actions.runner.drrozen-med-nurse_job_interview.nbi-ci-251.service",
    "road-trip-companion": "actions.runner.drrozen-med-road-trip-companion.rt-ci-251.service",
    "run-residency": "actions.runner.drrozen-med-run-residency.rr-ci-251.service",
    "shared-marketing": "actions.runner.drrozen-med-shared-marketing.sm-ci-251.service",
    "shlav-bet-family": "actions.runner.drrozen-med-shlav-bet-family.fm-ci-251.service",
    "sleepySounds_love": "actions.runner.drrozen-med-sleepySounds-love.ss-ci-251.service",
    "vera": "actions.runner.drrozen-med-vera.vera-ci-251.service",
}
# extra runners for the repos whose queues are longest:
# sleepySounds' Android JVM gate is long, boardsBridge has many PR checks waiting
EXTRA = {
    "sleepySounds_love": ["actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-b.service"],
    "boardsBridge": ["actions.runner.drrozen-med-boardsBridge.hermes-bb-2.service",
                     "actions.runner.drrozen-med-boardsBridge.hermes-bb-3.service"],
}

MAX_ACTIVE = int(os.environ.get("RUNNER_ADMISSION_MAX", "4"))
SSH = ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", HOST]


def runs(repo, status):
    """The repo's runs in a status (queued, in_progress), newest first."""
    out = subprocess.run(
        ["gh", "api", "repos/drrozen-med/%s/actions/runs?status=%s&per_page=20" % (repo, status),
         "--jq", "[.workflow_runs[] | {id, name, created_at, head_branch}]"],
        capture_output=True, text=True)
    try:
        return json.loads(out.stdout or "[]")
    except ValueError:
        return []


def unit_state(unit):
    """(active, ) for a runner unit inside the container."""
    out = subprocess.run(SSH + ["incus exec %s -- systemctl is-active %s" % (CONTAINER, unit)],
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def runner_status(repo, unit):
    """'online' / 'offline' as GitHub sees the runner, or None when it cannot be read."""
    name = unit.split(".")[-2]
    out = subprocess.run(
        ["gh", "api", "repos/drrozen-med/%s/actions/runners" % repo, "--jq",
         '[.runners[] | select(.name=="' + name + '") | .status]'],
        capture_output=True, text=True)
    return (out.stdout or "").strip().strip('"') or None


def set_unit(unit, action):
    subprocess.run(SSH + ["incus exec %s -- systemctl %s %s" % (CONTAINER, action, unit)],
                   capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=MAX_ACTIVE)
    args = ap.parse_args()

    need = {}          # repo -> (queued, running)
    for repo in RUNNERS:
        q = len(runs(repo, "queued"))
        r = len(runs(repo, "in_progress"))
        need[repo] = (q, r)

    # a runner whose repo has a job in flight stays up; its repo having queued work is a reason to wake it
    wanted = [repo for repo, (q, r) in need.items() if q > 0 or r > 0]
    # repos that are busy first (their job is running now), then the ones waiting longest
    wanted.sort(key=lambda repo: (-need[repo][1], -need[repo][0], repo))

    active_now = {}
    for repo in set(list(RUNNERS) + list(EXTRA)):
        units = ([RUNNERS[repo]] if repo in RUNNERS else []) + EXTRA.get(repo, [])
        for unit in units:
            if unit_state(unit) == "active":
                active_now.setdefault(repo, []).append(unit)

    awake = sum(len(v) for v in active_now.values())
    started, stopped = [], []

    # 1. wake the runners whose repo is waiting (room permitting)
    for repo in wanted:
        if awake >= args.max:
            break
        units = ([RUNNERS[repo]] if repo in RUNNERS else []) + EXTRA.get(repo, [])
        for unit in units:
            if unit in active_now.get(repo, []):
                continue
            if awake >= args.max:
                break
            started.append((repo, unit))
            awake += 1
    # 2. stop the runners whose repo has nothing queued and nothing running
    for repo, units in active_now.items():
        q, r = need.get(repo, (0, 0))
        if q or r:
            continue
        for unit in units:
            stopped.append((repo, unit))
            awake -= 1

    if args.dry_run:
        for repo in sorted(need):
            q, r = need[repo]
            print("%-22s queued=%-3d running=%-3d %s" % (repo, q, r, ",".join(active_now.get(repo, [])) or "-"))
        print("would start:", started)
        print("would stop: ", stopped)
        return 0

    for repo, unit in started:
        set_unit(unit, "start")
        print("start %s (%s)" % (unit, repo))
    for repo, unit in stopped:
        set_unit(unit, "stop")
        print("stop  %s (%s, no queued or running work)" % (unit, repo))

    # a runner whose repo is waiting but which GitHub calls offline has lost its session:
    # restart it so it re-registers and picks the work up
    for repo in wanted:
        q, r = need[repo]
        if not q:
            continue
        unit = RUNNERS.get(repo)
        if not unit or unit in [u for _, u in started] or unit in [u for _, u in stopped]:
            continue
        if runner_status(repo, unit) == "offline":
            set_unit(unit, "restart")
            print("restart %s (%s: GitHub says offline while %d runs wait)" % (unit, repo, q))
    return 0


if __name__ == "__main__":
    sys.exit(main())
