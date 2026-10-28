#!/usr/bin/env python3
"""watchtower-collect.py - read-only portfolio snapshot.

Two layers, live first:

  1. Live: what every agent session is doing right now, read from the local
     CCC Navigator (status labels kept fresh by its triage worker: working,
     needs-you, blocked, done, plus your notes). Seconds old.
  2. GitHub: CI on the default branch, open PRs, blocker issues. Slow and
     rate-limited, so it is refreshed at most every WATCHTOWER_GITHUB_EVERY
     seconds (default 3600) and reused from the cache in between.

Sessions are mapped to lanes by project name (`repos` plus `match` substrings
in lanes.json); anything unmatched lands under "Other".

Writes:

  $WATCHTOWER_STATE_DIR/latest.json   machine-readable snapshot
  $WATCHTOWER_STATE_DIR/brief.md      one-page brief, open decisions first

Never writes to GitHub or to any session. A repo the token cannot see is
reported as "no access", not skipped silently. Nothing leaves the machine
except the read-only GitHub calls.

Env:
  GITHUB_TOKEN           read-only token (fine-grained: Contents, Pull requests,
                         Issues, Checks, Commit statuses = read). Optional for
                         public repos.
  WATCHTOWER_CONFIG_DIR  default: <repo>/watchtower
  WATCHTOWER_STATE_DIR   default: $STANDUP_OS_ROOT/watchtower-state or ./state
  WATCHTOWER_NAV_URL     default: http://127.0.0.1:8095 (the navigator)
  WATCHTOWER_WINDOW      sessions active within: now | 1d | 7d (default 7d)
  WATCHTOWER_GITHUB      0 turns the GitHub layer off
  WATCHTOWER_GITHUB_EVERY  seconds between GitHub refreshes (default 3600)

Without GITHUB_TOKEN the GitHub layer uses the `gh` CLI if it is signed in.
"""
import json
import os
import shutil
import subprocess
import sys
import time
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
NAV_URL = os.environ.get("WATCHTOWER_NAV_URL", "http://127.0.0.1:8095").rstrip("/")
WINDOW = os.environ.get("WATCHTOWER_WINDOW", "7d")
GITHUB_ON = os.environ.get("WATCHTOWER_GITHUB", "1") != "0"
GITHUB_EVERY = int(os.environ.get("WATCHTOWER_GITHUB_EVERY", "3600"))
GH = shutil.which("gh") if not TOKEN else None
ATTENTION = ("needs-you", "blocked")


class NoAccess(Exception):
    pass


def api(path):
    if GH:
        r = subprocess.run([GH, "api", path], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise NoAccess((r.stderr.strip().splitlines() or ["gh api failed"])[-1][:120])
        return json.loads(r.stdout)
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


def lane_for(project, lanes):
    name = project.lower()
    for lane_id, lane in lanes.items():
        if name in (r.lower() for r in lane["repos"]) or any(m.lower() in name for m in lane.get("match", [])):
            return lane_id
    return "other"


def collect_live(lanes):
    """Every visible session from the navigator, with its live label, by lane."""
    url = f"{NAV_URL}/api/tree?window={WINDOW}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            tree = json.load(resp)
    except Exception as e:  # navigator down: say so, keep the rest of the brief
        return {"ok": False, "error": f"navigator unreachable at {NAV_URL}: {e}", "lanes": {}}
    out = {}
    for repo in tree.get("repos") or []:
        project = repo.get("name") or repo.get("key") or "?"
        for group in repo.get("groups") or []:
            for it in group.get("items") or []:
                if it.get("hidden"):
                    continue
                if it.get("live"):
                    label = "working"
                elif it.get("needs_you"):
                    label = "needs-you"
                else:
                    label = it.get("triage") or "idle"
                lane = out.setdefault(lane_for(project, lanes), {"counts": {}, "attention": [], "working": []})
                lane["counts"][label] = lane["counts"].get(label, 0) + 1
                row = {"key": it.get("key"), "project": project, "group": group.get("name") or "",
                       "title": it.get("nick") or it.get("original") or "", "engine": it.get("engine"),
                       "why": it.get("triage_reason") or "", "note": it.get("last_note") or "",
                       "marked_by_you": it.get("triage_by") == "you", "mtime": it.get("mtime")}
                if label in ATTENTION:
                    lane["attention"].append(dict(row, label=label))
                elif label == "working":
                    lane["working"].append(row)
    for lane in out.values():
        lane["attention"].sort(key=lambda r: (ATTENTION.index(r["label"]), -(r["mtime"] or 0)))
    return {"ok": True, "window": WINDOW, "lanes": out}


def _safe_repo(owner, name, cfg, now):
    try:
        return collect_repo(owner, name, cfg, now)
    except NoAccess as e:  # e.g. an empty repo (HTTP 409): report it, keep going
        return {"repo": f"{owner}/{name}", "access": True, "flags": [f"unreadable: {e}"]}


def collect_github(cfg, now):
    """GitHub layer, from cache unless it is older than GITHUB_EVERY."""
    cache = STATE_DIR / "github.json"
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = None
    if not GITHUB_ON:
        return cached
    if cached and time.time() - cached.get("fetched", 0) < GITHUB_EVERY:
        return cached
    fresh = {"fetched": time.time(), "generated_at": now.isoformat(), "lanes": {}}
    try:
        for lane_id, lane in cfg["lanes"].items():
            fresh["lanes"][lane_id] = [_safe_repo(cfg["owner"], n, cfg, now) for n in lane["repos"]]
    except Exception as e:  # network trouble: keep the last good copy
        if cached:
            cached["stale_error"] = str(e)[:200]
            return cached
        return {"fetched": 0, "error": str(e)[:200], "lanes": {}}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(fresh, indent=2))
    return fresh


def _ago(ts):
    if not ts:
        return ""
    mins = int((time.time() - ts) // 60)
    return f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago" if mins < 2880 else f"{mins // 1440}d ago"


def render(snapshot, decisions, now):
    today = now.date()
    live, gh = snapshot["live"], snapshot.get("github") or {}
    titles = dict(snapshot["lane_titles"], other="Other projects")
    tot = {}
    for lane in live["lanes"].values():
        for k, v in lane["counts"].items():
            tot[k] = tot.get(k, 0) + v
    lines = [f"# Watchtower - {now.astimezone():%Y-%m-%d %H:%M}", ""]
    if live["ok"]:
        lines.append(f"**Right now:** {tot.get('needs-you', 0)} need you, {tot.get('blocked', 0)} blocked, "
                     f"{tot.get('working', 0)} working, {tot.get('done', 0)} done "
                     f"(sessions active in the last {live['window']}).")
    else:
        lines.append(f"**Live sessions unavailable:** {live['error']}")
    lines.append("")

    open_dec = [d for d in decisions if d.get("status") == "open"]
    open_dec.sort(key=lambda d: (d["priority"], d["deadline"] or "9999"))
    lines.append(f"## Decisions waiting on you ({len(open_dec)})")
    lines.append("")
    for d in open_dec:
        due = ""
        if d["deadline"]:
            days = (datetime.fromisoformat(d["deadline"]).date() - today).days
            due = f" **[due {d['deadline']}, {'OVERDUE' if days < 0 else f'{days}d left'}]**"
        lines.append(f"- **P{d['priority']}** `{d['id']}` ({d['lane']}){due}: {d['question']} Options: {' / '.join(d['options'])}")
    lines.append("")

    order = list(snapshot["lane_titles"]) + ["other"]
    quiet = []
    for lane_id in order:
        lane = live["lanes"].get(lane_id)
        repos = (gh.get("lanes") or {}).get(lane_id) or []
        gh_flags = [(r["repo"], f) for r in repos for f in r["flags"]]
        c = (lane or {}).get("counts", {})
        summary = ", ".join(f"{k} {c[k]}" for k in ("needs-you", "blocked", "working", "done", "idle", "stale") if c.get(k))
        if not (lane or {}).get("attention") and not (lane or {}).get("working") and not gh_flags:
            quiet.append(titles.get(lane_id, lane_id) + (f" ({summary})" if summary else ""))
            continue
        lines.append(f"## {titles.get(lane_id, lane_id)}" + (f"  ({summary})" if summary else ""))
        lines.append("")
        for r in (lane or {}).get("attention", []):
            who = " (marked by you)" if r["marked_by_you"] else ""
            lines.append(f"- **{r['label'].upper()}**{who} {r['title']} [{r['project']}, {r['engine']}, {_ago(r['mtime'])}]")
            if r["why"]:
                lines.append(f"  - {r['why']}")
            if r["note"]:
                lines.append(f"  - note: {r['note']}")
        for r in (lane or {}).get("working", []):
            lines.append(f"- working: {r['title']} [{r['project']}, {r['engine']}]")
        for repo, f in gh_flags:
            lines.append(f"- GitHub `{repo}`: {f}")
        lines.append("")

    if quiet:
        lines.append("**Quiet:** " + "; ".join(quiet))
        lines.append("")
    if gh.get("fetched"):
        note = f"GitHub layer from {_ago(gh['fetched'])}"
        if gh.get("stale_error"):
            note += f" (last refresh failed: {gh['stale_error']})"
        lines.append(f"_{note}._")
    elif GITHUB_ON:
        lines.append(f"_GitHub layer unavailable: {gh.get('error', 'not fetched yet')}._")
    return "\n".join(lines)


def load_decisions():
    """Open decisions from the navigator's inbox (Watchtower's queue minus what Uri answered, plus questions
    agents asked with `nav ask`). Falls back to the local file when the navigator is down."""
    try:
        with urllib.request.urlopen(f"{NAV_URL}/api/decisions", timeout=20) as resp:
            return json.loads(resp.read())["open"]
    except Exception:
        return json.loads((CONFIG_DIR / "decisions.json").read_text())


def main():
    cfg = json.loads((CONFIG_DIR / "lanes.json").read_text())
    decisions = load_decisions()
    now = datetime.now(timezone.utc)

    snapshot = {
        "generated_at": now.isoformat(),
        "lane_titles": {k: v["title"] for k, v in cfg["lanes"].items()},
        "live": collect_live(cfg["lanes"]),
        "github": collect_github(cfg, now),
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "latest.json").write_text(json.dumps(snapshot, indent=2))
    brief = render(snapshot, decisions, now)
    (STATE_DIR / "brief.md").write_text(brief)
    print(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
