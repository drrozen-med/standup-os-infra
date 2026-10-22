"""Read Orca's workspaces (git worktrees it created), read-only.

Orca (github.com/stablyai/orca) keeps per-profile state in SQLite:
<userData>/profiles/<profileId>/profile-state.db, table
profile_state_documents(domain TEXT PK, payload TEXT JSON, ...). Domains used:

- repos:            [{id, path, displayName, ...}]
- worktreeMeta:     {"<repoId>::<absWorktreePath>": {displayName, isArchived,
                     createdWithAgent, ...}}  (defaults are omitted on write)
- folderWorkspaces: [{id, name, folderPath, isArchived, ...}]  (non-git folders)
- settings:         {workspaceDir, nestWorkspaces, ...}

An empty displayName means "show the branch name", so we read the branch
from the worktree's .git file. The DB runs in WAL mode; we copy db + wal +
shm to a temp dir and open the copy so we never touch Orca's live files.
Orca renames the folder and branch after the first prompt, so sessions can
point at a stale path; workspace_for() falls back to matching the branch.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_PROFILE = "local-default"
_RELOAD_SECS = 10.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "sig": None, "val": ([], {"found": False})}


def candidate_dirs():
    home = Path.home()
    if sys.platform == "darwin":
        yield home / "Library" / "Application Support" / "Orca"
    elif os.name == "nt":
        yield Path(os.environ.get("APPDATA", home)) / "Orca"
    else:
        yield Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "Orca"


def find_db(data_dir=None):
    for d in ([Path(data_dir).expanduser()] if data_dir else list(candidate_dirs())):
        if not d.is_dir():
            continue
        profile = DEFAULT_PROFILE
        try:
            idx = json.loads((d / "orca-profile-index.json").read_text())
            profile = idx.get("activeProfileId") or profile
        except (OSError, ValueError):
            pass
        db = d / "profiles" / profile / "profile-state.db"
        if db.is_file():
            return d, db
        return d, None
    return None, None


def _read_domains(db: Path, domains):
    with tempfile.TemporaryDirectory(prefix="ccc-nav-orca-") as tmp:
        dst = Path(tmp) / "profile-state.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, str(dst) + suffix)
        con = sqlite3.connect(str(dst))
        try:
            q = "select domain, payload from profile_state_documents where domain in (%s)" % ",".join("?" * len(domains))
            out = {}
            for domain, payload in con.execute(q, list(domains)):
                try:
                    out[domain] = json.loads(payload)
                except (TypeError, ValueError):
                    pass
            return out
        finally:
            con.close()


def _branch_of(path: Path):
    """Current branch of a worktree from .git -> gitdir/HEAD (no subprocess)."""
    try:
        g = path / ".git"
        gitdir = g
        if g.is_file():
            txt = g.read_text().strip()
            if not txt.startswith("gitdir:"):
                return ""
            gitdir = Path(txt.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (path / gitdir).resolve()
        head = (gitdir / "HEAD").read_text().strip()
        return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    except OSError:
        return ""


def parse(docs):
    """Domains dict -> list of workspaces {path, name, repo_path, repo_name, archived, agent, branch}."""
    repos = {r.get("id"): r for r in (docs.get("repos") or []) if isinstance(r, dict)}
    out = []
    for key, meta in (docs.get("worktreeMeta") or {}).items():
        if "::" not in key or not isinstance(meta, dict):
            continue
        repo_id, path = key.split("::", 1)
        if path.startswith("workspace:"):
            continue
        repo = repos.get(repo_id) or {}
        p = Path(path)
        branch = _branch_of(p) if p.exists() else ""
        name = (meta.get("displayName") or "").strip() or branch or p.name
        out.append({
            "path": path.rstrip("/"), "name": name, "branch": branch,
            "repo_path": repo.get("path") or "",
            "repo_name": repo.get("displayName") or Path(repo.get("path") or "").name,
            "archived": bool(meta.get("isArchived")),
            "agent": meta.get("createdWithAgent") or "",
        })
    for fw in docs.get("folderWorkspaces") or []:
        if isinstance(fw, dict) and fw.get("folderPath"):
            out.append({
                "path": str(fw["folderPath"]).rstrip("/"), "name": fw.get("name") or Path(fw["folderPath"]).name,
                "branch": "", "repo_path": "", "repo_name": "",
                "archived": bool(fw.get("isArchived")), "agent": fw.get("createdWithAgent") or "",
            })
    return out


def load_workspaces(data_dir=None):
    """-> (workspaces, meta). Re-reads at most every 10s, and only if the DB changed."""
    with _lock:
        if time.time() - _cache["at"] < _RELOAD_SECS:
            return _cache["val"]
        _cache["at"] = time.time()
        d, db = find_db(data_dir)
        if not db:
            _cache["val"] = ([], {"found": False, "dir": str(d) if d else None})
            return _cache["val"]
        try:
            sig = tuple((Path(str(db) + s).stat().st_mtime_ns if Path(str(db) + s).exists() else 0)
                        for s in ("", "-wal"))
        except OSError:
            sig = None
        if sig is not None and sig == _cache["sig"]:
            return _cache["val"]
        try:
            ws = parse(_read_domains(db, ("repos", "worktreeMeta", "folderWorkspaces")))
            meta = {"found": True, "db": str(db), "count": len(ws)}
        except (sqlite3.Error, OSError) as e:
            ws, meta = [], {"found": True, "db": str(db), "error": str(e)}
        _cache["sig"], _cache["val"] = sig, (ws, meta)
        return _cache["val"]


def workspace_for(workspaces, cwd, branch=""):
    """Orca workspace a session ran in: deepest workspace path containing cwd,
    else (folder renamed since) the workspace whose current branch matches."""
    if not workspaces:
        return None
    cwd = (cwd or "").rstrip("/")
    best = None
    if cwd:
        for w in workspaces:
            if cwd == w["path"] or cwd.startswith(w["path"] + "/"):
                if best is None or len(w["path"]) > len(best["path"]):
                    best = w
    if best is None and branch:
        hits = [w for w in workspaces if w["branch"] and w["branch"] == branch]
        if len(hits) == 1:
            best = hits[0]
    return best
