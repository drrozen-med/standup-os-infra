"""Send a message to an OpenCode session, and read its transcript.

CCC has an OpenCode adapter, but it reads the legacy `session` table of
OpenCode's SQLite store. Current OpenCode writes `session_v2` + `session_message`,
so CCC cannot see a live session at all: asked to deliver a message to one it
answers "could not derive repo context for session ...". That is why the
Navigator's co-pilot check-in could not reach an OpenCode co-pilot.

The Navigator therefore talks to OpenCode directly, the same way it already
talks to Pi (pi_run.py):

  * send: `opencode run --session <id> --auto "<text>"` resumes the session
    non-interactively - the same mechanism CCC uses when it does find a session.
    The run is detached, so a long turn never blocks the navigator, and each
    one logs to ~/.ccc-navigator/opencode-runs/.
  * read: the session's messages come straight out of OpenCode's own store, so
    `nav.py read opencode:<id>` shows the real transcript.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

RUN_DIR = Path(os.path.expanduser(os.environ.get("CCC_NAV_OPENCODE_RUNS", "~/.ccc-navigator/opencode-runs")))
DB_PATH = Path(os.path.expanduser(os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db")))
SID_RE = re.compile(r"^ses_[0-9a-zA-Z]{8,40}$")
MAX_TEXT = 20000
_runs = {}        # pid -> {"proc", "log", "sid", "started"}


def reap():
    """Collect the runs that finished, so they do not stay as zombies."""
    for pid in list(_runs):
        proc = _runs[pid].get("proc")
        if proc is not None and proc.poll() is not None:
            _runs.pop(pid, None)


def opencode_bin():
    """The opencode executable, or None."""
    env = os.environ.get("OPENCODE_BIN")
    if env:
        return env
    for cand in (str(Path.home() / ".opencode/bin/opencode"), "/opt/homebrew/bin/opencode"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("opencode")


def connect():
    """A read-only connection to OpenCode's store, or None."""
    if not DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _table(con, name):
    """True when the store has this table."""
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def session_row(sid):
    """The session's row from OpenCode's store: {id, directory, title, ...} or None.

    Current OpenCode keeps sessions in `session_v2`; older ones in `session`.
    Both are read so a session started either way still works.
    """
    con = connect()
    if con is None:
        return None
    try:
        for table in ("session_v2", "session"):
            if not _table(con, table):
                continue
            cols = {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}
            pick = [c for c in ("id", "directory", "title", "time_created", "time_updated", "agent", "model") if c in cols]
            if not pick:
                continue
            row = con.execute("SELECT %s FROM %s WHERE id=?" % (", ".join(pick), table), (sid,)).fetchone()
            if row:
                return dict(row)
        return None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def session_cwd(sid):
    """The folder the session runs in, or ""."""
    row = session_row(sid) or {}
    return str(row.get("directory") or "")


def known(sid):
    """True when OpenCode's store knows this session."""
    return session_row(sid) is not None


def transcript(sid, after_seq=0):
    """The session's messages, oldest first: [{"role", "text", "seq", "time"}]."""
    con = connect()
    if con is None:
        return []
    out = []
    try:
        if not _table(con, "session_message"):
            return []
        rows = con.execute(
            "SELECT id, type, seq, time_created, data FROM session_message "
            "WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
        for r in rows:
            seq = r["seq"] or 0
            if after_seq and seq <= after_seq:
                continue
            try:
                d = json.loads(r["data"] or "{}")
            except ValueError:
                d = {}
            role = str(r["type"] or d.get("role") or "").lower()
            if role not in ("user", "assistant"):
                continue
            texts = []
            # a user message keeps its text at the top level; an assistant message
            # keeps its text in the content parts.
            if isinstance(d.get("text"), str) and d["text"].strip():
                texts.append(d["text"])
            for part in (d.get("content") or []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
            text = "\n".join(texts).strip()
            if not text:
                continue
            out.append({"role": role, "text": text, "seq": seq,
                        "time": r["time_created"] or 0, "id": r["id"]})
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def events(sid):
    """Transcript in the shape the Navigator's chat pane expects.

    Same shape as CCC's conversation events, so every reader works unchanged:
    `type` is the role, `text` is the message, `blocks` carries the text part,
    and `ts` is when it happened (nav.py's flatten_events and the co-pilot
    check-in both read those).
    """
    out = []
    for m in transcript(sid):
        ts = None
        if m.get("time"):
            try:
                ts = datetime.datetime.fromtimestamp(m["time"] / 1000.0,
                                                     datetime.timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                ts = None
        out.append({"type": m["role"], "role": m["role"], "text": m["text"],
                    "blocks": [{"kind": "text", "text": m["text"]}],
                    "ts": ts, "seq": m["seq"]})
    return out


def _run_dir():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


def send(sid, text, timeout=0):
    """Deliver text to an OpenCode session by resuming it headlessly.

    `opencode run --session <id> --auto "<text>"` continues the session with
    its whole history; the new turn lands in the same store the transcript
    reader reads. The run is detached (the caller gets "queued" at once) and
    its output is logged, so a long turn never blocks the navigator.
    """
    text = (text or "").strip()
    if not sid or not text:
        return {"ok": False, "error": "missing sid or text"}
    if not SID_RE.match(str(sid)):
        return {"ok": False, "error": "not an opencode session id"}
    binary = opencode_bin()
    if not binary:
        return {"ok": False, "error": "opencode binary not found"}
    cwd = session_cwd(sid)
    if not cwd or not Path(cwd).is_dir():
        cwd = str(Path.home())
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = _run_dir() / ("%s-%s.log" % (stamp, str(sid)[-8:]))
    logf = open(log_path, "ab", buffering=0)
    cmd = [binary, "run", "--session", str(sid), "--auto", text[:MAX_TEXT]]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        logf.close()
        return {"ok": False, "error": str(e)}
    reap()
    _runs[proc.pid] = {"proc": proc, "log": str(log_path), "sid": sid, "started": time.time()}
    return {"ok": True, "queued": True, "pid": proc.pid, "engine": "opencode",
            "sid": sid, "cwd": cwd, "log": str(log_path),
            "started_at": time.time(),
            "warning": ("a live opencode terminal in this folder will not show the exchange "
                        "until it is reopened") if _live_terminal(cwd) else None}


def _live_terminal(cwd):
    """True when an opencode process runs in this folder (probably an open tab)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines()[1:]:
        if "opencode" not in line or "ps -axo" in line:
            continue
        pid = line.strip().split(None, 1)[0]
        if not pid.isdigit():
            continue
        try:
            res = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if any(p == cwd for p in (l[1:] for l in res.splitlines() if l.startswith("n"))):
            return True
    return False


def run_status(run_id):
    """State of one run: {state: running|done|failed, log, pid}."""
    log_path = _run_dir() / str(run_id)
    if not log_path.exists():
        return {"ok": False, "error": "no such run"}
    return {"ok": True, "log": str(log_path),
            "size": log_path.stat().st_size,
            "mtime": log_path.stat().st_mtime}
