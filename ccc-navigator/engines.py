"""Engine registry: one adapter per agent harness a session can live in (docs/OWNER-ENGINES.md).

The Navigator talks to whatever runs a project: Claude Code (through CCC), Pi
(resumed headlessly) and OpenCode (its CLI). Every adapter answers the same
three verbs, so the owner switcher and `deliver()` do not care which engine a
session belongs to:

    ls_sessions(ctx, limit)   -> rows for the picker: {"id", "title", "updated", "directory"}
    deliver(ctx, sid, text)   -> human short status ("sent", "queued", "failed: ...")
    transcript(ctx, sid)      -> {"updated": epoch_seconds|None, "messages": [...]} or None

Claude stays in App.deliver() for now: its "moved copy" guard and Devin's
delivery path live there and need CCC state. Everything else is registered
here, so adding a 4th engine means subclassing Engine and calling register().
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------- registry

_ENGINES = {}   # name -> Engine instance


class Engine:
    """A harness adapters must answer. Subclass and register() an instance."""

    name = ""

    # dir-based engines can own a folder project; "desktop:" repos cannot run an IDE harness
    dir_based = True

    def available(self, ctx=None):
        """Whether the engine's CLI exists on this machine."""
        raise NotImplementedError

    def ls_sessions(self, ctx=None, directory=None, limit=50):
        """Candidate sessions for the picker, newest first."""
        raise NotImplementedError

    def deliver(self, ctx, sid, text):
        """Send text into a running/stored session; -> short human status."""
        raise NotImplementedError

    def transcript(self, ctx, sid):
        """Best-effort liveness read: {"updated": ts, "messages": [...]} or None."""
        raise NotImplementedError


def register(engine):
    _ENGINES[engine.name] = engine


def get(name):
    return _ENGINES.get(str(name or "").strip().lower())


def names():
    return sorted(_ENGINES)


def _env():
    """The navigator's PATH plus the usual tool locations (engines install in ~/*)."""
    env = dict(os.environ)
    extra = [str(Path.home() / ".local/bin"), str(Path.home() / ".opencode/bin"),
             "/opt/homebrew/bin", "/usr/local/bin"]
    extra += sorted(_glob_nvm(), reverse=True)
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _glob_nvm():
    try:
        return [str(p) for p in Path.home().glob(".nvm/versions/node/*/bin")]
    except OSError:
        return []


def _run(argv, directory=None, timeout=20):
    """Run an engine CLI and parse its JSON output; -> (ok, data|error-string)."""
    try:
        r = subprocess.run(argv, cwd=directory or None, capture_output=True, timeout=timeout,
                           env=_env(), text=True)
    except FileNotFoundError:
        return False, "%s is not installed" % argv[0]
    except subprocess.TimeoutExpired:
        return False, "%s timed out after %ds" % (argv[0], timeout)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "exit %d" % r.returncode).strip()[:300]
    try:
        return True, json.loads(r.stdout or "null")
    except ValueError as e:
        return False, "bad JSON from %s: %s" % (argv[0], e)


# --------------------------------------------------------------------------- opencode

# `opencode session list --format json` rows carry epoch-ms stamps
class OpenCode(Engine):
    name = "opencode"

    def __init__(self, bin_name="opencode"):
        self.bin_name = os.environ.get("OPENCODE_BIN") or bin_name

    def available(self, ctx=None):
        return bool(shutil.which(self.bin_name, path=_env()["PATH"]))

    def ls_sessions(self, ctx=None, directory=None, limit=50):
        ok, data = _run([self.bin_name, "session", "list", "--format", "json", "-n", "200"],
                        directory=directory)
        if not ok:
            return [], data
        rows = data if isinstance(data, list) else []
        out = [{"id": r.get("id") or "", "title": r.get("title") or "",
                "updated": (r.get("updated") or 0) / 1000.0 or None,
                "directory": r.get("directory") or ""} for r in rows]
        if directory:     # session list spans every workspace of this OpenCode install
            wanted = str(directory).rstrip("/")
            out = [r for r in out if r["directory"].rstrip("/") == wanted]
        out.sort(key=lambda r: r["updated"] or 0, reverse=True)
        return out[:limit], None

    def deliver(self, ctx, sid, text):
        """`opencode run -s <id>` resumes the stored session headlessly. Detached, logged,
        one live run per session (two would interleave their turns). The run must start in the
        session's own workspace, so the directory comes from the session list lookup."""
        if not (sid and text):
            return "failed: session id and text are required"
        run_dir = Path(os.environ.get("CCC_NAV_ENGINE_RUNS", "~/.ccc-navigator/engine-runs")).expanduser()
        with _OC_RUNS_LOCK:
            for run in list(_OC_RUNS.values()):
                if run["sid"] == sid and run["proc"].poll() is None:
                    return "failed: an OpenCode run for this session is already in progress; try again in a minute"
        directory = self._sid_dir(sid)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = "%s-%d" % (sid[:20], int(time.time() * 1000))
        log = run_dir / (run_id + ".log")
        try:
            fh = log.open("w")
        except OSError as e:
            return "failed: %s" % e
        argv = [self.bin_name, "run", "--agent", os.environ.get("OPENCODE_AGENT") or "build", "-s", sid]
        model = os.environ.get("OPENCODE_MODEL")
        if model:
            argv[2:2] = ["-m", model]
        argv.append(str(text)[:20000])
        try:
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=directory)
        except FileNotFoundError:
            fh.close()
            return "failed: %s is not installed" % self.bin_name
        except OSError as e:
            fh.close()
            return "failed: %s" % e
        _OC_RUNS[run_id] = {"proc": proc, "log": str(log), "sid": sid, "started": time.time()}
        fh.close()
        return "sent"

    def _sid_dir(self, sid):
        """The workspace directory a session was opened in (send must run there)."""
        rows, _err = self.ls_sessions(limit=200)
        for r in rows:
            if r["id"] == sid:
                return r["directory"] or None
        return None

    def transcript(self, ctx, sid):
        ok, data = _run([self.bin_name, "session", "export", sid], timeout=30)
        if not ok or not isinstance(data, dict):
            return None
        msgs = []
        for m in data.get("messages") or []:
            info = m.get("info") if isinstance(m.get("info"), dict) else m
            t = ((m.get("time") or {}).get("created") or 0) / 1000.0 or None
            msgs.append({"t": t, "type": m.get("type") or info.get("role") or ""})
        msgs = [m for m in msgs if m["t"]]
        msgs.sort(key=lambda m: m["t"] or 0)
        return {"updated": msgs[-1]["t"] if msgs else None, "messages": msgs}


_OC_RUNS = {}
_OC_RUNS_LOCK = threading.Lock()
_SID_RE = re.compile(r"^ses_[A-Za-z0-9]{10,60}$")


def opencode_sid_ok(sid):
    return bool(_SID_RE.match(str(sid or "")))


# --------------------------------------------------------------------------- pi

class Pi(Engine):
    """Pi wraps ccc-navigator/pi_run.py: the adapter's verbs map 1:1 onto it."""

    name = "pi"

    def __init__(self, pi_run_module=None):
        # passed in lazily so engines.py stays importable without navigator's package layout
        self._pi_run = pi_run_module

    def _run_mod(self):
        if self._pi_run is None:
            import pi_run
            self._pi_run = pi_run
        return self._pi_run

    def available(self, ctx=None):
        return True     # pi_run.send reports a missing binary as a failed send

    def ls_sessions(self, ctx=None, directory=None, limit=50):
        pr = self._run_mod()
        from navigator import pi_sessions     # navigator owns the pi dir listing
        rows, _ = pi_sessions(getattr(getattr(ctx, "args", None), "pi_dir", None))
        out = [{"id": r.get("session_id") or "", "title": r.get("title") or r.get("name") or "",
                "updated": r.get("updated") or r.get("mtime") or None,
                "directory": r.get("cwd") or r.get("directory") or ""} for r in rows]
        if directory:
            wanted = str(directory).rstrip("/")
            out = [r for r in out if r["directory"].rstrip("/") == wanted]
        out.sort(key=lambda r: r["updated"] or 0, reverse=True)
        return out[:limit], None

    def deliver(self, ctx, sid, text):
        r = self._run_mod().send(getattr(getattr(ctx, "args", None), "pi_dir", None), sid, text)
        return "sent" if r.get("ok") else "failed: " + str(r.get("error"))

    def transcript(self, ctx, sid):
        from navigator import pi_transcript
        return pi_transcript(getattr(getattr(ctx, "args", None), "pi_dir", None), sid)


register(OpenCode())
register(Pi())
