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
import datetime
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
    "run-residency": "actions.runner.drrozen-med-run-residency.rr-ci-251.service",
    "shared-marketing": "actions.runner.drrozen-med-shared-marketing.sm-ci-251.service",
    "shlav-bet-family": "actions.runner.drrozen-med-shlav-bet-family.fm-ci-251.service",
    "sleepySounds_love": "actions.runner.drrozen-med-sleepySounds-love.ss-ci-251.service",
    "vera": "actions.runner.drrozen-med-vera.vera-ci-251.service",
}
# extra runners for the repos whose queues are longest:
# sleepySounds' Android JVM gate is long, boardsBridge has many PR checks waiting
EXTRA = {
    "sleepySounds_love": ["actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-b.service",
                          "actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-c.service",
                          "actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-d.service"],
    "run-residency": ["actions.runner.drrozen-med-run-residency.rr-ci-252.service"],
    "boardsBridge": ["actions.runner.drrozen-med-boardsBridge.hermes-bb-2.service"],
}

# The GitHub runner name of a unit, when it differs from the unit's own name (a runner
# registered under another name because GitHub would not delete the old registration).
RUNIT = {
    # the boardsBridge lane re-registered the primary runner under a new name
    "actions.runner.drrozen-med-boardsBridge.hermes-bb.service": "hermes-bb-4",
}
MAX_ACTIVE = int(os.environ.get("RUNNER_ADMISSION_MAX", "4"))
RESTART_COOLDOWN = int(os.environ.get("RUNNER_ADMISSION_COOLDOWN", "900"))  # seconds between restarts of one runner
STATE = os.path.expanduser("~/.ccc-navigator/runner-admission-state.json")
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


_RUNNER_INFO = {}


def runner_info(repo):
    """{name: {"status":..., "busy":...}} as GitHub sees the repo's runners (cached per run)."""
    if repo not in _RUNNER_INFO:
        out = subprocess.run(
            ["gh", "api", "repos/drrozen-med/%s/actions/runners?per_page=50" % repo, "--jq",
             '[.runners[] | {name: .name, status: .status, busy: .busy}]'],
            capture_output=True, text=True)
        try:
            rows = json.loads(out.stdout or "[]")
        except ValueError:
            rows = []
        _RUNNER_INFO[repo] = {r.get("name"): r for r in rows if isinstance(r, dict)}
    return _RUNNER_INFO[repo]


def runner_status(repo, unit):
    """'online' / 'offline' as GitHub sees the runner, or None when it cannot be read."""
    return (runner_info(repo).get(RUNIT.get(unit, unit.split(".")[-2])) or {}).get("status")


_BUSY_DIRS = None


def container_busy_dirs():
    """The runner directories on .251 that currently run a job (the worker processes).
    GitHub's own "busy" flag stays true for minutes after a runner was killed mid-job, and
    trusting it would keep dead runners awake and let the concurrency drift."""
    global _BUSY_DIRS
    if _BUSY_DIRS is None:
        cmd = r"""pgrep -af Runner.Worker | sed -n 's/.*\(\/opt\/gha[a-z0-9-]*\)\/bin.*/\1/p' | sort -u"""
        out = subprocess.run(SSH + ["incus exec %s -- bash -c %s" % (CONTAINER, cmd)],
                             capture_output=True, text=True, timeout=60)
        _BUSY_DIRS = set(out.stdout.split())
    return _BUSY_DIRS


# the runner directory on .251 behind each unit (the unit name is not the directory name)
DIR = {
    "actions.runner.drrozen-med-vera.vera-ci-251.service": "/opt/gha",
    "actions.runner.drrozen-med-airline.airline-ci-251.service": "/opt/gha-airline",
    "actions.runner.drrozen-med-boardsBridge.hermes-bb.service": "/opt/gha-runner-bb",
    "actions.runner.drrozen-med-boardsBridge.hermes-bb-2.service": "/opt/gha-runner-bb-2",
    "actions.runner.drrozen-med-boardsBridge.hermes-bb-3.service": "/opt/gha-runner-bb-3",
    "actions.runner.drrozen-med-boardsBridge.hermes-bb-4.service": "/opt/gha-runner-bb-4",
    "actions.runner.drrozen-med-cdl-bridge.cdl-ci-251.service": "/opt/gha-cdl",
    "actions.runner.drrozen-med-nurse_job_interview.nbi-ci-251.service": "/opt/gha-nbi",
    "actions.runner.drrozen-med-road-trip-companion.rt-ci-251.service": "/opt/gha-rt",
    "actions.runner.drrozen-med-run-residency.rr-ci-251.service": "/opt/gha-rr",
    "actions.runner.drrozen-med-run-residency.rr-ci-252.service": "/opt/gha-runner-bb-3",
    "actions.runner.drrozen-med-shared-marketing.sm-ci-251.service": "/opt/gha-sm",
    "actions.runner.drrozen-med-shared-marketing.sm-ci-251-b.service": "/opt/gha-sm-2",
    "actions.runner.drrozen-med-slav-bet-family.fm-ci-251.service": "/opt/gha-fm",
    "actions.runner.drrozen-med-sleepySounds-love.ss-ci-251.service": "/opt/gha-ss",
    "actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-b.service": "/opt/gha-ss-2",
    "actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-c.service": "/opt/gha-airline-2",
    "actions.runner.drrozen-med-sleepySounds_love.ss-ci-251-d.service": "/opt/gha-rt-2",
}


def runner_busy(repo, unit):
    """True when this runner is actually running a job (the container's worker processes)."""
    dirs = container_busy_dirs()
    if not dirs:
        return bool((runner_info(repo).get(RUNIT.get(unit, unit.split(".")[-2])) or {}).get("busy"))
    return DIR.get(unit, "") in dirs


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(state, fh, indent=1)
    except OSError:
        pass


def set_unit(unit, action):
    subprocess.run(SSH + ["incus exec %s -- systemctl %s %s" % (CONTAINER, action, unit)],
                   capture_output=True, text=True)


def cancel_dead_queued(repo, min_age_min=30):
    """Cancel queued runs whose branch has no open PR. GitHub cancels a job that waits 24h and
    counts it as a failed check, so a pile of stale queued runs turns every PR red. They cost
    nothing to cancel: no open PR means nobody is waiting for that check."""
    out = subprocess.run(["gh", "api", "repos/drrozen-med/%s/actions/runs?status=queued&per_page=50" % repo,
                          "--jq", "[.workflow_runs[] | {id, branch:.head_branch, created:.created_at}]"],
                         capture_output=True, text=True)
    try:
        queued = json.loads(out.stdout or "[]")
    except ValueError:
        return 0
    if not queued:
        return 0
    prs = subprocess.run(["gh", "api", "repos/drrozen-med/%s/pulls?state=open&per_page=100" % repo,
                           "--jq", "[.[].head.ref]"], capture_output=True, text=True)
    try:
        live = set(json.loads(prs.stdout or "[]"))
    except ValueError:
        live = set()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    n = 0
    default_branch = subprocess.run(["gh", "api", "repos/drrozen-med/%s" % repo, "--jq", ".default_branch"],
                                    capture_output=True, text=True).stdout.strip()
    for run in queued:
        if run.get("branch") in live:
            continue
        if run.get("branch") == default_branch:
            continue      # the default branch is what the board measures: never cancel it
        try:
            age = (now - _dt.datetime.fromisoformat(str(run["created"]).replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            continue
        if age < min_age_min:
            continue
        rr = subprocess.run(["gh", "run", "cancel", str(run["id"]), "--repo", "drrozen-med/%s" % repo],
                            capture_output=True, text=True)
        if rr.returncode == 0:
            n += 1
            print("cancel stale queued run %s (%s, %.0f min old)" % (run["id"], run.get("branch"), age))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=MAX_ACTIVE)
    args = ap.parse_args()

    need = {}          # repo -> (queued, running, queued_on_main)
    for repo in RUNNERS:
        q = len(runs(repo, "queued"))
        r = len(runs(repo, "in_progress"))
        qm = len([x for x in runs(repo, "queued") if x.get("head_branch") == "main"])
        need[repo] = (q, r, qm)

    # a runner whose repo has a job in flight stays up; its repo having queued work is a reason to wake it
    wanted = [repo for repo, (q, r, _qm) in need.items() if q > 0 or r > 0]
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

    # The cap counts runners that are AWAKE, not jobs that are running: GitHub hands work to any
    # online runner, so the number of awake runners IS the concurrency of the box. .251 has four
    # cores (and 70% steal from the hypervisor), and under that load the e2e suites fail on their
    # own timeouts - the airline browser-full logged "machine load 26.5 on 4 cores ... a timing
    # failure below may be load, not a bug". So at most args.max runners stay awake; a runner with
    # a job is never stopped (that would kill work in flight), and a repo whose MAIN run is queued
    # keeps its primary runner.
    awake = sum(len(v) for v in active_now.values())
    busy = sum(1 for repo, units in active_now.items() for u in units if runner_busy(repo, u))

    def keep_first(repo):
        """The runner of this repo that is kept awake when there is room for only one."""
        units = active_now.get(repo) or []
        primary = RUNNERS.get(repo)
        return primary if primary in units else (units[0] if units else None)

    # 1. stop the runners whose repo has nothing queued and nothing running
    for repo, units in active_now.items():
        q, r, _qm = need.get(repo, (0, 0, 0))
        if q or r:
            continue
        for unit in units:
            stopped.append((repo, unit))
            awake -= 1

    # 1b. stale queued runs (no open PR) are dead weight: GitHub cancels them after 24h and counts
    #     that as a failed check, which is how a big queue turns every PR red.
    if not args.dry_run:
        for repo in sorted(need):
            cancel_dead_queued(repo)

    # 2. over the cap: put idle runners to sleep, keeping the repos with the most waiting work and
    #    the primary runner of any repo whose main CI is queued.
    if awake > args.max:
        candidates = []
        for repo, units in active_now.items():
            q, r, qm = need.get(repo, (0, 0, 0))
            if not q and not r:
                continue
            keeper = keep_first(repo)
            for unit in units:
                if runner_busy(repo, unit):
                    continue                      # a runner with a job in flight stays
                if unit == keeper and qm:
                    continue                      # the primary of a repo whose MAIN run waits stays
                # a repo whose MAIN run waits keeps its runner longest; among the rest the
                # shortest queue gives up first
                candidates.append((1 if qm else 0, q, repo, unit))
        candidates.sort()
        for _qm, _q, repo, unit in candidates:
            if awake <= args.max:
                break
            if (repo, unit) in stopped:
                continue
            stopped.append((repo, unit))
            awake -= 1

    # 4. the box is still oversubscribed because every runner that finishes picks up the next
    #    queued job at once: eleven jobs on four (stolen) cores means every e2e suite times out.
    #    Shed load deliberately: stop a runner of a repo with a deep queue (its job is the least
    #    urgent one to finish, and its owner can re-run it), at most a couple per pass so the queue
    #    keeps moving. A runner that is the only one for a repo whose MAIN run waits is never taken.
    if awake > args.max:
        shed = []
        for repo, units in active_now.items():
            q, r, qm = need.get(repo, (0, 0, 0))
            if not q and not r:
                continue
            keeper = keep_first(repo)
            for unit in units:
                if unit == keeper and qm:
                    continue      # never shed a runner that is working on a repo's MAIN check:
                                # a cancelled main check is a red main, which is what the board measures
                if runner_busy(repo, unit):
                    continue      # never cancel a job in flight
                shed.append((q, repo, unit))
        # the repo with the SHORTEST queue gives up its runner first: a repo with a deep queue
        # needs its runners, a repo with one waiting job can wait its turn.
        shed.sort()
        for _q, repo, unit in shed[:2]:
            if awake <= args.max:
                break
            if (repo, unit) in stopped:
                continue
            stopped.append((repo, unit))
            awake -= 1

    # 3. wake a runner for a repo that is waiting. A repo's PRIMARY runner is always woken when its
    #    repo has queued work: sleeping it is what made PR #98's CI wait for a human (the unit was
    #    stopped, so Restart=always could not bring it back). The cap only limits the EXTRA runners.
    for repo in wanted:
        primary = RUNNERS.get(repo)
        units = ([primary] if primary else []) + EXTRA.get(repo, [])
        for unit in units:
            if unit in active_now.get(repo, []):
                continue
            if unit != primary and awake >= args.max:
                break
            started.append((repo, unit))
            awake += 1

    if args.dry_run:
        for repo in sorted(need):
            q, r, qm = need[repo]
            print("%-22s queued=%-3d running=%-3d main=%-3d %s" % (repo, q, r, qm, ",".join(active_now.get(repo, [])) or "-"))
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
    # restart it so it re-registers and picks the work up. One restart is enough - a runner that
    # is offline because the box is overloaded reconnects on its own, and restarting every pass
    # would kill the job it is working on.
    now = int(datetime.datetime.now().timestamp())
    state = load_state()
    for repo in wanted:
        q, r, _qm = need[repo]
        if not q:
            continue
        unit = RUNNERS.get(repo)
        if not unit or unit in [u for _, u in started] or unit in [u for _, u in stopped]:
            continue
        if runner_status(repo, unit) != "offline":
            continue
        last = state.get(unit, 0)
        if now - last < RESTART_COOLDOWN:
            continue
        state[unit] = now
        set_unit(unit, "restart")
        print("restart %s (%s: GitHub says offline while %d runs wait)" % (unit, repo, q))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
