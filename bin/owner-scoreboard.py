#!/usr/bin/env python3
"""Per-repo scoreboard and owner log for the Co-pilot's managed repos.

Reads Watchtower's cached GitHub snapshot (no GitHub calls of its own) and, for
each managed repo, says whether it is at target:
  - default-branch CI green (or no CI),
  - at most TARGET_PRS open PRs,
  - no open PR idle for STALE_DAYS or more.
Each repo has an owner folder ~/.ccc-navigator/owners/<repo>/ (docs/owners.md):
scoreboard.json is rewritten on every run, log.md is only ever appended to, one
line per entry `- YYYY-MM-DD HH:MM [actor] text`, so it is the repo's running record.
The old single file owners/<repo>.md is migrated once: its "## Log" lines move into
log.md (tagged [supervisor]) and the file is renamed to <repo>.md.migrated.

  owner-scoreboard.py                              table, worst repo first; refreshes scoreboard.json
  owner-scoreboard.py --json                       same as JSON
  owner-scoreboard.py --log REPO [--actor A] TEXT  append one line to REPO's log.md (actor: supervisor)

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


def owner_dir(repo):
    return os.path.join(OWNER_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", repo).lstrip(".") or "_")


def log_line(actor, text, when=None):
    return "- %s [%s] %s" % ((when or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M"), actor, " ".join(text.split()))


def migrate(repo):
    """Move the "## Log" lines of the old owners/<repo>.md into owners/<repo>/log.md, once."""
    old = os.path.join(OWNER_DIR, os.path.basename(owner_dir(repo)) + ".md")
    if not os.path.exists(old):
        return False
    with open(old) as f:
        text = f.read()
    i = text.find(LOG_HEAD)
    lines = []
    for ln in (text[i + len(LOG_HEAD):] if i >= 0 else "").splitlines():
        m = re.match(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) (.*)$", ln)
        if m:   # old lines have no actor: they were written by the co-pilot, the supervisor
            rest = m.group(2) if re.match(r"^\[[a-z0-9._-]+\] ", m.group(2)) else "[supervisor] " + m.group(2)
            lines.append("- %s %s" % (m.group(1), rest))
    d = owner_dir(repo)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "log.md")
    try:
        with open(path) as f:
            current = f.read()
    except OSError:
        current = ""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:      # old lines first: they are older than anything already in log.md
        f.write("".join(ln + "\n" for ln in lines) + current)
    os.replace(tmp, path)
    os.replace(old, old + ".migrated")
    return True


def write_owner(s, fetched):
    migrate(s["repo"])
    d = owner_dir(s["repo"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "scoreboard.json")
    out = dict(s, fetched=fetched or None,
               target={"max_open_prs": TARGET_PRS, "stale_days": STALE_DAYS, "main_ci": "green"})
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, path)
    return path


def append_log(repo, text, actor="supervisor"):
    migrate(repo)
    d = owner_dir(repo)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "log.md")
    with open(path, "a") as f:
        f.write(log_line(actor, text) + "\n")
    return path


def main(argv):
    if argv[:1] == ["--log"]:
        rest, actor = argv[1:], "supervisor"
        if "--actor" in rest:
            i = rest.index("--actor")
            actor, rest = (rest[i + 1] if i + 1 < len(rest) else ""), rest[:i] + rest[i + 2:]
        if len(rest) < 2 or not re.match(r"^[a-z0-9][a-z0-9._-]{0,39}$", actor):
            print("usage: owner-scoreboard.py --log REPO [--actor A] TEXT", file=sys.stderr)
            return 2
        print(append_log(rest[0], " ".join(rest[1:]), actor))
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
