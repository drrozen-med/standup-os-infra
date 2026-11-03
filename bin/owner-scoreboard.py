#!/usr/bin/env python3
"""Per-repo scoreboard and owner log for the Co-pilot's managed repos.

Reads Watchtower's cached GitHub snapshot (no GitHub calls of its own) and, for
each managed repo, says whether it is at target:
  - default-branch CI green (or no CI),
  - at most TARGET_PRS open PRs,
  - no open PR idle for STALE_DAYS or more.
Each repo gets an owner file ~/.ccc-navigator/owners/<repo>.md: a scoreboard
block that is rewritten on every run, and a "## Log" section below it that is
only ever appended to (by `--log`), so it is the repo's running record.

  owner-scoreboard.py                 table, worst repo first; refreshes owner files
  owner-scoreboard.py --json          same as JSON
  owner-scoreboard.py --log REPO TEXT append one dated line to REPO's log

Env OWNER_DIR / WATCHTOWER_GITHUB_JSON override the paths (for tests).
"""
import datetime
import json
import os
import re
import sys

MANAGED = ["boardsBridge", "cdl-bridge", "airline", "shlav-bet-family",
           "shared-marketing", "sleepySounds_love", "vera", "private_clinic"]
OWNER = "drrozen-med"
TARGET_PRS = 5
STALE_DAYS = 7
OWNER_DIR = os.environ.get("OWNER_DIR", os.path.expanduser("~/.ccc-navigator/owners"))
GITHUB_JSON = os.environ.get("WATCHTOWER_GITHUB_JSON", os.path.expanduser("~/.ccc-navigator/watchtower/github.json"))
LOG_HEAD = "## Log"


def load_repos():
    try:
        with open(GITHUB_JSON) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, ""
    repos = {}
    for rows in (data.get("lanes") or {}).values():
        for r in rows:
            repos[r.get("repo", "").split("/")[-1]] = r
    when = data.get("fetched") or data.get("generated_at") or ""
    if isinstance(when, (int, float)):
        when = datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
    return repos, when


def score(name, r):
    if not r:
        return {"repo": name, "known": False, "at_target": False, "problems": ["not in the Watchtower snapshot"]}
    prs = r.get("open_prs") or []
    more = 0
    blockers = 0
    for f in r.get("flags") or []:
        m = re.match(r"(\d+) more open PRs", f)
        if m:
            more = int(m.group(1))
        m = re.match(r"(\d+) open blocker issue", f)
        if m:
            blockers = int(m.group(1))
    total = len(prs) + more
    stale = [p["number"] for p in prs if (p.get("idle_days") or 0) >= STALE_DAYS]
    red = [p["number"] for p in prs if p.get("ci") == "red"]
    ci = r.get("default_ci") or "none"
    problems = []
    if ci == "red":
        problems.append("CI red on %s" % (r.get("default_branch") or "main"))
    if total > TARGET_PRS:
        problems.append("%d open PRs (target %d)" % (total, TARGET_PRS))
    if stale:
        problems.append("%d PRs idle %dd+" % (len(stale), STALE_DAYS))
    return {"repo": name, "known": True, "at_target": not problems, "problems": problems,
            "default_ci": ci, "open_prs": total, "stale_prs": stale, "red_prs": red,
            "blocker_issues": blockers}


def badness(s):
    # worst first: unknown, red main, then most PR overflow + stale
    if not s["known"]:
        return (3, 0)
    return (2 if s["default_ci"] == "red" else 1 if not s["at_target"] else 0,
            s["open_prs"] + len(s["stale_prs"]))


def board():
    repos, fetched = load_repos()
    rows = sorted((score(n, repos.get(n)) for n in MANAGED), key=badness, reverse=True)
    return {"fetched": fetched, "target": {"max_open_prs": TARGET_PRS, "stale_days": STALE_DAYS, "main_ci": "green"},
            "at_target": sum(1 for s in rows if s["at_target"]), "repos": rows}


def owner_path(repo):
    return os.path.join(OWNER_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", repo) + ".md")


def read_log(path):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return LOG_HEAD + "\n"
    i = text.find(LOG_HEAD)
    return text[i:] if i >= 0 else LOG_HEAD + "\n"


def write_owner(s, fetched):
    os.makedirs(OWNER_DIR, exist_ok=True)
    path = owner_path(s["repo"])
    head = ["# Owner: %s" % s["repo"], "",
            "Goal: main CI green, at most %d open PRs, none idle %d days or more." % (TARGET_PRS, STALE_DAYS),
            "Scoreboard (GitHub as of %s): %s" % (fetched or "unknown", "AT TARGET" if s["at_target"] else "; ".join(s["problems"]))]
    if s["known"]:
        head.append("Open PRs %d, idle %s, CI red %s, blocker issues %d." % (
            s["open_prs"], ", ".join("#%d" % n for n in s["stale_prs"]) or "none",
            ", ".join("#%d" % n for n in s["red_prs"]) or "none", s["blocker_issues"]))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(head) + "\n\n" + read_log(path))
    os.replace(tmp, path)


def append_log(repo, text):
    os.makedirs(OWNER_DIR, exist_ok=True)
    path = owner_path(repo)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("# Owner: %s\n\n%s\n" % (repo, LOG_HEAD))
    line = " ".join(text.split())
    with open(path, "a") as f:
        f.write("- %s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), line))
    return path


def main(argv):
    if argv[:1] == ["--log"]:
        if len(argv) < 3:
            print("usage: owner-scoreboard.py --log REPO TEXT", file=sys.stderr)
            return 2
        print(append_log(argv[1], " ".join(argv[2:])))
        return 0
    b = board()
    for s in b["repos"]:
        write_owner(s, b["fetched"])
    if "--json" in argv:
        print(json.dumps(b, indent=1))
        return 0
    print("%d of %d repos at target (GitHub as of %s)" % (b["at_target"], len(b["repos"]), b["fetched"] or "unknown"))
    for s in b["repos"]:
        print("%-18s %s" % (s["repo"], "at target" if s["at_target"] else "; ".join(s["problems"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
