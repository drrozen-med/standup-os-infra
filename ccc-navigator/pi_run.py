"""Send a message to a Pi session.

Pi has no remote-input channel, so a message is delivered the way Pi itself
supports: resume the session file non-interactively (`pi --session <file> -p
"<text>"`). The agent appends its turn to the same jsonl, which is what the
transcript reader already shows. Runs are detached, so a long turn never blocks
the navigator; each one logs to ~/.ccc-navigator/pi-runs/.

Pi's print mode runs its tools without approval prompts, the same as a message
sent to any other engine through CCC. A session that is open in a terminal gets
the message as a branch it will not see until reopened; the result carries a
warning when that is likely.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

RUN_DIR = Path(os.path.expanduser(os.environ.get("CCC_NAV_PI_RUNS", "~/.ccc-navigator/pi-runs")))
SID_RE = re.compile(r"^[0-9a-fA-F-]{8,40}$")
MAX_TEXT = 20000
_runs = {}  # run id -> {"proc", "log", "sid", "started"}
_pending_model = {}   # sid -> {"provider", "model"}: a model switch waiting for the current run to end
_lock = threading.Lock()


def _env():
    env = dict(os.environ)
    extra = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    extra += sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin")), reverse=True)
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def pi_bin():
    return os.environ.get("PI_BIN") or shutil.which("pi", path=_env()["PATH"])


def session_file(pi_dir, sid):
    root = Path(os.path.expanduser(str(pi_dir)))
    hits = sorted(root.glob("*/*_%s.jsonl" % sid))
    return hits[-1] if hits else None


def session_cwd(path):
    try:
        with open(path, encoding="utf-8") as fh:
            head = json.loads(fh.readline())
        return str(head.get("cwd") or "")
    except (OSError, ValueError):
        return ""


def _pi_open_in(cwd, exclude=()):
    """True when a pi process we did not start runs in this folder (probably a live terminal).
    Pi rewrites its process title to just "pi", so runs are told apart by pid, not arguments."""
    try:
        out = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines()[1:]:
        pid, _, cmd = line.strip().partition(" ")
        if not re.search(r"(^|/)pi( |$)|pi-coding-agent", cmd) or int(pid) in exclude:
            continue
        try:
            lsof = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if ("n" + cwd) in lsof.splitlines():
            return True
    return False


def send(pi_dir, sid, text):
    """Start a detached run. -> {"ok", "run", "pid", "log", "warning"?} or {"ok": False, "error"}."""
    sid = str(sid or "").strip()
    text = str(text or "").strip()
    if not SID_RE.match(sid):
        return {"ok": False, "error": "bad session id"}
    if not text:
        return {"ok": False, "error": "empty message"}
    if len(text) > MAX_TEXT:
        return {"ok": False, "error": "message too long (max %d characters)" % MAX_TEXT}
    f = session_file(pi_dir, sid)
    if not f:
        return {"ok": False, "error": "Pi session not found"}
    cwd = session_cwd(f)
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "error": "the session's folder no longer exists: %s" % (cwd or "unknown")}
    binary = pi_bin()
    if not binary:
        return {"ok": False, "error": "the pi command was not found (set PI_BIN)"}
    with _lock:
        for old, v in _runs.items():
            if v["sid"] == sid and v["proc"].poll() is None:
                # Two Pi processes on one session file interleave their turns.
                return {"ok": False, "error": "a Pi run for this session is already in progress"}
    pending = _apply_pending_model(pi_dir, sid)     # a model switch queued while a run was live
    if pending and not pending.get("ok"):
        return {"ok": False, "error": pending.get("error")}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    run_id = "%s-%d" % (sid[:8], int(time.time() * 1000))
    log = RUN_DIR / (run_id + ".log")
    fh = open(log, "wb")
    proc = subprocess.Popen([binary, "--session", str(f), "-p", text], cwd=cwd, env=_env(), stdin=subprocess.DEVNULL,
                            stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    fh.close()
    with _lock:
        _runs[run_id] = {"proc": proc, "log": str(log), "sid": sid, "started": time.time()}
        for old in [k for k, v in _runs.items() if v["proc"].poll() is not None and time.time() - v["started"] > 3600]:
            _runs.pop(old, None)
    out = {"ok": True, "run": run_id, "pid": proc.pid, "log": str(log)}
    with _lock:
        ours = {r["proc"].pid for r in _runs.values() if r["proc"].poll() is None}
    if _pi_open_in(cwd, ours):
        out["warning"] = "a Pi terminal is open in this folder; it will not show this exchange until reopened"
    return out


def status(run_id):
    with _lock:
        r = _runs.get(run_id)
    if not r:
        return {"ok": False, "error": "unknown run (the navigator restarted?)"}
    rc = r["proc"].poll()
    return {"ok": True, "run": run_id, "done": rc is not None, "rc": rc, "seconds": int(time.time() - r["started"]),
            "log": r["log"]}


def session_model(pi_dir, sid):
    """The model a Pi session resumes with: its last model_change entry, or None."""
    path = session_file(pi_dir, str(sid or "").strip())
    if not path:
        return None
    model = None
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
                if o.get("type") == "model_change":
                    model = (o.get("provider"), o.get("modelId"))
    except OSError:
        return None
    return model


def set_model(pi_dir, sid, provider, model):
    """Switch a Pi session's model without spending a turn.

    Pi keeps the active model as a model_change entry on the session's path
    (docs/session-format.md), which is what SessionManager.appendModelChange
    writes. The next resume of the session uses the model written here.

    While a run is live the session file is being written, so the switch is
    queued and applied before the next run starts.
    """
    sid = str(sid or "").strip()
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not SID_RE.match(sid):
        return {"ok": False, "error": "bad session id"}
    if not provider or not model:
        return {"ok": False, "error": "provider and model are required"}
    if not session_file(pi_dir, sid):
        return {"ok": False, "error": "Pi session not found"}
    with _lock:
        if _live_run(sid):
            _pending_model[sid] = {"provider": provider, "model": model}
            return {"ok": True, "pending": True, "provider": provider, "model": model,
                    "note": "a Pi run is in progress; the model switches when it finishes"}
    return _apply_model(pi_dir, sid, provider, model)


def _live_run(sid):
    """True when a Pi process is running for this session id."""
    for v in _runs.values():
        if v["sid"] == sid and v["proc"].poll() is None:
            return True
    return False


def _apply_model(pi_dir, sid, provider, model):
    """Append the model_change entry that makes the session resume on this model."""
    path = session_file(pi_dir, sid)
    if not path:
        return {"ok": False, "error": "Pi session not found"}
    leaf = None
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
                if o.get("type") != "session":
                    leaf = o.get("id") or leaf
    except OSError as e:
        return {"ok": False, "error": "read error: %s" % e}
    entry = {"type": "model_change", "id": uuid.uuid4().hex[:8], "parentId": leaf,
             "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "provider": provider, "modelId": model}
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:
        return {"ok": False, "error": "write error: %s" % e}
    return {"ok": True, "provider": provider, "model": model, "file": str(path), "entry": entry}


def _apply_pending_model(pi_dir, sid):
    """Apply a model switch that was queued while a run was live. -> the result or None."""
    with _lock:
        want = _pending_model.pop(sid, None)
    if not want:
        return None
    res = _apply_model(pi_dir, sid, want["provider"], want["model"])
    if not res.get("ok"):
        with _lock:
            _pending_model[sid] = want
    return res
