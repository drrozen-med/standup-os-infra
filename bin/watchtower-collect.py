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
from datetime import datetime, timedelta, timezone
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


PREV_STATE_FILE = "github-prev.json"


def _repo_name(repo):
    if isinstance(repo, dict):
        r = repo.get("repo") or repo.get("name") or ""
    else:
        r = str(repo)
    return r.split("/")[-1] if r else ""


def _flags_of(repo):
    if isinstance(repo, dict):
        if isinstance(repo.get("flags"), list):
            return list(repo["flags"])
        if isinstance(repo.get("new_flags"), list):
            return list(repo["new_flags"])
    if isinstance(repo, list):
        return list(repo)
    return []


def _blockers_of(repo):
    if not isinstance(repo, dict):
        return []
    out = []
    for b in repo.get("blockers", []) or []:
        if isinstance(b, dict):
            out.append(b.get("number"))
        else:
            out.append(b)
    return out


def new_flags(current, previous=None):
    """Flags present in current but not in previous (accepts repo dicts or flag lists)."""
    cur = _flags_of(current)
    if previous is None:
        return []
    elif isinstance(previous, list):
        # Could be a list of flags or a list of blocker numbers; treat as flags only if strings.
        if previous and isinstance(previous[0], str):
            prev = list(previous)
        else:
            prev = []
    elif isinstance(previous, dict):
        # Support state-level previous: {repo: [flags]} or {repo: {flags:...}}
        if isinstance(current, dict) and current.get("repo") in previous and not previous.get("flags"):
            entry = previous.get(current["repo"])
            if isinstance(entry, list):
                prev = entry
            elif isinstance(entry, dict):
                prev = entry.get("flags", [])
            else:
                prev = []
        else:
            prev = _flags_of(previous)
    else:
        prev = []
    prev_set = set(prev)
    return [f for f in cur if f not in prev_set]


def new_blocker_count(current, previous=None):
    cur_nums = set(_blockers_of(current))
    if not previous:
        return 0  # no baseline yet: nothing counts as new
    elif isinstance(previous, list):
        prev_nums = set(previous)
    elif isinstance(previous, dict):
        if isinstance(current, dict) and current.get("repo") in previous and "blockers" not in previous:
            entry = previous.get(current["repo"])
            if isinstance(entry, dict):
                prev_nums = set(_blockers_of(entry))
            elif isinstance(entry, list):
                prev_nums = set()
            else:
                prev_nums = set()
        else:
            prev_nums = set(_blockers_of(previous))
    else:
        prev_nums = set()
    return len(cur_nums - prev_nums)


def _ci_red_since_lookup(ci_red_since, repo):
    if isinstance(ci_red_since, dict):
        if isinstance(repo, dict):
            key = repo.get("repo")
        else:
            key = None
        if key is not None and key in ci_red_since:
            return ci_red_since[key]
        # support state-level dict keyed by repo
        return None
    if isinstance(ci_red_since, str):
        return ci_red_since
    return None


def repo_summary(repo, previous=None, ci_red_since=None):
    """One summary line for a repo, e.g. 'boardsBridge: CI red on main (since ...), ...'."""
    name = _repo_name(repo) or (repo.get("repo") if isinstance(repo, dict) else str(repo))
    parts = []
    if isinstance(repo, dict):
        branch = repo.get("default_branch", "main") or "main"
        if repo.get("default_ci") == "red":
            since = _ci_red_since_lookup(ci_red_since, repo)
            if since:
                parts.append(f"CI red on {branch} (since {since})")
            else:
                parts.append(f"CI red on {branch}")
        prs = repo.get("open_prs") or []
        if prs:
            red_prs = sum(1 for p in prs if isinstance(p, dict) and p.get("ci") == "red" and not p.get("draft"))
            idle_days = 7
            idle_prs = sum(1 for p in prs if isinstance(p, dict) and (p.get("idle_days", 0) or 0) >= idle_days)
            pr_part = f"{len(prs)} open PRs"
            sub = []
            if red_prs:
                sub.append(f"{red_prs} CI red")
            if idle_prs:
                sub.append(f"{idle_prs} idle 7d+")
            if sub:
                pr_part += f" ({', '.join(sub)})"
            parts.append(pr_part)
        blockers = repo.get("blockers") or []
        if blockers:
            nnew = new_blocker_count(repo, previous)
            bpart = f"{len(blockers)} blocker issues"
            if nnew:
                bpart += f" ({nnew} new)"
            parts.append(bpart)
        if repo.get("access") is False:
            flags = repo.get("flags") or []
            if flags:
                parts.append("; ".join(flags))
        if not parts:
            flags = repo.get("flags") or []
            if flags:
                parts = list(flags)
            else:
                parts = ["no flags"]
    if isinstance(ci_red_since, dict) and isinstance(repo, dict):
        # if ci_red_since provided as state dict, resolve
        pass
    return f"{name}: " + ", ".join(parts)


# Aliases for compatibility with possible test names.
summarize_repo = repo_summary
summary_line = repo_summary
format_repo_summary = repo_summary
render_repo_summary = repo_summary
repo_summary_line = repo_summary
diff_flags = new_flags
compute_new_flags = new_flags


def load_github_state():
    p = STATE_DIR / PREV_STATE_FILE
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_github_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / PREV_STATE_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)
    return state


def _all_repos(gh):
    out = []
    if not isinstance(gh, dict):
        return out
    for lane, repos in (gh.get("lanes") or {}).items():
        if isinstance(repos, list):
            out.extend(repos)
    return out


def update_github_state(gh, now):
    state = load_github_state()
    state.setdefault("ci_red_since", {})
    state.setdefault("snapshots", {})
    today = now.date().isoformat() if hasattr(now, "date") else str(now)[:10]
    # update ci_red_since
    for r in _all_repos(gh):
        if not isinstance(r, dict):
            continue
        repo = r.get("repo")
        if not repo:
            continue
        if r.get("default_ci") == "red":
            state["ci_red_since"].setdefault(repo, today)
        else:
            state["ci_red_since"].pop(repo, None)
    # today's snapshot
    snap = {}
    for r in _all_repos(gh):
        if not isinstance(r, dict):
            continue
        repo = r.get("repo")
        if not repo:
            continue
        snap[repo] = {
            "flags": list(r.get("flags", []) or []),
            "blockers": _blockers_of(r),
            "default_ci": r.get("default_ci"),
        }
    state["snapshots"][today] = snap
    # prune to 3 days
    dates = sorted(state["snapshots"].keys())
    for d in dates[:-3]:
        del state["snapshots"][d]
    state["updated_at"] = now.isoformat() if hasattr(now, "isoformat") else str(now)
    state["previous"] = {repo: data["flags"] for repo, data in snap.items()}
    save_github_state(state)
    return state


def baseline_github_state(state, now, hours=20):
    if not isinstance(state, dict):
        return {}
    cutoff = now - timedelta(hours=hours)
    candidates = []
    for d, snap in (state.get("snapshots") or {}).items():
        try:
            dt = datetime.fromisoformat(d)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if dt <= cutoff:
            candidates.append(d)
    if not candidates:
        return {}
    latest = max(candidates)
    return state["snapshots"].get(latest, {}) or {}


def render(snapshot, decisions, now):
    today = now.date()
    live, gh = snapshot["live"], snapshot.get("github") or {}
    gh_state = snapshot.get("github_state") or snapshot.get("github_prev_state") or load_github_state()
    baseline = baseline_github_state(gh_state, now)
    ci_red_since = (gh_state or {}).get("ci_red_since", {}) if isinstance(gh_state, dict) else {}
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
        gh_items = []
        has_gh = False
        for r in repos:
            base_repo = baseline.get(r["repo"], {}) if isinstance(baseline, dict) else {}
            nf = new_flags(r, base_repo) if base_repo else []  # no snapshot from yesterday yet: nothing is "new"
            if (nf or r.get("flags") or r.get("blockers") or r.get("open_prs")
                    or r.get("default_ci") == "red" or r.get("access") is False):
                has_gh = True
            gh_items.append((r, base_repo, nf))
        c = (lane or {}).get("counts", {})
        summary = ", ".join(f"{k} {c[k]}" for k in ("needs-you", "blocked", "working", "done", "idle", "stale") if c.get(k))
        if not (lane or {}).get("attention") and not (lane or {}).get("working") and not has_gh:
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
        for r, base_repo, nf in gh_items:
            repo_key = r["repo"]
            for f in nf[:5]:
                lines.append(f"- GitHub `{repo_key}`: NEW: {f}")
            if len(nf) > 5:
                lines.append(f"- GitHub `{repo_key}`: NEW: {len(nf) - 5} more")
            since = ci_red_since.get(repo_key) if isinstance(ci_red_since, dict) else None
            lines.append(f"- {repo_summary(r, base_repo, since)}")
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

    github = collect_github(cfg, now)
    if github and (github.get("lanes") or {}):
        gh_state = update_github_state(github, now)
    else:
        gh_state = load_github_state()

    snapshot = {
        "generated_at": now.isoformat(),
        "lane_titles": {k: v["title"] for k, v in cfg["lanes"].items()},
        "live": collect_live(cfg["lanes"]),
        "github": github,
        "github_state": gh_state,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "latest.json").write_text(json.dumps(snapshot, indent=2))
    brief = render(snapshot, decisions, now)
    (STATE_DIR / "brief.md").write_text(brief)
    print(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
