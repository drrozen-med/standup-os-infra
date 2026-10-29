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

import changes
import decisions
import orca
import pi_run
import remote

HERE = Path(__file__).resolve().parent
HOME = str(Path.home())
WINDOWS = {"now": 3600, "1d": 86400, "7d": 7 * 86400, "all": None}
UNGROUPED = ""
HIDE_LOG_MAX = 30
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
     "assign":    {key: {"repo": repo_key, "group": name}},
     "moved":     {key: repo_key},   # session filed under another project than its folder
     "desktop_map": {desktop group name: repo_key}}   # unmapped desktop groups are their own project
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
            "moved": dict(d.get("moved") or {}),
            "desktop_map": dict(d.get("desktop_map") or {}),
            "hidden": dict(d.get("hidden") or {}),
            "pinned": dict(d.get("pinned") or {}),
            "icons": dict(d.get("icons") or {}),
            "repo_names": dict(d.get("repo_names") or {}),
            "notes": {k: list(v) for k, v in (d.get("notes") or {}).items()},
            "triage": dict(d.get("triage") or {}),
            "copilot": str(d.get("copilot") or ""),   # session key of Uri's main assistant
            "hide_log": list(d.get("hide_log") or [])[-HIDE_LOG_MAX:],
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

    def move_to_project(self, keys, repo):
        """File sessions under another project (repo key); an empty repo puts them back by folder."""
        with self.lock:
            for key in keys:
                if repo:
                    self.data["moved"][key] = repo
                else:
                    self.data["moved"].pop(key, None)
                a = self.data["assign"].get(key)
                if a and a.get("repo") != repo:
                    self.data["assign"].pop(key, None)
            self._save()
        return len(keys)

    def set_copilot(self, key):
        with self.lock:
            before = self.data["copilot"]
            self.data["copilot"] = (key or "").strip()[:200]
            self._save()
        return before

    def map_desktop_group(self, group, repo):
        """File a Claude desktop sidebar group's sessions under a project; an empty repo unmaps it."""
        group = (group or "").strip()[:80]
        with self.lock:
            before = self.data["desktop_map"].get(group)
            if repo:
                self.data["desktop_map"][group] = repo
            else:
                self.data["desktop_map"].pop(group, None)
            self._save()
        return before

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

    def add_note(self, key, text, by="you"):
        with self.lock:
            text = (text or "").strip()[:4000]
            if not key or not text:
                return None
            note = {"ts": time.time(), "text": text, "by": (by or "you")[:40]}
            self.data["notes"].setdefault(key, []).append(note)
            self._save()
            return note

    def remove_note(self, key, index):
        with self.lock:
            notes = self.data["notes"].get(key) or []
            if not 0 <= index < len(notes):
                return False
            notes.pop(index)
            if not notes:
                self.data["notes"].pop(key, None)
            self._save()
            return True

    def set_triage(self, entries):
        """entries: {key: {"label", "reason", "by"}}. A label set by the owner
        ("by": "you") is never overwritten by the sorter; label "" clears it."""
        with self.lock:
            changed = 0
            for key, e in entries.items():
                cur = self.data["triage"].get(key) or {}
                by = (e.get("by") or "sorter")[:40]
                if by != "you" and cur.get("by") == "you":
                    continue
                label = (e.get("label") or "").strip()[:30]
                if not label:
                    changed += key in self.data["triage"]
                    self.data["triage"].pop(key, None)
                    continue
                new = {"label": label, "reason": (e.get("reason") or "").strip()[:300], "by": by,
                       "ts": cur.get("ts") if cur.get("label") == label and cur.get("by") == by else time.time()}
                if cur != new:
                    self.data["triage"][key] = new
                    changed += 1
            if changed:
                self._save()
            return changed

    def set_pinned(self, key, pinned):
        with self.lock:
            if pinned:
                self.data["pinned"][key] = True
            else:
                self.data["pinned"].pop(key, None)
            self._save()

    def set_hidden(self, keys, hidden, label=""):
        """Hide or unhide keys. Every hide is logged so it can be undone later; returns the log id."""
        with self.lock:
            for key in keys:
                if hidden:
                    self.data["hidden"][key] = True
                else:
                    self.data["hidden"].pop(key, None)
            entry_id = None
            if hidden and keys:
                entry_id = hashlib.sha1(("%s%s" % (time.time(), keys)).encode()).hexdigest()[:10]
                self.data["hide_log"].append({"id": entry_id, "ts": time.time(), "keys": list(keys),
                                              "label": (label or "")[:120]})
                self.data["hide_log"] = self.data["hide_log"][-HIDE_LOG_MAX:]
            self._save()
            return entry_id

    def undo_hide(self, entry_id):
        """Unhide everything one logged hide hid. None when the id is unknown or already undone."""
        with self.lock:
            entry = next((e for e in self.data["hide_log"] if e.get("id") == entry_id and not e.get("undone")), None)
            if not entry:
                return None
            for key in entry["keys"]:
                self.data["hidden"].pop(key, None)
            entry["undone"] = time.time()
            self._save()
            return entry

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


DESKTOP_DIR = os.environ.get("CCC_NAV_DESKTOP_DIR", "~/Library/Application Support/Claude/claude-code-sessions")
DESKTOP_CONFIG = os.environ.get("CCC_NAV_DESKTOP_CONFIG", "~/Library/Application Support/Claude/claude_desktop_config.json")
DESKTOP_PREFIX = "desktop:"   # project key for a desktop group with no project mapped to it
TITLER_STATE = os.environ.get("CCC_NAV_TITLER_STATE", "~/.ccc-navigator/titler.json")
_desktop_cache: dict[str, tuple[float, dict]] = {}


def desktop_titles(root=DESKTOP_DIR):
    """Session titles from the Claude desktop app (Code tab): CLI session id -> title.
    Read-only; each record file is re-read only when its mtime changes."""
    out = {}
    for f in Path(root).expanduser().glob("*/*/local_*.json"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        hit = _desktop_cache.get(str(f))
        if not hit or hit[0] != m:
            try:
                d = json.loads(f.read_text())
                rec = {"sid": str(d.get("cliSessionId") or ""), "title": clean(d.get("title"), 140),
                       "lid": str(d.get("sessionId") or f.stem),
                       "bridge": str((d.get("bridgeSessionIds") or [""])[-1] or "")}
            except (OSError, ValueError, AttributeError, TypeError):
                rec = {"sid": "", "title": "", "lid": "", "bridge": ""}
            _desktop_cache[str(f)] = hit = (m, rec)
        if hit[1]["sid"] and hit[1]["title"]:
            out[hit[1]["sid"]] = hit[1]["title"]
    return out


BRIDGE_RE = re.compile(r"^session_[0-9A-Za-z]{10,40}$")
LOCAL_RE = re.compile(r"^local_[0-9a-fA-F-]{8,40}$")


def desktop_links(sid, root=DESKTOP_DIR):
    """Links that open a Claude desktop session: on claude.ai (phone, needs Remote Control on) and in the Mac app."""
    desktop_titles(root)
    rec = next((r for _, r in _desktop_cache.values() if r.get("sid") == sid), None)
    if not rec:
        return {}
    out = {"title": rec.get("title") or ""}
    if BRIDGE_RE.match(rec.get("bridge") or ""):
        out["url"] = "https://claude.ai/code/" + rec["bridge"]
    if LOCAL_RE.match(rec.get("lid") or ""):
        out["mac_url"] = "claude://claude.ai/epitaxy/" + rec["lid"]
    return out


def desktop_groups(root=DESKTOP_DIR, config=DESKTOP_CONFIG):
    """Sidebar groups from the Claude desktop app: CLI session id -> group name. Read-only."""
    try:
        prefs = json.loads(Path(config).expanduser().read_text())["preferences"]["epitaxyPrefs"]
        scopes = prefs.get("dframe-group-scopes") or {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    by_lid = {}
    for scope in scopes.values() if isinstance(scopes, dict) else []:
        try:
            names = {g["id"]: str(g["name"]).strip() for g in scope.get("groups") or []}
            for k, gid in (scope.get("assignments") or {}).items():
                if names.get(gid) and str(k).startswith("code:"):
                    by_lid[str(k)[5:]] = names[gid][:80]
        except (KeyError, TypeError, AttributeError):
            continue
    if not by_lid:
        return {}
    desktop_titles(root)     # fills the record cache (local id -> CLI id)
    return {rec["sid"]: by_lid[rec["lid"]] for _, rec in _desktop_cache.values()
            if rec.get("sid") and rec.get("lid") in by_lid}


def titler_titles(path=TITLER_STATE):
    """Titles the background titler set, so a desktop title can replace them (a user's own rename is kept)."""
    try:
        d = json.loads(Path(path).expanduser().read_text())
        return {k: v.get("title") for k, v in d.items() if isinstance(v, dict) and v.get("title")}
    except (OSError, ValueError, AttributeError):
        return {}


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
        "is_live": (time.time() - st.st_mtime) < 120,
        "live": (time.time() - st.st_mtime) < 120,
    }


def _pi_file_for_sid(pi_dir, sid):
    """Look up the Pi jsonl for a session id, reusing the Pi cache."""
    try:
        for row in (pi_sessions(pi_dir)[0] or []):
            if row and str(row.get("session_id")) == str(sid):
                jp = row.get("jsonl_path")
                if jp:
                    pp = Path(jp)
                    if pp.is_file():
                        return pp
    except Exception:
        pass
    # fallback: scan headers directly
    root = Path(str(pi_dir)).expanduser()
    if root.is_dir():
        for f in root.glob("*/*.jsonl"):
            try:
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if o.get("type") == "session" and str(o.get("id")) == str(sid):
                            return f
            except OSError:
                continue
    return None


def _pi_content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("text", "thinking", "output_text"):
                    parts.append(str(p.get("text") or p.get("thinking") or ""))
                elif "text" in p:
                    parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("thinking") or "")
    return "" if content is None else str(content)


def _pi_content_images(content):
    out = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("image", "image_url"):
                out.append(p)
    return out


def pi_transcript(*args, **kwargs):
    """Parse a Pi jsonl into the event shape static/chat.js expects.

    Accepts pi_dir and sid in either positional order or as keywords
    (pi_dir=, sid= / session_id= / id=).
    """
    pi_dir = kwargs.get("pi_dir", kwargs.get("dir", kwargs.get("pi_dir_path")))
    sid = kwargs.get("sid", kwargs.get("session_id", kwargs.get("id")))
    for a in args:
        pa = None
        if isinstance(a, (str, Path)):
            try:
                pa = Path(a).expanduser()
            except Exception:
                pa = None
        if pi_dir is None and pa is not None and pa.is_dir():
            pi_dir = a
        elif sid is None:
            sid = a
    if pi_dir is None:
        pi_dir = os.path.join(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"), "sessions")
    if sid is None:
        return {"events": [], "engine": "pi", "session_id": None}

    path = _pi_file_for_sid(pi_dir, sid)
    if not path or not path.is_file():
        return {"events": [], "engine": "pi", "session_id": sid, "error": "not found"}

    events = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("type")
                if t != "message":
                    # keep session_info as a lightweight system note? skip to match chat shape
                    continue
                m = o.get("message") or {}
                role = m.get("role")
                content = m.get("content")
                ts = m.get("timestamp") or o.get("timestamp")
                if role == "user":
                    events.append({
                        "type": "user_text",
                        "text": _pi_content_text(content),
                        "images": _pi_content_images(content),
                        "timestamp": ts,
                    })
                elif role == "assistant":
                    blocks = []
                    if isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict):
                                if isinstance(part, str) and part.strip():
                                    blocks.append({"kind": "text", "text": part})
                                continue
                            ptype = part.get("type")
                            if ptype == "text":
                                if part.get("text"):
                                    blocks.append({"kind": "text", "text": part.get("text")})
                            elif ptype == "thinking":
                                txt = part.get("thinking") or part.get("text") or ""
                                if txt:
                                    blocks.append({"kind": "thinking", "text": txt})
                            elif ptype in ("toolCall", "tool_call", "tool_use"):
                                name = (part.get("name") or part.get("toolName")
                                        or part.get("tool_name") or "Tool")
                                args = (part.get("arguments") or part.get("input")
                                        or part.get("args") or {})
                                if isinstance(args, str):
                                    detail = args
                                else:
                                    try:
                                        detail = json.dumps(args)
                                    except Exception:
                                        detail = str(args)
                                blocks.append({"kind": "tool_use", "name": name, "detail": detail})
                            elif ptype == "text" and part.get("text"):
                                blocks.append({"kind": "text", "text": part.get("text")})
                    elif isinstance(content, str) and content.strip():
                        blocks.append({"kind": "text", "text": content})
                    if blocks:
                        ev = {"type": "assistant", "blocks": blocks}
                        if ts is not None:
                            ev["timestamp"] = ts
                        events.append(ev)
                elif role in ("toolResult", "tool_result"):
                    events.append({
                        "type": "tool_result",
                        "text": _pi_content_text(content),
                        "timestamp": ts,
                    })
    except OSError:
        return {"events": [], "engine": "pi", "session_id": sid, "error": "read error"}

    # cap very large files: last 400 messages/events
    if len(events) > 400:
        events = events[-400:]
    return {"events": events, "engine": "pi", "session_id": sid}


# aliases for callers/tests that use a different name
read_pi_transcript = pi_transcript
get_pi_transcript = pi_transcript
pi_conversation = pi_transcript
pi_transcript_events = pi_transcript


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


_version_cache = {"sig": None, "v": ""}


def app_version():
    """Short hash of the files the browser runs; changes whenever a new version is deployed."""
    files = [HERE / "static" / n for n in ("index.html", "chat.js", "chat.css")]
    sig = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
    if sig != _version_cache["sig"]:
        h = hashlib.sha1()
        for f in files:
            if f.exists():
                h.update(f.read_bytes())
        _version_cache.update(sig=sig, v=h.hexdigest()[:10])
    return _version_cache["v"]


def static_asset(name):
    """Resolve one file under static/ as (bytes, content-type), or None.

    Only the basename is ever used and only if the file exists, so a request
    for /__nav/../../etc/passwd can never leave the folder.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    f = HERE / "static" / name
    if not f.is_file():
        return None
    ctype = {"css": "text/css; charset=utf-8",
             "js": "application/javascript; charset=utf-8",
             "svg": "image/svg+xml", "png": "image/png",
             "webmanifest": "application/manifest+json"}.get(name.rsplit(".", 1)[-1], "text/plain; charset=utf-8")
    return f.read_bytes(), ctype


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


def build_tree(rows, state, window, orca_ws, ccc_base, batch_min=3, now=None, show_hidden=False,
               desktop=None, auto_titles=None, desktop_group=None):
    """desktop_group: CLI session id -> Claude desktop sidebar group. Placement order:
    an explicit move, then the desktop group (its mapped project, or its own), then the folder."""
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
    moved = state.get("moved") or {}
    dmap = state.get("desktop_map") or {}
    method_ct = collections.Counter()
    unresolved = collections.Counter()
    repos = {}

    def dgroup(r):
        return (desktop_group or {}).get(str(r.get("session_id") or "")) if str(r.get("engine") or "claude") == "claude" else None

    def place(r, item_key=None):
        cwd = r.get("session_cwd") or r.get("folder_path") or ""
        dg = dgroup(r) if item_key not in moved else None
        if item_key in moved or dg:
            key = moved[item_key] if item_key in moved else dmap.get(dg) or DESKTOP_PREFIX + dg
            name = key.split(":", 1)[1] if ":" in key and not key.startswith("/") else Path(key).name
            method_ct["moved" if item_key in moved else "desktop-group"] += 1
            repo = repos.setdefault(key, {"key": key, "name": state.get("repo_names", {}).get(key) or name,
                                          "orig_name": name, "path": short(key) if key.startswith("/") else "",
                                          "items": []})
            return repo, cwd, None, None
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
        # the desktop app's title (yours or its own) beats CCC's guess and the titler's
        dt = (desktop or {}).get(sid) if engine == "claude" else None
        name = nick.get(item_key) or ""
        if dt:
            original = dt
            if name and name == (auto_titles or {}).get(item_key):
                name = ""
        return {
            "key": item_key, "sid": sid, "engine": engine,
            "original": clean(original, 140),
            "nick": name,
            "moved": item_key in moved,
            "desktop_group": dgroup(r) or "",
            "live": is_running(r),
            "pinned": bool(state.get("pinned", {}).get(item_key)),
            "notes": len(state.get("notes", {}).get(item_key) or []),
            "last_note": ((state.get("notes", {}).get(item_key) or [{}])[-1].get("text") or "")[:200],
            "triage": (state.get("triage", {}).get(item_key) or {}).get("label") or "",
            "triage_reason": (state.get("triage", {}).get(item_key) or {}).get("reason") or "",
            "triage_by": (state.get("triage", {}).get(item_key) or {}).get("by") or "",
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
        key = f"{r.get('engine') or 'claude'}:{r.get('session_id')}"
        repo, cwd, wt, ws = place(r, key)
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
        home = Path(args.state).expanduser().parent       # a test instance with its own --state keeps its own files
        self.inbox = decisions.Inbox(
            os.environ.get("CCC_NAV_DECISIONS") or home / "decisions.json",
            os.environ.get("CCC_NAV_ANSWERS") or home / "answers.json",
            os.environ.get("CCC_NAV_WATCHTOWER_DECISIONS",
                           "~/projects/standup-os-watchtower/watchtower/decisions.json"))
        self.changes = changes.ChangeLog(os.environ.get("CCC_NAV_CHANGES") or home / "changes.jsonl")

    def deliver(self, session_key, text):
        """Send text into an agent session: Pi by resuming it, everything else through CCC. -> short status."""
        engine, _, sid = str(session_key or "").partition(":")
        if not sid:
            return "no-session"
        if engine == "pi":
            r = pi_run.send(self.args.pi_dir, sid, text)
            return "sent" if r.get("ok") else "failed: " + str(r.get("error"))
        try:
            body = json.dumps({"session_id": sid, "text": text, "mode": "send"}).encode()
            base = urllib.parse.urlparse(self.ccc.base)
            c = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=15)
            c.request("POST", "/api/inject-input", body=body, headers={"Content-Type": "application/json", "Host": base.netloc})
            r = c.getresponse(); out = r.read(); c.close()
            ok = r.status < 300 and json.loads(out or b"{}").get("ok", True) is not False
            return "sent" if ok else "failed: HTTP %d" % r.status
        except Exception as e:
            return "failed: %s" % e

    def answer_decision(self, b, actor):
        did = str(b.get("id") or "")
        d = self.inbox.get(did)
        rec, before = self.inbox.answer(did, b.get("choice"), b.get("note"), by=actor)
        cid = self.changes.record(actor, "decision.answer", {"id": did, "choice": rec["choice"]},
                                  {"answers": {did: before}}, {"answers": {did: rec}})
        delivery = self.deliver(d.get("session"), decisions.message_for(d, rec)) if d and d.get("session") else "recorded"
        return {"ok": True, "id": did, "answer": rec, "change": cid, "delivery": delivery}

    def rename(self, b):
        """Rename a session / group / project and write it through to the tool that owns the name."""
        kind, name = str(b.get("kind") or "session"), (b.get("name") or "").strip()
        res = {"ok": True, "applied": []}
        if kind == "session":
            key = str(b.get("key") or "")
            self.state.set_nickname(key, name)
            res["applied"].append("navigator")
            sid, engine = str(b.get("sid") or ""), str(b.get("engine") or "")
            if sid and engine == "pi":
                pj = _pi_file_for_sid(self.args.pi_dir, sid)
                if pj is not None:
                    try:
                        st = pj.stat()
                        if time.time() - st.st_mtime < 30:
                            res["applied"].append("pi-busy")
                        else:
                            entry = {"type": "session_info", "name": name}
                            with pj.open("a", encoding="utf-8") as fh:
                                fh.write(json.dumps(entry) + "\n")
                                fh.flush()
                                os.fsync(fh.fileno())
                            res["applied"].append("pi")
                    except Exception as e:
                        res["applied"].append("pi-failed:%s" % e)
            elif sid:
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
                       show_hidden=show_hidden, desktop=desktop_titles(), auto_titles=titler_titles(),
                       desktop_group=desktop_groups())
        t["copilot"] = self.copilot()
        if t.get("resurface") and RESURFACE:
            self.state.set_hidden(t["resurface"], False)
        t["sources"] = {"ccc": dict(ccc_meta, base=self.ccc.base), "pi": pi_meta, "orca": orca_meta}
        t["generated_at"] = time.time()
        return t

    def copilot(self):
        """The main assistant's session and the links that open it, or None when none is set."""
        key = self.state.snapshot()["copilot"]
        if not key:
            return None
        out = {"key": key, "name": self.state.snapshot()["nicknames"].get(key) or ""}
        if key.startswith("claude:"):
            links = desktop_links(key[7:])
            out.update(links)
            out["name"] = links.get("title") or out["name"]     # the desktop title is the one Uri sees
        return out

    def desktop_group_list(self):
        """Every Claude desktop sidebar group with its session count and the project it files under."""
        dmap = self.state.snapshot()["desktop_map"]
        counts = collections.Counter(desktop_groups().values())
        return [{"name": g, "sessions": counts[g], "project": dmap.get(g) or DESKTOP_PREFIX + g,
                 "mapped": g in dmap} for g in sorted(set(counts) | set(dmap), key=str.lower)]

    @staticmethod
    def project_folders(root="~/projects"):
        """Git checkouts under ~/projects, so a group can be mapped to a project with no recent sessions."""
        base = Path(root).expanduser()
        try:
            return sorted(str(p) for p in base.iterdir() if (p / ".git").exists())
        except OSError:
            return []

    def pi_transcript(self, sid):
        return pi_transcript(self.args.pi_dir, sid)


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set in main()
    server_version = "ccc-navigator"

    def log_message(self, fmt, *a):
        if self.app.args.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    remote_cfg = remote.RemoteConfig("", Path("."))  # set in main()
    remote_store = None
    limiter = None
    PUBLIC_STATIC = {"/__nav/manifest.webmanifest", "/__nav/icon-192.png", "/__nav/icon-512.png",
                     "/__nav/apple-touch-icon.png"}
    LOCAL_ONLY = {"/api/remote/pair", "/api/remote/sessions", "/api/remote/revoke"}

    def _host_ok(self):
        # DNS-rebinding guard: only answer to localhost names (and the tunnel's public name, when set).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1") or (
            self.remote_cfg.enabled and host == self.remote_cfg.public_host)

    def _gate(self):
        """Remote requests (through the tunnel) need a paired session. Returns False when it already answered."""
        self._rem = remote.is_remote(self.headers, self.remote_cfg.public_host)
        if not self._rem:
            return True
        path = urllib.parse.urlparse(self.path).path
        if not self.remote_cfg.enabled:
            self._send(403, {"error": "remote access is off"})
            return False
        if path in self.LOCAL_ONLY:
            self._send(403, {"error": "only from the Mac itself"})
            return False
        if path == "/__pair" and self.command in ("GET", "POST"):
            self._pair()
            return False
        if path in self.PUBLIC_STATIC and self.command == "GET":
            return True
        if self.remote_store.check_session(self.headers.get("Cookie")):
            if self.command != "GET" and not self._origin_ok():
                self._send(403, {"error": "cross-origin"})
                return False
            return True
        if path.startswith("/api/"):
            self._send(401, {"error": "not paired: on the Mac run `nav remote pair`"})
        else:
            self._send(401, remote.page("Pair this phone",
                                        "On the Mac, run <code>nav remote pair</code> and open the link it prints here. "
                                        "The link works once and expires after 15 minutes."), "text/html; charset=utf-8")
        return False

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if getattr(self, "_rem", False):
            return origin == "https://" + self.remote_cfg.public_host
        return not origin or urllib.parse.urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")

    def _pair(self):
        ip = remote.client_ip(self.headers)
        html = "text/html; charset=utf-8"
        if not self.limiter.allow(ip):
            return self._send(429, remote.page("Too many tries", "Wait 15 minutes, then make a new link on the Mac."), html)
        if self.command == "GET":  # show the button only; the code is spent by the POST
            code = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("code") or [""])[0]
            if not remote.CODE_RE.match(code):
                self.limiter.fail(ip)
                return self._send(403, remote.page("Link not valid", "This pairing link is mistyped."), html)
            return self._send(200, remote.confirm_page(code), html)
        if self.headers.get("Origin") != "https://" + self.remote_cfg.public_host:
            return self._send(403, remote.page("Link not valid", "Open the pairing link directly on the phone."), html)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode("ascii", "replace"))
        except ValueError:
            form = {}
        code = (form.get("code") or [""])[0]
        sid = self.remote_store.redeem_pairing(code, self.headers.get("User-Agent") or "")
        if not sid:
            self.limiter.fail(ip)
            return self._send(403, remote.page("Link not valid",
                                               "This pairing link was already used, has expired or is mistyped. "
                                               "On the Mac run <code>nav remote pair</code> for a new one."), html)
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", remote.session_cookie(sid))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        if getattr(self, "_rem", False):
            sent = b"".join(getattr(self, "_headers_buffer", [])).lower()
            for k, v in remote.SECURITY_HEADERS:
                if k.lower().encode() + b":" not in sent:
                    self.send_header(k, v)
        super().end_headers()

    def log_request(self, code="-", size="-"):
        if getattr(self, "_rem", False) and self.remote_store:
            self.remote_store.access_log(remote.client_ip(self.headers), self.command, self.path, code)
        super().log_request(code, size)

    def _remote_api(self, path, body):
        cfg = self.remote_cfg
        if path == "/api/remote/sessions":
            return self._send(200, {"public_host": cfg.public_host, "enabled": cfg.enabled,
                                    "sessions": self.remote_store.list_sessions()})
        if path == "/api/remote/pair":
            if not cfg.enabled:
                return self._send(400, {"error": "remote access is off (CCC_NAV_PUBLIC_HOST is not set)"})
            code, exp = self.remote_store.create_pairing()
            return self._send(200, {"link": "https://%s/__pair?code=%s" % (cfg.public_host, code), "expires_at": exp})
        if path == "/api/remote/revoke":
            which = str((body or {}).get("id") or "").strip()
            if not which:
                return self._send(400, {"error": "id or 'all' required"})
            return self._send(200, {"revoked": self.remote_store.revoke(which)})
        return self._send(404, {"error": "not found"})

    def _actor(self):
        """Who is acting: the X-Nav-Actor header (agents, CLI), else the owner. The phone is always the owner."""
        if remote.is_remote(self.headers, self.remote_cfg.public_host):
            return "you"
        a = (self.headers.get("X-Nav-Actor") or "you").strip().lower()
        return a if changes.ACTOR_RE.match(a) else "you"

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, name):
        """Serve one file from static/; nothing else is ever read from disk."""
        got = static_asset(name)
        if not got:
            return self._send(404, {"error": "not found"})
        return self._send(200, got[0], got[1])

    NAV_POST = {"/api/rename", "/api/nickname", "/api/assign", "/api/group", "/api/hide", "/api/pin", "/api/icon", "/api/note", "/api/triage", "/api/pi/send", "/api/hide/undo", "/api/unhide-scope", "/api/move-project", "/api/desktop-map", "/api/copilot",
                "/api/decisions/answer", "/api/decisions/reopen", "/api/decisions/ask"}
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

    def _proxy_gated(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._gate():
            return
        if not self._rem and not self._origin_ok():
            return self._send(403, {"error": "cross-origin"})
        return self._proxy()

    do_PUT = do_DELETE = do_PATCH = _proxy_gated

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._gate():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/remote/sessions":
            return self._remote_api(u.path, None)
        if u.path.startswith("/__nav/"):
            return self._static(u.path.rsplit("/", 1)[-1])
        if u.path in ("/", "/index.html") and "ccc_popout" not in u.query:
            html = (HERE / "static" / "index.html").read_bytes().replace(b"__NAV_VERSION__", app_version().encode())
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/hidelog":
            log = self.app.state.snapshot()["hide_log"]
            return self._send(200, {"entries": [e for e in reversed(log) if not e.get("undone")][:15]})
        if u.path == "/api/version":
            return self._send(200, {"version": app_version()})
        if u.path == "/api/pi/transcript":
            q = urllib.parse.parse_qs(u.query)
            sid = (q.get("sid") or [""])[0]
            if not sid:
                return self._send(400, {"error": "sid required"})
            return self._send(200, self.app.pi_transcript(sid))
        if u.path == "/api/pi/run":
            return self._send(200, pi_run.status((urllib.parse.parse_qs(u.query).get("id") or [""])[0]))
        if u.path == "/api/decisions":
            return self._send(200, dict(self.app.inbox.list(), ok=True))
        if u.path == "/api/desktop-groups":
            return self._send(200, {"ok": True, "groups": self.app.desktop_group_list(),
                                    "folders": self.app.project_folders()})
        if u.path == "/api/notes":
            key = (urllib.parse.parse_qs(u.query).get("key") or [""])[0]
            notes = self.app.state.snapshot()["notes"]
            return self._send(200, {"key": key, "notes": notes.get(key, [])} if key else {"notes": notes})
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
        if not self._gate():
            return
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin"})
        path = urllib.parse.urlparse(self.path).path
        if path in self.LOCAL_ONLY:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            return self._remote_api(path, body)
        if path not in self.NAV_POST:
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
        elif p == "/api/pi/send":
            res = pi_run.send(self.app.args.pi_dir, body.get("sid"), body.get("text"))
            return self._send(200 if res.get("ok") else 400, res)
        elif p == "/api/note":
            key = str(body.get("key") or "")
            if body.get("remove") is not None:
                try:
                    ok = st.remove_note(key, int(body.get("remove")))
                except (TypeError, ValueError):
                    ok = False
                return self._send(200 if ok else 404, {"ok": ok})
            note = st.add_note(key, body.get("text"), body.get("by"))
            if note is None:
                return self._send(400, {"error": "key and text required"})
            return self._send(200, {"ok": True, "note": note})
        elif p == "/api/triage":
            entries = body.get("entries")
            if not isinstance(entries, dict):
                entries = {str(body.get("key") or ""): body}
            entries = {str(k): v for k, v in entries.items() if k and isinstance(v, dict)}
            return self._send(200, {"ok": True, "changed": st.set_triage(entries)})
        elif p == "/api/pin":
            st.set_pinned(str(body.get("key") or ""), bool(body.get("pinned", True)))
        elif p == "/api/hide":
            keys = body.get("keys") if isinstance(body.get("keys"), list) else [body.get("key")]
            entry_id = st.set_hidden([str(k) for k in keys if k], bool(body.get("hidden", True)), str(body.get("label") or ""))
            return self._send(200, {"ok": True, "id": entry_id})
        elif p.startswith("/api/decisions/"):
            actor = self._actor()
            try:
                if p == "/api/decisions/answer":
                    return self._send(200, self.app.answer_decision(body, actor))
                if p == "/api/decisions/reopen":
                    did = str(body.get("id") or "")
                    before = self.app.inbox.reopen(did)
                    cid = self.app.changes.record(actor, "decision.reopen", {"id": did}, {"answers": {did: before}},
                                                  {"answers": {did: None}})
                    return self._send(200, {"ok": True, "id": did, "change": cid})
                d = self.app.inbox.ask(body.get("question"), body.get("options"), body.get("lane"), body.get("priority", 2),
                                       body.get("deadline"), body.get("session"), body.get("source"), body.get("id") or "",
                                       by=actor)
                self.app.changes.record(actor, "decision.ask", {"id": d["id"]}, {}, {"questions": {d["id"]: d["question"]}},
                                        undoable=False)
                return self._send(200, {"ok": True, "decision": d})
            except (decisions.DecisionError, changes.ChangeError) as e:
                return self._send(400, {"ok": False, "error": {"code": e.code, "message": str(e)}})
        elif p == "/api/move-project":
            keys = body.get("keys") if isinstance(body.get("keys"), list) else [body.get("key")]
            keys = [str(k) for k in keys if k and not str(k).startswith("batch:")]
            if not keys:
                return self._send(400, {"ok": False, "error": "no session keys"})
            return self._send(200, {"ok": True, "count": st.move_to_project(keys, str(body.get("repo") or "")),
                                    "repo": str(body.get("repo") or "")})
        elif p == "/api/copilot":
            key = str(body.get("key") or "").strip()
            if key and not re.match(r"^[a-z][a-z0-9_-]*:\S+$", key):
                return self._send(400, {"ok": False, "error": "key must be <engine>:<id>"})
            before = st.set_copilot(key)
            cid = self.app.changes.record(self._actor(), "copilot.set", {"key": key},
                                          {"copilot": {"key": before or None}}, {"copilot": {"key": key or None}})
            return self._send(200, {"ok": True, "copilot": self.app.copilot(), "change": cid})
        elif p == "/api/desktop-map":
            group, repo = str(body.get("group") or "").strip(), str(body.get("repo") or "")
            if not group:
                return self._send(400, {"ok": False, "error": "group required"})
            before = st.map_desktop_group(group, repo)
            cid = self.app.changes.record(self._actor(), "desktop-group.map", {"group": group, "repo": repo},
                                          {"desktop_map": {group: before}}, {"desktop_map": {group: repo or None}})
            return self._send(200, {"ok": True, "group": group, "repo": repo, "change": cid})
        elif p == "/api/unhide-scope":
            repo = str(body.get("repo") or "")
            group = str(body.get("group")) if body.get("group") is not None else None
            t = self.app.tree("all", show_hidden=True)
            r = next((x for x in t["repos"] if x["key"] == repo), None)
            if not r:
                return self._send(404, {"ok": False, "error": "unknown project"})
            groups = [g for g in r["groups"] if group is None or g["name"] == group]
            keys = [s["key"] for g in groups for s in g["items"] if s.get("hidden")]
            keys += ["r:" + repo] if group is None else []
            keys += ["g:%s|%s" % (repo, g["name"]) for g in groups]
            hidden = st.snapshot()["hidden"]
            keys = [k for k in keys if k in hidden]
            st.set_hidden(keys, False)
            return self._send(200, {"ok": True, "count": len(keys)})
        elif p == "/api/hide/undo":
            entry = st.undo_hide(str(body.get("id") or ""))
            return self._send(200 if entry else 404, {"ok": bool(entry), "entry": entry})
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
    Handler.remote_cfg = remote.load_config()
    Handler.remote_store = remote.Store(Handler.remote_cfg.state_dir)
    Handler.limiter = remote.RateLimiter()
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
