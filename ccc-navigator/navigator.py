#!/usr/bin/env python3
"""CCC Navigator: a Repo -> Group -> Session sidebar for Claude Command Center.

Reads sessions from a running Claude Command Center (all engines it ingests:
Claude, Codex, Devin, Cursor, ...), plus Pi sessions straight from disk, and
serves a small browser UI that groups them by *main* git repo (worktrees fold
into their parent), then by group, then session. Groups come from, in order:
your manual assignment, the Orca workspace the session ran in, else
"Ungrouped". Nicknames and groups live in this tool's own state file; CCC's
data is never modified.

    python3 navigator.py                    # http://127.0.0.1:8095, CCC on :8090
    python3 navigator.py --ccc http://localhost:9001 --port 8096

Stdlib only. Binds to 127.0.0.1.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import http.client
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orca

HERE = Path(__file__).resolve().parent
HOME = str(Path.home())
WINDOWS = {"now": 3600, "1d": 86400, "7d": 7 * 86400, "all": None}
UNGROUPED = ""
# Auto-resurface lifts a tombstone whose subject is live again. Kill switch in
# case it ever misfires: CCC_NAV_RESURFACE=0.
RESURFACE = os.environ.get("CCC_NAV_RESURFACE", "1").strip().lower() not in ("0", "false", "no", "off")
# Claude desktop throws away each scratch workspace in its own folder, named after
# the project it was opened for with the slashes flattened to dashes. None of them
# is a repo, so they all fold into this single pseudo-repo.
CLAUDE_SCRATCH_MARKER = "Application Support/Claude/scratch-workspaces"
CLAUDE_SCRATCH_KEY = "folder:claude-scratch"
CLAUDE_SCRATCH_NAME = "Claude scratch"


# ---------------------------------------------------------------- state

class State:
    """Nicknames, manual groups and assignments. One JSON file, atomic writes.

    {"nicknames": {key: name},
     "groups":    {repo_key: [group, ...]},
     "assign":    {key: {"repo": repo_key, "group": name}}}
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        return {
            "nicknames": dict(d.get("nicknames") or {}),
            "groups": {k: list(v) for k, v in (d.get("groups") or {}).items()},
            "assign": dict(d.get("assign") or {}),
            "hidden": dict(d.get("hidden") or {}),
            "pinned": dict(d.get("pinned") or {}),
            "icons": dict(d.get("icons") or {}),
            "repo_names": dict(d.get("repo_names") or {}),
        }

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # this file is the user's curation (hides, stars, names): keep the previous
        # version plus one snapshot per day, so a bad write is never the only copy
        try:
            if self.path.exists():
                bdir = self.path.parent / "backups"
                bdir.mkdir(exist_ok=True)
                shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
                snap = bdir / ("state-%s.json" % time.strftime("%Y%m%d"))
                if not snap.exists():
                    shutil.copy2(self.path, snap)
                    for old in sorted(bdir.glob("state-*.json"))[:-30]:
                        old.unlink()
        except OSError:
            pass
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set_nickname(self, key, name):
        with self.lock:
            name = (name or "").strip()[:120]
            if name:
                self.data["nicknames"][key] = name
            else:
                self.data["nicknames"].pop(key, None)
            self._save()

    def assign(self, key, repo, group):
        with self.lock:
            group = (group or "").strip()[:60]
            if group:
                self.data["assign"][key] = {"repo": repo, "group": group}
                groups = self.data["groups"].setdefault(repo, [])
                if group not in groups:
                    groups.append(group)
            else:
                self.data["assign"].pop(key, None)
            self._save()

    def set_repo_name(self, key, name):
        with self.lock:
            name = (name or "").strip()[:80]
            if name:
                self.data["repo_names"][key] = name
            else:
                self.data["repo_names"].pop(key, None)
            self._save()

    def set_icon(self, key, icon):
        with self.lock:
            icon = (icon or "").strip()[:8]
            if icon:
                self.data["icons"][key] = icon
            else:
                self.data["icons"].pop(key, None)
            self._save()

    def set_pinned(self, key, pinned):
        with self.lock:
            if pinned:
                self.data["pinned"][key] = True
            else:
                self.data["pinned"].pop(key, None)
            self._save()

    def set_hidden(self, keys, hidden):
        with self.lock:
            for key in keys:
                if hidden:
                    self.data["hidden"][key] = True
                else:
                    self.data["hidden"].pop(key, None)
            self._save()

    def add_group(self, repo, name):
        with self.lock:
            name = (name or "").strip()[:60]
            groups = self.data["groups"].setdefault(repo, [])
            if name and name not in groups:
                groups.append(name)
            self._save()

    def rename_group(self, repo, old, new):
        with self.lock:
            new = (new or "").strip()[:60]
            groups = self.data["groups"].setdefault(repo, [])
            if not new or old not in groups:
                return
            groups[groups.index(old)] = new
            if groups.count(new) > 1:
                groups.remove(new)
            for a in self.data["assign"].values():
                if a.get("repo") == repo and a.get("group") == old:
                    a["group"] = new
            self._save()

    def remove_group(self, repo, name):
        with self.lock:
            groups = self.data["groups"].setdefault(repo, [])
            if name in groups:
                groups.remove(name)
            for k in [k for k, a in self.data["assign"].items()
                      if a.get("repo") == repo and a.get("group") == name]:
                del self.data["assign"][k]
            self._save()


# ---------------------------------------------------------------- sources

class CCCSource:
    """GET /api/conversations?all=1 from a running CCC, cached briefly."""

    def __init__(self, base, ttl=30.0, cache_file="~/.ccc-navigator/ccc-cache.json"):
        self.base = base.rstrip("/").replace("//localhost", "//127.0.0.1")  # skip the ::1 attempt
        self.ttl = ttl
        self.cache_file = Path(cache_file).expanduser()
        self._lock = threading.Lock()
        self._at = 0.0
        self._good = None  # (rows, fetched_at) from the last successful fetch
        try:
            d = json.loads(self.cache_file.read_text())
            self._good = (d["rows"], d["at"])
        except Exception:
            pass
        self._busy = False
        self._val = ([], {"ok": False, "error": "not fetched yet"})

    def rows(self):
        """Never blocks a request on CCC: serve the last value, refresh in the background."""
        with self._lock:
            stale = time.time() - self._at >= self.ttl and not self._busy
            if stale:
                self._busy = True
        if stale:
            th = threading.Thread(target=self._refresh, daemon=True)
            th.start()
            if self._at == 0.0:  # first ever call: wait briefly so the first paint has data
                th.join(8)
        return self._val

    def _refresh(self):
        t0 = time.time()
        try:
            url = self.base + "/api/conversations?all=1&compact=1"
            with urllib.request.urlopen(url, timeout=6) as r:
                d = json.load(r)
            rows = d.get("conversations") or []
            meta = {"ok": True, "ms": round((time.time() - t0) * 1000), "count": len(rows)}
            self._good = (rows, time.time())
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(json.dumps({"rows": rows, "at": self._good[1]}))
            except OSError:
                pass
        except Exception as e:  # CCC down: serve the last good snapshot, then Pi/Orca data
            if self._good:
                rows = self._good[0]
                meta = {"ok": False, "stale": True, "age": round(time.time() - self._good[1]),
                        "count": len(rows), "error": str(e)}
            else:
                rows, meta = [], {"ok": False, "error": str(e)}
        with self._lock:
            # a failed fetch costs the connect timeout, so back off: retry after 2x ttl
            self._val, self._at = (rows, meta), time.time() + (0 if meta["ok"] else self.ttl)
            self._busy = False


_pi_cache: dict[str, tuple[tuple[float, int], dict | None]] = {}


def pi_sessions(pi_dir):
    """Pi sessions: <dir>/--<cwd>--/<ts>_<uuid>.jsonl (header has id + cwd;
    `session_info` entries carry the /name). Cached per file by (mtime, size)."""
    root = Path(pi_dir).expanduser()
    if not root.is_dir():
        return [], {"found": False, "dir": str(root)}
    out = []
    for f in root.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        sig = (st.st_mtime, st.st_size)
        hit = _pi_cache.get(str(f))
        if hit and hit[0] == sig:
            if hit[1]:
                out.append(hit[1])
            continue
        row = _read_pi(f, st)
        _pi_cache[str(f)] = (sig, row)
        if row:
            out.append(row)
    return out, {"found": True, "dir": str(root), "count": len(out)}


def _read_pi(f: Path, st):
    sid, cwd, name, first = f.stem, None, None, None
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("type")
                if t == "session":
                    sid, cwd = o.get("id") or sid, o.get("cwd") or cwd
                elif t == "session_info" and o.get("name"):
                    name = o["name"]
                elif t == "message" and first is None:
                    m = o.get("message") or {}
                    if m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, list):
                            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                        first = (c or "").strip()
    except OSError:
        return None
    if not cwd:
        cwd = "/" + f.parent.name.strip("-").replace("-", "/")  # lossy fallback
    return {
        "session_id": sid, "engine": "pi", "source": "pi",
        "session_cwd": cwd, "folder_path": cwd, "jsonl_path": str(f),
        "display_name": name, "name_overridden": bool(name),
        "first_message": first or "", "modified": st.st_mtime,
    }


# ---------------------------------------------------------------- repo resolution

_repo_cache: dict[str, tuple[float, tuple]] = {}
_REPO_TTL = 60.0


def resolve_repo(path):
    """Working folder -> (main_repo_path | None, method, worktree_name | None).

    method is "git" (read from .git / commondir, no subprocess), "heuristic"
    (folder gone: known worktree path patterns) or "none"."""
    if not path:
        return None, "none", None
    hit = _repo_cache.get(path)
    if hit and time.time() - hit[0] < _REPO_TTL:
        return hit[1]
    res = _resolve_git(Path(path)) or _resolve_pattern(path) or (None, "none", None)
    _repo_cache[path] = (time.time(), res)
    return res


def _resolve_git(p: Path):
    try:
        if not p.exists():
            return None
        top = g = None
        for d in [p, *p.parents]:
            if (d / ".git").exists():
                top, g = d, d / ".git"
                break
            if str(d) == HOME:
                break
        if g is None:
            return None
        if g.is_dir():
            return str(top), "git", None
        txt = g.read_text(errors="replace").strip()
        if not txt.startswith("gitdir:"):
            return None
        gitdir = Path(txt.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (top / gitdir).resolve()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
        main = common.parent if common.name == ".git" else common
        return str(main), "git", top.name
    except OSError:
        return None


def _resolve_pattern(path):
    for marker in ("/.claude/worktrees/", "/.worktrees/", "/worktrees/"):
        if marker in path:
            main, rest = path.split(marker, 1)
            return main, "heuristic", rest.split("/", 1)[0]
    head, base = os.path.split(path.rstrip("/"))
    if "-wt-" in base:
        main, wt = base.split("-wt-", 1)
        return os.path.join(head, main), "heuristic", wt
    return None


def claude_scratch_root(path):
    """Scratch-workspace root for a cwd, or None if it isn't one.

    A Claude desktop scratch workspace is
    <home>/Library/Application Support/Claude/scratch-workspaces/<dash-mangled
    name>, one folder per session and never a git repo. Return the root they
    share so callers can group them under a single name."""
    path = str(path or "")
    i = path.find(CLAUDE_SCRATCH_MARKER)
    return None if i < 0 else (path[:i] + CLAUDE_SCRATCH_MARKER).rstrip("/")


# ---------------------------------------------------------------- classification

_origin_cache: dict[str, str | None] = {}


def transcript_origin(row):
    """How a session was launched, from its transcript head.

    CCC's list API reports every Claude row as source=interactive, so read
    Claude's `entrypoint` (cli / sdk-cli / ...) or Codex's session_meta
    `source` (cli / exec / vscode). The head never changes: cache by path."""
    p = row.get("jsonl_path") or row.get("rollout_path")
    if not p:
        return None
    if p in _origin_cache:
        return _origin_cache[p]
    origin = None
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 8:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("entrypoint"):
                    origin = str(o["entrypoint"])
                    break
                if o.get("type") == "session_meta":
                    s = (o.get("payload") or {}).get("source")
                    origin = "codex-" + (s if isinstance(s, str) else "other")
                    break
    except OSError:
        return None
    _origin_cache[p] = origin
    return origin


def is_scratch(r):
    paths = f"{r.get('folder_path') or ''} {r.get('session_cwd') or ''}"
    return ("/command-center/scratch" in paths
            or "-claude-command-center-scratch" in str(r.get("slug") or "")
            or r.get("folder_label") == "scratch")


def is_headless(r, origin):
    src = str(r.get("source") or "").lower()
    o = str(origin or "").lower()
    return src in ("sdk-cli", "exec") or o.startswith("sdk") or o == "codex-exec"


def prompt_key(r):
    t = re.sub(r"\s+", " ", str(r.get("first_message") or "")).strip().lower()
    return t[:80] if len(t) >= 20 else ""


def mtime_of(r):
    v = float(r.get("modified") or r.get("mtime") or 0)
    return v / 1000 if v > 1e11 else v


def clean(text, n=90):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:n] + ("…" if len(t) > n else "")


def short(path):
    return path.replace(HOME, "~", 1) if path else ""


# ---------------------------------------------------------------- tree

def is_running(r):
    """CCC flags every resumable Devin CLI session as live (no process behind it).
    Trust the flag for other engines, a real pid, or recent activity."""
    if not r.get("is_live"):
        return False
    return (str(r.get("engine")) != "devin" or bool(r.get("spawn_pid"))
            or time.time() - mtime_of(r) < 600)


def build_tree(rows, state, window, orca_ws, ccc_base, batch_min=3, now=None, show_hidden=False):
    now = now or time.time()
    cutoff = now - WINDOWS[window] if WINDOWS.get(window) else 0
    inwin = [r for r in rows if mtime_of(r) >= cutoff or is_running(r)]

    hidden = collections.Counter()
    keep, origins = [], {}
    for r in inwin:
        if r.get("trashed") or r.get("archived"):
            hidden["Archived or trashed"] += 1
        elif is_scratch(r):
            hidden["CCC's own title-generation runs"] += 1
        else:
            keep.append(r)
            origins[id(r)] = transcript_origin(r)

    pk_count = collections.Counter(prompt_key(r) for r in keep if prompt_key(r))
    singles, batches = [], collections.defaultdict(list)
    for r in keep:
        pk = prompt_key(r)
        if pk and pk_count[pk] >= batch_min:
            batches[pk].append(r)
        elif is_headless(r, origins[id(r)]):
            hidden["Headless runs (claude -p, codex exec)"] += 1
        else:
            singles.append(r)

    nick = state["nicknames"]
    assign = state["assign"]
    method_ct = collections.Counter()
    unresolved = collections.Counter()
    repos = {}

    def place(r):
        cwd = r.get("session_cwd") or r.get("folder_path") or ""
        scratch = claude_scratch_root(cwd)
        ws = orca.workspace_for(orca_ws, cwd, r.get("git_branch") or r.get("branch") or "")
        main, method, wt = resolve_repo(cwd)
        if scratch:
            main, method, wt = CLAUDE_SCRATCH_KEY, "claude-scratch", None
        elif not main and ws and ws.get("repo_path"):
            main, method, wt = ws["repo_path"], "orca", Path(ws["path"]).name
        if not main and str(r.get("engine")) == "devin":
            label = r.get("folder_label") or r.get("folder_label_chip") or "Devin"
            main, method = "devin:" + label, "devin"
        method_ct[method] += 1
        if main == CLAUDE_SCRATCH_KEY:
            key, name, path = CLAUDE_SCRATCH_KEY, CLAUDE_SCRATCH_NAME, short(scratch)
        elif main:
            key, name, path = main, (main[6:] if main.startswith("devin:") else Path(main).name), short(main)
        else:
            unresolved[short(cwd) or "(no folder)"] += 1
            # last path component: CCC's folder_label is often a dash-mangled path
            label = Path(cwd).name or r.get("folder_label") or "unknown"
            key, name, path = "folder:" + label, label, short(cwd)
        repo = repos.setdefault(key, {"key": key, "name": state.get("repo_names", {}).get(key) or name,
                                      "orig_name": name, "path": path, "items": []})
        return repo, cwd, wt, ws

    def group_for(item_key, repo_key, ws):
        a = assign.get(item_key)
        if a and a.get("repo") == repo_key and a.get("group"):
            return a["group"], "manual"
        if ws:
            return ws["name"], "orca"
        return UNGROUPED, "none"

    def base_item(r, item_key, repo, cwd, wt, ws):
        group, gsrc = group_for(item_key, repo["key"], ws)
        sid = str(r.get("session_id") or "")
        engine = str(r.get("engine") or "claude")
        original = (r.get("display_name") if r.get("name_overridden") else None) \
            or r.get("display_name") or r.get("ai_title") or clean(r.get("first_message")) \
            or f"(untitled {sid[:8]})"
        return {
            "key": item_key, "sid": sid, "engine": engine,
            "original": clean(original, 140),
            "nick": nick.get(item_key) or "",
            "live": is_running(r),
            "pinned": bool(state.get("pinned", {}).get(item_key)),
            "needs_you": bool(r.get("needs_approval") or r.get("question_waiting")),
            "mtime": mtime_of(r),
            "cost": float(r.get("cost_usd") or 0),
            "cwd": short(cwd), "branch": r.get("git_branch") or r.get("branch") or "",
            "wt": wt or r.get("worktree_label") or "",
            "ccc_label": r.get("folder_label") or r.get("folder_label_chip") or "",
            "group": group, "group_source": gsrc,
            "orca_workspace": ws["name"] if ws else "",
            "orca_path": (ws.get("path") or "") if ws else "",
            "sid_raw": sid,
            "open_url": (f"{ccc_base}/?ccc_popout=conversation&conv={urllib.parse.quote(sid)}"
                         if engine != "pi" and sid else ""),
            "file": short(r.get("jsonl_path") or ""),
        }

    for r in singles:
        repo, cwd, wt, ws = place(r)
        key = f"{r.get('engine') or 'claude'}:{r.get('session_id')}"
        repo["items"].append(base_item(r, key, repo, cwd, wt, ws))

    for pk, runs in batches.items():
        runs.sort(key=mtime_of, reverse=True)
        head = runs[0]
        repo, cwd, wt, ws = place(head)
        key = "batch:" + hashlib.sha1((repo["key"] + "|" + pk).encode()).hexdigest()[:12]
        item = base_item(head, key, repo, cwd, wt, ws)
        item.update({
            "original": clean(head.get("first_message"), 140),
            "batch": len(runs),
            "live": any(is_running(x) for x in runs),
            "cost": sum(float(x.get("cost_usd") or 0) for x in runs),
            "runs": [{"sid": str(x.get("session_id")), "engine": x.get("engine") or "",
                      "mtime": mtime_of(x), "live": is_running(x),
                      "open_url": f"{ccc_base}/?ccc_popout=conversation&conv={urllib.parse.quote(str(x.get('session_id')))}"}
                     for x in runs[:50]],
        })
        repo["items"].append(item)

    user_hidden = 0
    hmap = state.get("hidden", {})

    # A hidden item that went live again (or is waiting on the user) is signal, not
    # noise, so it comes back on its own. Hides stay as tombstones; this only lifts
    # the ones whose subject became active again.
    live_items, live_groups, live_repos = set(), set(), set()
    for repo in repos.values():
        for it in repo["items"]:
            if not (it.get("live") or it.get("needs_you")):
                continue
            live_items.add(it["key"])
            live_groups.add(repo["key"] + "|" + it["group"])
            live_repos.add(repo["key"])
            for run in it.get("runs") or []:
                live_items.add("%s:%s" % (run.get("engine"), run.get("sid")))
    resurface = []
    for k in hmap:
        if k.startswith("r:"):
            if k[2:] in live_repos:
                resurface.append(k)
        elif k.startswith("g:"):
            if k[2:] in live_groups:
                resurface.append(k)
        elif k in live_items:
            resurface.append(k)
    if resurface:
        hmap = {k: v for k, v in hmap.items() if k not in set(resurface)}

    for repo in repos.values():
        kept = []
        repo_hidden = bool(hmap.get("r:" + repo["key"]))
        for it in repo["items"]:
            if repo_hidden or hmap.get(it["key"]) or hmap.get("g:%s|%s" % (repo["key"], it["group"])):
                user_hidden += 1
                if not show_hidden:
                    continue
                it["hidden"] = True
            kept.append(it)
        repo["items"] = kept

    out_repos = []
    pmap = state.get("pinned", {})
    for repo in repos.values():
        if not repo["items"]:
            continue
        manual = state["groups"].get(repo["key"], [])
        by_group = collections.defaultdict(list)
        for it in repo["items"]:
            by_group[it["group"]].append(it)
        names = sorted((g for g in by_group if g != UNGROUPED),
                       key=lambda g: (not pmap.get("g:%s|%s" % (repo["key"], g)), g.lower()))
        groups = []
        for g in names + ([UNGROUPED] if UNGROUPED in by_group else []):
            items = sorted(by_group[g], key=lambda x: (not x["pinned"], -x["mtime"]))
            srcs = {x["group_source"] for x in items}
            groups.append({"name": g, "source": "manual" if "manual" in srcs or g in manual
                           else ("orca" if "orca" in srcs else "none"), "items": items,
                           "hidden": bool(hmap.get("g:%s|%s" % (repo["key"], g))),
                           "pinned": bool(pmap.get("g:%s|%s" % (repo["key"], g))),
                           "orca_path": next((x["orca_path"] for x in items if x.get("orca_path")), "")})
        all_items = repo["items"]
        out_repos.append({
            "key": repo["key"], "name": repo["name"], "orig_name": repo["orig_name"], "path": repo["path"],
            "hidden": bool(hmap.get("r:" + repo["key"])),
            "pinned": bool(pmap.get("r:" + repo["key"])),
            "count": len(all_items),
            "live": sum(1 for x in all_items if x["live"]),
            "cost": round(sum(x["cost"] for x in all_items), 2),
            "latest": max((x["mtime"] for x in all_items), default=0),
            "groups": groups,
            "group_names": sorted(set(manual) | set(names), key=str.lower),
        })
    out_repos.sort(key=lambda r: (not r["pinned"], -r["latest"]))

    return {
        "window": window,
        "shown": sum(r["count"] for r in out_repos),
        "in_window": len(inwin),
        "hidden": [{"label": k, "count": v} for k, v in hidden.most_common()],
        "hidden_total": sum(hidden.values()),
        "user_hidden": user_hidden,
        "resurface": resurface,
        "icons": state.get("icons", {}),
        "diagnostics": {
            "resolution": dict(method_ct),
            "unresolved": unresolved.most_common(20),
            "engines": dict(collections.Counter(
                f"{r.get('engine') or '?'}/{origins.get(id(r)) or r.get('source') or '?'}" for r in keep)),
        },
        "repos": out_repos,
    }


# ---------------------------------------------------------------- http

class App:
    def __init__(self, args):
        self.args = args
        self.ccc = CCCSource(args.ccc)
        self.state = State(Path(args.state).expanduser())

    def rename(self, b):
        """Rename a session / group / project and write it through to the tool that owns the name."""
        kind, name = str(b.get("kind") or "session"), (b.get("name") or "").strip()
        res = {"ok": True, "applied": []}
        if kind == "session":
            key = str(b.get("key") or "")
            self.state.set_nickname(key, name)
            res["applied"].append("navigator")
            sid, engine = str(b.get("sid") or ""), str(b.get("engine") or "")
            if sid and engine != "pi":
                try:
                    body = json.dumps({"session_id": sid, "name": name}).encode()
                    base = urllib.parse.urlparse(self.ccc.base)
                    c = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=10)
                    c.request("POST", "/api/conversations/%s/rename" % urllib.parse.quote(sid), body=body,
                              headers={"Content-Type": "application/json", "Host": base.netloc})
                    r = c.getresponse(); r.read(); c.close()
                    res["applied"].append("ccc" if r.status < 300 else "ccc-failed:%d" % r.status)
                except Exception as e:
                    res["applied"].append("ccc-failed:%s" % e)
        elif kind == "group":
            repo, old, path = str(b.get("repo") or ""), str(b.get("group") or ""), str(b.get("orca_path") or "")
            if path and name:
                try:
                    out = subprocess.run(["/usr/local/bin/orca", "worktree", "set", "--worktree", "path:" + path,
                                          "--display-name", name], capture_output=True, text=True, timeout=25)
                    res["applied"].append("orca" if out.returncode == 0 else "orca-failed:" + (out.stderr or out.stdout)[:120])
                except Exception as e:
                    res["applied"].append("orca-failed:%s" % e)
            else:
                self.state.rename_group(repo, old, name)
                res["applied"].append("navigator")
        elif kind == "repo":
            self.state.set_repo_name(str(b.get("key") or ""), name)
            res["applied"].append("navigator")
        else:
            return {"ok": False, "error": "unknown kind"}
        self.ccc._at = 0.0  # re-read CCC so the new names show up at once
        return res

    def tree(self, window, show_hidden=False):
        rows, ccc_meta = self.ccc.rows()
        prows, pi_meta = pi_sessions(self.args.pi_dir)
        ws, orca_meta = orca.load_workspaces(self.args.orca_dir)
        t = build_tree(rows + prows, self.state.snapshot(), window, ws, self.ccc.base,
                       show_hidden=show_hidden)
        if t.get("resurface") and RESURFACE:
            self.state.set_hidden(t["resurface"], False)
        t["sources"] = {"ccc": dict(ccc_meta, base=self.ccc.base), "pi": pi_meta, "orca": orca_meta}
        t["generated_at"] = time.time()
        return t


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set in main()
    server_version = "ccc-navigator"

    def log_message(self, fmt, *a):
        if self.app.args.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    def _host_ok(self):
        # DNS-rebinding guard: only answer to localhost names.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    NAV_POST = {"/api/rename", "/api/nickname", "/api/assign", "/api/group", "/api/hide", "/api/pin", "/api/icon"}
    HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
           "trailers", "transfer-encoding", "upgrade"}

    def _proxy(self):
        """Same-origin passthrough to CCC so its conversation view can be themed.
        Streams (SSE) chunk by chunk; the popout page gets our skin injected."""
        base = urllib.parse.urlparse(self.app.ccc.base)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "accept-encoding", "origin", "referer", "content-length")}
        hdrs["Host"] = base.netloc
        hdrs["Accept-Encoding"] = "identity"
        conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except Exception as e:
            return self._send(502, {"error": "CCC unreachable: %s" % e})
        page = "ccc_popout" in self.path and (resp.getheader("Content-Type") or "").startswith("text/html")
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in self.HOP or (page and k.lower() == "content-length"):
                    continue
                self.send_header(k, v)
            if page:
                html = resp.read().decode("utf-8", "replace")
                inj = '<link rel="stylesheet" href="/__nav/skin.css">'
                html = html.replace("</head>", inj + "</head>", 1) if "</head>" in html else inj + html
                data = html.encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    do_PUT = do_DELETE = do_PATCH = lambda self: self._proxy()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        if u.path == "/__nav/skin.css":
            return self._send(200, (HERE / "static" / "ccc-skin.css").read_bytes(), "text/css; charset=utf-8")
        if u.path in ("/", "/index.html") and "ccc_popout" not in u.query:
            return self._send(200, (HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path != "/api/tree":
            return self._proxy()
        if u.path == "/api/tree":
            q = urllib.parse.parse_qs(u.query)
            w = q.get("window", ["1d"])[0]
            return self._send(200, self.app.tree(w if w in WINDOWS else "1d", q.get("hidden", ["0"])[0] == "1"))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return self._send(403, {"error": "cross-origin"})
        if urllib.parse.urlparse(self.path).path not in self.NAV_POST:
            return self._proxy()
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "json only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        st, p = self.app.state, urllib.parse.urlparse(self.path).path
        if p == "/api/nickname":
            st.set_nickname(str(body.get("key") or ""), body.get("name"))
        elif p == "/api/assign":
            st.assign(str(body.get("key") or ""), str(body.get("repo") or ""), body.get("group"))
        elif p == "/api/rename":
            return self._send(200, self.app.rename(body))
        elif p == "/api/icon":
            st.set_icon(str(body.get("key") or ""), body.get("icon"))
        elif p == "/api/pin":
            st.set_pinned(str(body.get("key") or ""), bool(body.get("pinned", True)))
        elif p == "/api/hide":
            keys = body.get("keys") if isinstance(body.get("keys"), list) else [body.get("key")]
            st.set_hidden([str(k) for k in keys if k], bool(body.get("hidden", True)))
        elif p == "/api/group":
            repo, action = str(body.get("repo") or ""), body.get("action")
            if action == "add":
                st.add_group(repo, body.get("name"))
            elif action == "rename":
                st.rename_group(repo, body.get("name"), body.get("new_name"))
            elif action == "remove":
                st.remove_group(repo, body.get("name"))
            else:
                return self._send(400, {"error": "unknown action"})
        else:
            return self._send(404, {"error": "not found"})
        return self._send(200, {"ok": True})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ccc", default=os.environ.get("CCC_URL", "http://localhost:8090"), help="CCC base URL")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CCC_NAV_PORT", 8095)))
    ap.add_argument("--state", default=os.environ.get("CCC_NAV_STATE", "~/.ccc-navigator/state.json"))
    ap.add_argument("--pi-dir", default=os.path.join(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"), "sessions"))
    ap.add_argument("--orca-dir", default=os.environ.get("ORCA_DATA_DIR") or None,
                    help="Orca's data folder (auto-detected when omitted)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    Handler.app = App(args)
    def _warm():  # the first full-history tree reads every transcript header; do it before anyone asks
        for w in ("1d", "all"):
            try:
                Handler.app.tree(w, True)
            except Exception:
                pass
    threading.Thread(target=_warm, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"CCC Navigator on {url}  (reading CCC at {args.ccc})")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
