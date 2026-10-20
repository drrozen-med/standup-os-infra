#!/usr/bin/env python3
"""watchtower-collect.py - read-only portfolio snapshot.

Reads watchtower/lanes.json and watchtower/decisions.json, queries the GitHub
REST API for every repo (open PRs, CI on default branch and PR heads, blocker
issues), and writes:

  $WATCHTOWER_STATE_DIR/latest.json   machine-readable snapshot
  $WATCHTOWER_STATE_DIR/brief.md      one-page brief, open decisions first

Never writes to GitHub. A repo the token cannot see is reported as
"no access", not skipped silently.

Env:
  GITHUB_TOKEN           read-only token (fine-grained: Contents, Pull requests,
                         Issues, Checks, Commit statuses = read). Optional for
                         public repos.
  WATCHTOWER_CONFIG_DIR  default: <repo>/watchtower
  WATCHTOWER_STATE_DIR   default: $STANDUP_OS_ROOT/watchtower-state or ./state
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
MAX_PR_HEADS = 20

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("WATCHTOWER_CONFIG_DIR", ROOT / "watchtower"))
STATE_DIR = Path(
    os.environ.get(
        "WATCHTOWER_STATE_DIR",
        Path(os.environ["STANDUP_OS_ROOT"]) / "watchtower-state"
        if "STANDUP_OS_ROOT" in os.environ
        else ROOT / "state",
    )
)
TOKEN = os.environ.get("GITHUB_TOKEN", "")


class NoAccess(Exception):
    pass


def api(path):
    req = urllib.request.Request(API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "standup-os-watchtower")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise NoAccess(f"HTTP {e.code}") from e
        raise


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ci_state(repo, sha):
    """Return green | red | pending | none for a commit."""
    try:
        runs = api(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
        status = api(f"/repos/{repo}/commits/{sha}/status")
    except NoAccess:
        return "unknown"
    bad = {"failure", "timed_out", "cancelled", "action_required"}
    if any(r["conclusion"] in bad for r in runs) or status["state"] in ("failure", "error"):
        return "red"
    if any(r["status"] != "completed" for r in runs) or (status["state"] == "pending" and status["statuses"]):
        return "pending"
    if runs or status["statuses"]:
        return "green"
    return "none"


def collect_repo(owner, name, cfg, now):
    repo = f"{owner}/{name}"
    out = {"repo": repo, "access": True, "flags": []}
    try:
        meta = api(f"/repos/{repo}")
    except NoAccess as e:
        out.update(access=False, flags=[f"no access ({e}); private repo or rate-limited: set GITHUB_TOKEN (read-only) or install the Claude GitHub App"])
        return out
    branch = meta["default_branch"]
    out["default_branch"] = branch
    out["pushed_at"] = meta["pushed_at"]

    head = api(f"/repos/{repo}/commits/{branch}")["sha"]
    out["default_ci"] = ci_state(repo, head)
    if out["default_ci"] == "red":
        out["flags"].append(f"CI red on {branch}")

    prs = api(f"/repos/{repo}/pulls?state=open&per_page=50")
    out["open_prs"] = []
    for pr in prs[:MAX_PR_HEADS]:
        age = (now - parse_ts(pr["updated_at"])).days
        state = ci_state(repo, pr["head"]["sha"])
        item = {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["user"]["login"],
            "draft": pr["draft"],
            "idle_days": age,
            "ci": state,
        }
        out["open_prs"].append(item)
        if state == "red" and not pr["draft"]:
            out["flags"].append(f"PR #{pr['number']} CI red")
        if age >= cfg["stale_pr_days"]:
            out["flags"].append(f"PR #{pr['number']} idle {age}d")
    if len(prs) > MAX_PR_HEADS:
        out["flags"].append(f"{len(prs) - MAX_PR_HEADS} more open PRs not checked")

    out["blockers"] = []
    for label in cfg["blocker_labels"]:
        issues = api(f"/repos/{repo}/issues?state=open&labels={label}&per_page=100")
        for i in issues:
            if "pull_request" not in i:
                out["blockers"].append({"number": i["number"], "title": i["title"], "label": label})
    if out["blockers"]:
        out["flags"].append(f"{len(out['blockers'])} open blocker issue(s)")
    return out


def render(snapshot, decisions, now):
    today = now.date()
    lines = [f"# Watchtower brief - {now:%Y-%m-%d %H:%MZ}", ""]

    open_dec = [d for d in decisions if d.get("status") == "open"]

    def sort_key(d):
        return (d["priority"], d["deadline"] or "9999")

    open_dec.sort(key=sort_key)
    lines.append(f"## Decisions waiting on you ({len(open_dec)})")
    lines.append("")
    for d in open_dec:
        due = ""
        if d["deadline"]:
            days = (datetime.fromisoformat(d["deadline"]).date() - today).days
            due = f" **[due {d['deadline']}, {'OVERDUE' if days < 0 else f'{days}d left'}]**"
        lines.append(f"- **P{d['priority']}** `{d['id']}` ({d['lane']}){due}: {d['question']} Options: {' / '.join(d['options'])}")
    lines.append("")

    for lane_id, lane in snapshot["lanes"].items():
        lines.append(f"## {lane['title']}")
        lines.append("")
        for r in lane["repos"]:
            if not r["access"]:
                lines.append(f"- `{r['repo']}`: NO ACCESS - {r['flags'][0]}")
                continue
            prs = r["open_prs"]
            lines.append(
                f"- `{r['repo']}`: default CI **{r['default_ci']}**, "
                f"{len(prs)} open PR(s), {len(r['blockers'])} blocker issue(s)"
            )
            for f in r["flags"]:
                lines.append(f"  - FLAG: {f}")
        lines.append("")
    return "\n".join(lines)


def main():
    cfg = json.loads((CONFIG_DIR / "lanes.json").read_text())
    decisions = json.loads((CONFIG_DIR / "decisions.json").read_text())
    now = datetime.now(timezone.utc)

    snapshot = {"generated_at": now.isoformat(), "lanes": {}}
    for lane_id, lane in cfg["lanes"].items():
        repos = [collect_repo(cfg["owner"], n, cfg, now) for n in lane["repos"]]
        snapshot["lanes"][lane_id] = {"title": lane["title"], "repos": repos}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "latest.json").write_text(json.dumps(snapshot, indent=2))
    brief = render(snapshot, decisions, now)
    (STATE_DIR / "brief.md").write_text(brief)
    print(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
