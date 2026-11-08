#!/usr/bin/env python3
"""Supervisor (masterplan #79, phase 2; contract: docs/supervisor.md).

Keeps every project owner moving and keeps Uri's view current. Routine work is code; judgment
stays with the AI supervisor (the Super-co-pilot), which reads digest.md and the register.

  supervisor.py cycle [--dry-run]              one 15-minute cycle (launchd com.standup-os.supervisor)
  supervisor.py status [--json]                the register as a table (JSON: what GET /api/supervisor returns)
  supervisor.py wake PROJECT [--reason TEXT]   manual wake, same caps as the cycle
  supervisor.py pause [PROJECT]                pause supervision (everything, or one project)
  supervisor.py resume [PROJECT]               resume it

One cycle: refresh the scoreboards, write digest.json + digest.md, wake owners by rule (answered,
no-status, blocked-due, keep-moving; first match wins), raise loud alerts for the few decisions that
need Uri now (P1, at/past deadline, or blocking an owner on Uri past its date; at most one per decision),
rewrite register.json, keep state.json and append one line to cycles.log, all under SUPERVISOR_DIR
(default ~/.ccc-navigator/supervisor).
Skips 00:00-07:00 local and while paused. A failing owner never stops the others; the cycle always
exits 0 after logging the error.

SUPERVISOR_DRY_RUN=1 (or --dry-run) decides and logs wakes but sends nothing, appends nothing to any
owner's log.md, does not write scoreboards (they are computed in memory) and does not save state.json.
Env: SUPERVISOR_DIR, OWNER_DIR, CCC_NAV_URL, CCC_NAV_ANSWERS.
"""
import argparse
import contextlib
import datetime
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import owner  # noqa: E402  (bin/owner.py: slug rule, owner folders, atomic write, log.md)

NAV = os.path.join(os.path.dirname(HERE), "ccc-navigator", "nav.py")
OWNER_PY = "~/projects/standup-os/bin/owner.py"
QUIET = (0, 7)          # local hours [start, end): no cycle
WORKING_MIN = 3         # activity this recent means the owner is working now: never wake
IDLE_MIN = 30           # keep-moving: a working / picking-next owner idle this long
BUSY_MAX_IDLE_MIN = 90   # a tool running longer than this with a silent transcript is treated as hung, not busy
WAIT_IDLE_MIN = 120     # keep-moving: a waiting-on-uri owner idle this long (waiting on Uri never stops the rest of the backlog)
GAP_MIN = 30            # never wake an owner woken this recently
MAX_WAKES = 24          # per owner per local day
DECISION_OLD_DAYS = 7   # flag decision-7d
LOG_TAIL = 5            # log.md lines in the digest
LOG_CHARS = 240         # digest.md cuts longer log lines (digest.json keeps them whole)
CYCLES_TAIL = 20        # cycles.log lines in `status --json` / GET /api/supervisor
URI_ACTORS = ("you", "uri", "")
REASONS = ("answered", "no-status", "blocked-due", "keep-moving")
ALERT_REARM = 12 * 3600 # an acked loud alert may come back this long after its ack (same rule as navigator alerts.py)
ALERT_Q_CHARS = 90      # question text in an alert title
ALERT_P1_AGE = 24 * 3600   # a P1 decision gets loud only after it has waited this long (owners mark many things P1)
ALERT_MAX_NEW = 1       # new loud alerts raised per cycle, so the first live cycle is not a wall of chimes

ENGINES = ("claude", "opencode", "devin")
ENGINE_DEFAULT = "claude"
HUNG_MIN = 90           # a non-claude job running longer than this is treated as hung and cancelled
LAUNCH_FAIL_ALERT = 3   # consecutive Outsourcerer launch failures before one loud alert
OSRC_CACHE_GLOB = os.path.expanduser("~/.claude/plugins/cache/outsourcerer/outsourcerer/*/skills/outsourcerer/scripts/outsourcerer.sh")
ENGINE_CONSTRAINT = (
    "Never edit, commit in, or switch branches of the main checkout; work in your own git worktree. "
    "You are one turn: do the work, then run owner.py status set, then stop. "
    "Use only Outsourcerer-delegable tools; never spend money; messages to real people are Uri's."
)
ENGINE_CMD = {
    "opencode": ["--provider", "opencode", "bg", "edit", "-m", "opencode-go/iceman-contributor-free"],
    "devin": ["--provider", "devin", "bg", "-m", "swe-2-high"],
}


def sup_dir():
    return os.environ.get("SUPERVISOR_DIR") or os.path.expanduser("~/.ccc-navigator/supervisor")


def path(name):
    return os.path.join(sup_dir(), name)


def nav_url():
    return os.environ.get("CCC_NAV_URL", "http://127.0.0.1:8095")


def actor():
    """Who asked for a pause/resume/manual wake: $NAV_ACTOR (the Navigator passes the request's actor)."""
    return os.environ.get("NAV_ACTOR") or "cli"


def dry_env():
    return os.environ.get("SUPERVISOR_DRY_RUN", "") not in ("", "0")



# ---------------------------------------------------------------- engines (Outsourcerer; spec ENGINE-SPEC.md)

def engine_of(slug):
    """Per-owner engine: SUPERVISOR_ENGINE overrides all (tests), else owners/<slug>/engine, else claude."""
    override = os.environ.get("SUPERVISOR_ENGINE", "").strip()
    if override:
        return override if override in ENGINES else ENGINE_DEFAULT
    try:
        with open(os.path.join(owner.folder(slug), "engine")) as f:
            eng = f.read().strip().split()[0]
        return eng if eng in ENGINES else ENGINE_DEFAULT
    except (OSError, IndexError):
        return ENGINE_DEFAULT


def set_engine_value(slug, eng):
    """Write owners/<slug>/engine (one line). eng in ENGINES."""
    if eng not in ENGINES:
        raise ValueError("engine must be one of: %s" % ", ".join(ENGINES))
    d = owner.folder(slug)
    os.makedirs(d, exist_ok=True)
    owner.write_atomic(os.path.join(d, "engine"), eng + "\n")
    return eng


def find_outsourcerer():
    """Latest outsourcerer.sh from the plugin cache glob, or None."""
    import glob
    hits = sorted(glob.glob(OSRC_CACHE_GLOB))
    return hits[-1] if hits else None


def osrc_run(args, full=False, timeout=120):
    """Run outsourcerer.sh with args -> (rc, output)."""
    sh = find_outsourcerer()
    if not sh:
        return 127, "outsourcerer.sh not found (%s)" % OSRC_CACHE_GLOB
    try:
        r = subprocess.run([sh] + list(args), capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, OSRC_REQUIRE_INTERACTIVE="0"))
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    out = (r.stdout or r.stderr or "").strip()
    return r.returncode, out if full else out[:400].replace("\n", " ")


# Status tokens: checked as whole tokens after normalizing punctuation around them.
_JOB_STATUS_TOKENS = ("permission-blocked", "done?", "canceled", "cancelled", "interrupted",
                      "running", "queued", "pending", "blocked", "failed", "timeout", "wedged", "done")
_JOB_STATUS_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _JOB_STATUS_TOKENS) + r")\b", re.I)

# Canonical terminal set (includes aliases as they may appear in stored/parsed state).
JOB_TERMINAL = {"done", "done?", "failed", "blocked", "timeout", "wedged", "canceled",
                "cancelled", "permission-blocked", "interrupted"}


def _norm_job_status(st):
    if st is None:
        return None
    s = str(st).strip().lower()
    if not s:
        return None
    if s == "cancelled":
        return "canceled"
    return s


def parse_job_status_output(out, json_ok=False):
    """Parse Outsourcerer status output reliably.

    When json_ok is True, `out` is from `status --json` and may be a JSON object or
    a JSON-encoded scalar/string; non-JSON is still scanned for a known status token.
    Returns a normalized status string, or None when unknown/ambiguous.
    """
    if out is None:
        return None
    text = out if isinstance(out, str) else str(out)
    text = text.strip()
    if not text:
        return None
    if json_ok:
        try:
            j = json.loads(text)
        except ValueError:
            j = None
        if isinstance(j, str):
            st = _norm_job_status(j)
            return st if st else None
        if isinstance(j, dict):
            st = j.get("status") or j.get("state")
            if st is None and isinstance(j.get("job"), dict):
                st = j["job"].get("status") or j["job"].get("state")
            st = _norm_job_status(st)
            if st:
                return st
            # JSON object but no status field: unknown, do not guess from prose.
            return None
        if j is not None:
            # JSON array/number/etc: unknown.
            return None
    m = _JOB_STATUS_RE.search(text)
    if m:
        return _norm_job_status(m.group(1))
    return None


def job_status(job_id, osrc=None):
    """Outsourcerer job status string, or None if unknown.

    Issues exactly one status query per call (prefers `status --json <id>`). Unknown
    or unparsable output fails closed to None so callers never treat it as launchable.
    """
    if not job_id:
        return None
    if osrc is None:
        sh = find_outsourcerer()
        if not sh:
            return None
        runner = lambda a: osrc_run(a, full=True)
    else:
        runner = osrc
    try:
        rc, out = runner(["status", "--json", str(job_id)])
    except Exception:
        return None
    # One query only: parse JSON output when the command ran; unknown => fail closed.
    # (Do not fall back to a second text query.)
    if rc != 0 and not (out and str(out).strip()):
        return None
    return parse_job_status_output(out, json_ok=True)


def job_running(job_id, osrc=None):
    st = job_status(job_id, osrc)
    if st is None:
        return None          # unknown: fail closed (not "not running")
    return st not in JOB_TERMINAL


def is_terminal_status(st):
    n = _norm_job_status(st)
    return bool(n) and n in JOB_TERMINAL


def cancel_job(job_id, osrc=None):
    """Cancel an Outsourcerer job. -> (ok, output). Never raises on runner errors."""
    if not job_id:
        return False, "no job id"
    if osrc is None:
        runner = lambda a: osrc_run(a)
    else:
        runner = osrc
    try:
        rc, out = runner(["cancel", str(job_id)])
    except Exception as e:
        return False, str(e)
    ok = (rc == 0)
    return ok, out


def brief_text(slug):
    """owner.py brief output for the slug (best effort)."""
    try:
        return owner.brief(slug)
    except Exception as e:
        return "(owner brief unavailable: %s)" % e



# ---------------------------------------------------------------- io (mocked in tests)

def get(p):
    with urllib.request.urlopen(nav_url() + p, timeout=60) as r:
        return json.load(r)


def nav(*args, full=False):
    """Run nav.py as the supervisor -> (exit code, first 200 chars of its output; all of it with full)."""
    r = subprocess.run([sys.executable, NAV] + list(args), capture_output=True, text=True, timeout=90,
                       env=dict(os.environ, NAV_ACTOR="supervisor"))
    out = (r.stdout or r.stderr or "").strip()
    return r.returncode, out if full else out[:200].replace("\n", " ")


def write_json(p, obj):
    owner.write_atomic(p, json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def load_state():
    st = owner.read_json(path("state.json"))
    return st if isinstance(st, dict) else {}


@contextlib.contextmanager
def state_lock():
    """Serialises read-modify-write of state.json between a cycle, pause/resume and a manual wake."""
    os.makedirs(sup_dir(), exist_ok=True)
    try:
        import fcntl
    except ImportError:         # not on macOS/Linux: no lock
        yield
        return
    with open(path("state.json.lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log_cycle(line, now=None):
    os.makedirs(sup_dir(), exist_ok=True)
    with open(path("cycles.log"), "a") as f:
        f.write("%s %s\n" % (iso(now or time.time()), " ".join(str(line).split())))


def cycles_tail(n=CYCLES_TAIL):
    try:
        with open(path("cycles.log")) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()][-n:]
    except OSError:
        return []


# ---------------------------------------------------------------- time

def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def epoch(text):
    try:
        return datetime.datetime.fromisoformat(str(text or "")).timestamp()
    except ValueError:
        return None


def today(now):
    return datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def quiet(now):
    return QUIET[0] <= datetime.datetime.fromtimestamp(now).hour < QUIET[1]


def ago(ts, now):
    """'42 min ago' / '3.5 h ago' / '2.1 d ago'."""
    m = max(0.0, (now - ts) / 60)
    if m < 90:
        return "%d min ago" % m
    if m < 48 * 60:
        return "%.1f h ago" % (m / 60)
    return "%.1f d ago" % (m / 1440)


# ---------------------------------------------------------------- inputs

def load_board_module():
    loader = importlib.machinery.SourceFileLoader("owner_scoreboard", os.path.join(HERE, "owner-scoreboard.py"))
    spec = importlib.util.spec_from_loader("owner_scoreboard", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)     # reads OWNER_DIR / WATCHTOWER_GITHUB_JSON now
    return mod


def refresh_scoreboards(dry):
    """Step 1. -> {repo: score row}. Dry run computes in memory and writes nothing."""
    mod = load_board_module()
    b = mod.board()
    if not dry:
        for s in b["repos"]:
            mod.write_owner(s, b["fetched"])
    return {s["repo"]: s for s in b["repos"]}


def load_answers(dec):
    """{id: answer}: the Navigator's recent answers, completed from answers.json (it lists only 15)."""
    out = {str(x.get("id") or ""): x.get("answer") or {} for x in dec.get("answered") or []}
    p = os.environ.get("CCC_NAV_ANSWERS") or os.path.expanduser("~/.ccc-navigator/answers.json")
    a = owner.read_json(p)
    if isinstance(a, dict):
        out.update({str(k): v for k, v in a.items() if isinstance(v, dict)})
    return out


def find_item(tree, key):
    """The tree's session item for `key` (anywhere under repos/groups/items), or None."""
    stack = list(tree.get("repos") or [])
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if o.get("key") == key and "engine" in o:
                return o
            stack.extend(v for v in o.values() if isinstance(v, (list, dict)))
        elif isinstance(o, list):
            stack.extend(o)
    return None


def live_of(item, now):
    """Owner session live state from its Navigator tree item: working now / idle minutes / last activity."""
    if not item:
        return {"found": False, "state": "", "live": False, "last_activity": None, "idle_min": None,
                "working_now": False}
    mt = item.get("mtime")
    idle = round((now - float(mt)) / 60, 1) if isinstance(mt, (int, float)) else None
    return {"found": True, "state": item.get("state") or "", "live": bool(item.get("live")),
            "last_activity": iso(float(mt)) if idle is not None else None, "idle_min": idle,
            "working_now": idle is not None and idle < WORKING_MIN}


SHELLS = ("/bin/zsh", "/bin/bash", "zsh", "bash", "sh ")


def session_busy(sid, ps_out):
    """True when a process holding session `sid` has a child process, i.e. a tool is running right now.
    ps_out: `ps -axo pid=,ppid=,command=`. A persistent idle session process (waiting for its next message)
    has no children, so it does not count as busy; a turn waiting on npm, a test run or a sub-reviewer does."""
    rows = []
    for line in ps_out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    holders = {pid for pid, _, cmd in rows if sid in cmd and not cmd.startswith(SHELLS)}
    return any(ppid in holders for _, ppid, _ in rows)


def process_running(key):
    """True when the owner's session is mid-turn on a long tool (see session_busy). A long turn waiting on a
    sub-process writes nothing to its transcript, so the transcript age alone made it look idle and a wake
    started a second copy of the same session."""
    sid = str(key or "").split(":", 1)[-1]
    if len(sid) < 8:
        return False
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and session_busy(sid, out.stdout)


# ---------------------------------------------------------------- one owner

def observe(pkey, info, tree, answers, open_map, board_rows, ps, now, dry=False, osrc=None):
    """Digest entry for one owner; updates ps (its row in state.json) with the observation:
    idle_checkins, seen_updated_at, last_shipped, and (opencode/devin) hung-job cancellation."""
    slug = owner.slug_of(pkey)
    key = (info or {}).get("key") or ""
    eng = engine_of(slug)
    status = owner.read_json(os.path.join(owner.folder(slug), "status.json"))
    status = status if isinstance(status, dict) else None
    upd = (status or {}).get("updated_at")
    upd_ts = epoch(upd)
    live = live_of(find_item(tree, key), now)
    job = {"job_id": ps.get("job_id"), "started_at": ps.get("started_at"), "engine": ps.get("engine") or eng,
           "running": False, "hung": False, "status": None, "unknown": False, "terminal": False,
           "cancel_failed": False}
    if eng in ("opencode", "devin") and job["job_id"]:
        if osrc is None:
            osrc = osrc_run
        # Exactly one status query per observation.
        st = job_status(job["job_id"], osrc)
        job["status"] = st
        if st is None:
            # FAIL CLOSED: unknown status means we cannot prove it is safe to launch another job.
            job["unknown"] = True
            job["running"] = False
            job["terminal"] = False
        else:
            nst = _norm_job_status(st)
            job["status"] = nst
            job["terminal"] = is_terminal_status(nst)
            job["running"] = not job["terminal"]
        started = job.get("started_at")
        if job["running"] and isinstance(started, (int, float)) and (now - started) >= HUNG_MIN * 60:
            job["hung"] = True
            if not dry:
                ok, cerr = cancel_job(job["job_id"], osrc)
                # Support mocks that return (rc, out).
                if not isinstance(ok, bool):
                    try:
                        ok = (int(ok) == 0)
                    except (TypeError, ValueError):
                        ok = bool(ok)
                if ok:
                    ps["last_job"] = {"job_id": job["job_id"], "engine": eng, "ended": "hung-canceled", "when": iso(now)}
                    ps.pop("job_id", None)
                    ps.pop("started_at", None)
                    job["running"] = False
                    job["terminal"] = True
                    job["status"] = "canceled"
                else:
                    # FAIL CLOSED: cancellation did not succeed; keep tracking the job and do not launch another.
                    job["cancel_failed"] = True
                    job["error"] = str(cerr or "cancel failed")
        # A confirmed terminal job releases tracking (preserve last_job record).
        if job["terminal"] and not job.get("cancel_failed") and ps.get("job_id") == job["job_id"]:
            ps["last_job"] = {"job_id": job["job_id"], "engine": eng, "ended": job["status"] or "terminal", "when": iso(now)}
            ps.pop("job_id", None)
            ps.pop("started_at", None)
    if eng == "claude":
        if (live["found"] and not live["working_now"] and (live["idle_min"] or 0) < BUSY_MAX_IDLE_MIN
                and process_running(key)):
            live["working_now"] = True          # mid-turn: a process holds the session, never wake it now
    else:
        # Any tracked non-claude job that is not confirmed terminal blocks (fail closed).
        if job["running"] or job.get("unknown") or job.get("cancel_failed") or (ps.get("job_id") and not job["terminal"]):
            live["working_now"] = True

    # idle check-ins: consecutive cycles with nothing new in status (held while the session is mid-turn)
    first = "seen_updated_at" not in ps
    if first or upd != ps.get("seen_updated_at"):
        ps["idle_checkins"] = 0
        if status and status.get("shipped") and upd:
            ps["last_shipped"] = {"text": "; ".join(str(x) for x in status["shipped"]), "when": upd}
    elif not live["working_now"]:
        ps["idle_checkins"] = int(ps.get("idle_checkins") or 0) + 1
    ps["seen_updated_at"] = upd

    decs = []
    for did in (status or {}).get("needs_uri") or []:
        did = str(did)
        d = {"id": did, "status": "unknown", "question": "", "deadline": None, "asked_at": None,
             "answered_after_status": False, "answer": None}
        if did in open_map:
            o = open_map[did]
            d.update(status="open", question=str(o.get("question") or ""), deadline=o.get("deadline"),
                     asked_at=o.get("asked_at"), priority=o.get("priority"))
        elif did in answers:
            a = answers[did]
            d.update(status="answered", question=str(a.get("question") or ""), answer=a,
                     answered_after_status=upd_ts is not None and float(a.get("ts") or 0) > upd_ts)
        decs.append(d)

    sb = board_rows.get(slug)
    if sb is None:
        sb = owner.read_json(os.path.join(owner.folder(slug), "scoreboard.json")) or {}
    return {
        "project": slug, "project_key": pkey, "owner_key": key, "engine": eng, "job": job, "live": live,
        "status": None if not status else {k: status.get(k) for k in
                                           ("state", "summary", "doing", "next", "shipped", "needs_uri",
                                            "blocked", "updated_at")},
        "stale": bool((info or {}).get("stale")) if info else True,
        "problem": (info or {}).get("problem") or ("" if status else "no status yet"),
        "scoreboard_problems": list(sb.get("problems") or []),
        "log_tail": owner.log_tail(slug, LOG_TAIL),
        "decisions": decs,
    }


def new_answers(entry, ps):
    """Decisions in needs_uri answered after the owner's last status and not yet passed on by a wake."""
    sent = ps.get("answered_sent") or {}
    return [d for d in entry["decisions"] if d["answered_after_status"]
            and sent.get(d["id"]) != (d["answer"] or {}).get("ts")]


def blocked_by(entry):
    b = (entry["status"] or {}).get("blocked") or {}
    return str(b.get("by") or "") if isinstance(b, dict) else ""


def wake_reason(entry, ps, now):
    """First matching wake rule, or None."""
    st = entry["status"]
    state = (st or {}).get("state")
    if new_answers(entry, ps):
        return "answered"
    if not st or entry["problem"] in ("no status this turn", "no status yet"):
        return "no-status"
    by = blocked_by(entry)
    if state == "blocked" and by and by <= today(now):
        return "blocked-due"
    idle = entry["live"]["idle_min"]
    if state in ("working", "picking-next") and idle is not None and idle >= IDLE_MIN:
        return "keep-moving"
    if state == "waiting-on-uri" and idle is not None and idle >= WAIT_IDLE_MIN:
        return "keep-moving"
    return None


def wakes_today(ps, now):
    w = ps.get("wakes") or {}
    return int(w.get("count") or 0) if w.get("day") == today(now) else 0


def cap_reason(ps, now, paused):
    """The caps every wake obeys, the manual one too."""
    if paused:
        return "paused"
    last = (ps.get("last_wake") or {}).get("ts")
    if isinstance(last, (int, float)) and now - last < GAP_MIN * 60:
        return "woken %d min ago" % ((now - last) // 60)
    if wakes_today(ps, now) >= MAX_WAKES:
        return "%d wakes today" % MAX_WAKES
    return None


def skip_reason(entry, reason, ps, now, paused):
    """Why a due wake must not happen (the never-wake rules), or None."""
    if paused:
        return "paused"
    eng = entry.get("engine") or engine_of(entry["project"])
    if eng == "claude":
        if not entry["owner_key"]:
            return "no owner session"
        if not entry["live"]["found"]:
            return "owner session not in the Navigator"
    else:
        job = entry.get("job") or {}
        tracked = job.get("job_id") or ps.get("job_id")
        # FAIL CLOSED: one job per owner. If a job is tracked and not confirmed terminal,
        # or its status is unknown / cancellation failed, never launch another.
        if tracked and not job.get("terminal"):
            if job.get("unknown"):
                return "job status unknown (%s)" % tracked
            if job.get("cancel_failed"):
                return "job cancel failed (%s)" % tracked
            return "job running (%s)" % tracked
        if tracked and job.get("terminal"):
            pass
        elif ps.get("job_id") and not job.get("terminal"):
            # observation did not prove terminal; keep the durable id and block.
            return "job status unknown (%s)" % ps.get("job_id")
    if entry["live"]["working_now"]:
        return "working now"
    if (entry["status"] or {}).get("state") == "waiting-on-uri" and reason not in ("answered", "keep-moving"):
        return "waiting on Uri, nothing newly answered"
    return cap_reason(ps, now, False)


def wake_text(entry, reason, ps, now, note=""):
    slug, st = entry["project"], entry["status"] or {}
    cmd = "python3 %s status set %s --session %s --json -" % (OWNER_PY, slug, entry["owner_key"])
    head = "[Supervisor wake: %s]" % reason
    if reason == "answered":
        lines = [head + " Uri's answers to your decisions came in after your last status. Act on them now:"]
        for d in new_answers(entry, ps):
            a = d["answer"] or {}
            who = "Uri" if str(a.get("by") or "") in URI_ACTORS else str(a.get("by"))
            lines.append("- %s (answered by %s): question: %s" % (d["id"], who, d["question"]))
            lines.append("  choice: %s" % a.get("choice", ""))
            lines.append("  note: %s" % (a.get("note") or "(none)"))
        lines.append("Apply each answer, drop answered ids from needs_uri and log what you did.")
        body = "\n".join(lines)
    elif reason == "no-status":
        body = (head + " Your last turn ended without a status update%s. Write it now: what you did, what is "
                "next. If you have nothing in progress, take the next item of your backlog toward the goals "
                "first." % ("" if st else " (you have no status yet: read `python3 %s brief %s`)" % (OWNER_PY, slug)))
    elif reason == "blocked-due":
        b = st.get("blocked") or {}
        body = (head + " You are blocked on \"%s\" with unblock date %s, which is due. Try again now with a way "
                "you have not tried yet (tried: %s), or escalate: ask the supervisor, or turn it into ONE decision "
                "for Uri (`nav.py ask`) with options, a deadline and a default."
                % (b.get("what", ""), b.get("by", ""), "; ".join(b.get("tried") or []) or "-"))
    elif reason == "keep-moving" and st.get("state") == "waiting-on-uri":
        body = (head + " You have been waiting on Uri for %d min (%s). Waiting on him for those does not stop the "
                "rest of your backlog: do the next item that does not need his answer now. Use waiting-on-uri only "
                "when nothing else can move; otherwise set state working and keep the ids in needs_uri."
                % (entry["live"]["idle_min"] or 0, ", ".join(st.get("needs_uri") or []) or "-"))
    elif reason == "keep-moving":
        body = (head + " Your session has been idle %d min while your state is %s. Your next action was: \"%s\". "
                "Do it now, or take the next item of your backlog toward the goals."
                % (entry["live"]["idle_min"] or 0, st.get("state"), st.get("next") or "-"))
    else:   # manual
        body = head + " " + (note or "Uri asked for a check-in: move your next item forward and report.")
    return "%s\nWhen done: %s. End with owner.py status set" % (body, cmd)


def _is_safe_project_cwd(path):
    """Reject supervisor repo/cwd as an accidental target; require an actual directory."""
    if not path or not os.path.isdir(path):
        return False
    try:
        real = os.path.realpath(path)
        sup_here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
        sup_repo = os.path.realpath(os.path.join(sup_here, ".."))
        proc = os.path.realpath(os.getcwd())
        if real in (sup_here, sup_repo, proc):
            # Allow only if it's explicitly a ~/projects/* style project dir (not the engine repo).
            home_proj = os.path.realpath(os.path.expanduser("~/projects"))
            if not (real == home_proj or real.startswith(home_proj + os.sep)):
                return False
    except Exception:
        return False
    return True


def project_cwd(entry):
    """Resolve cwd for an Outsourcerer wake. FAIL CLOSED if unresolved.

    -> (path, None) or (None, error). Never falls back to the supervisor process/repo cwd.
    """
    slug = entry.get("project") or ""
    tried = []
    candidates = []
    if slug:
        candidates.append(os.path.expanduser("~/projects/%s" % slug))
    pk = entry.get("project_key") or ""
    if pk:
        candidates.append(pk)
        # common: slug from project_key basename
        base = os.path.basename(os.path.normpath(pk))
        if base and base != slug:
            candidates.append(os.path.expanduser("~/projects/%s" % base))
    for c in candidates:
        if c in tried:
            continue
        tried.append(c)
        if _is_safe_project_cwd(c):
            return c, None
    return None, "project cwd unresolved for %s (tried: %s)" % (slug or pk or "?", ", ".join(tried) or "-")


def engine_prompt(entry, reason, ps, now, note=""):
    slug = entry["project"]
    return "%s\n\n%s\n\n%s\n" % (brief_text(slug), wake_text(entry, reason, ps, now, note), ENGINE_CONSTRAINT)


def parse_job_id(text):
    """Extract an Outsourcerer job id from bg output (first plausible token/line)."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith(">>>"):
            continue
        # common forms: a bare id, or "job <id>", or json
        if line.startswith("{"):
            try:
                j = json.loads(line)
                if isinstance(j, dict):
                    v = j.get("id") or j.get("job_id") or j.get("job")
                    if v:
                        return str(v)
            except ValueError:
                pass
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,}", line):
            return line
        m = re.search(r"\bjob\s+([A-Za-z0-9][A-Za-z0-9._:-]{2,})\b", line, re.I)
        if m:
            return m.group(1)
    # last resort: first token-like id anywhere
    m = re.search(r"\b([0-9a-f]{8,}|job[_-][A-Za-z0-9._-]{3,}|[A-Za-z0-9]+-[A-Za-z0-9._-]{3,})\b", text)
    return m.group(1) if m else None


def note_launch_failure(entry, ps, now, err):
    """Record a launch failure; on the 3rd consecutive, raise one loud alert."""
    slug = entry["project"]
    n = int(ps.get("launch_failures") or 0) + 1
    ps["launch_failures"] = n
    msg = "Outsourcerer launch failed (%d/%d) for %s: %s" % (n, LAUNCH_FAIL_ALERT, slug, err)
    try:
        log_cycle(msg, now)
    except OSError:
        pass
    try:
        owner.append_log(slug, "supervisor", "Outsourcerer launch failed (%d/%d): %s" % (n, LAUNCH_FAIL_ALERT, err))
    except (OSError, ValueError):
        pass
    if n >= LAUNCH_FAIL_ALERT:
        try:
            nav("alert", "raise", "--title", "Supervisor: %s Outsourcerer launch failing" % slug,
                "--body", "%s (consecutive failures)" % msg,
                "--key", "supervisor-launch-fail:%s" % slug, full=True)
        except Exception:
            pass
    return msg


def do_wake(entry, reason, ps, now, dry, note=""):
    """Send/launch one wake (unless dry) and record it in ps and log.md. -> (sent ok, short output)."""
    eng = entry.get("engine") or engine_of(entry["project"])
    if eng in ("opencode", "devin"):
        cwd, cwd_err = project_cwd(entry)
        if cwd_err:
            return False, note_launch_failure(entry, ps, now, cwd_err)
        prompt = engine_prompt(entry, reason, ps, now, note)
        if dry:
            return True, "dry run (%s)" % eng
        sh = find_outsourcerer()
        if not sh:
            return False, note_launch_failure(entry, ps, now, "outsourcerer.sh not found")
        args = list(ENGINE_CMD[eng]) + [prompt]
        try:
            r = subprocess.run([sh] + args, capture_output=True, text=True, timeout=180,
                               cwd=cwd,
                               env=dict(os.environ, OSRC_REQUIRE_INTERACTIVE="0"))
        except (OSError, subprocess.SubprocessError) as e:
            return False, note_launch_failure(entry, ps, now, str(e))
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        if r.returncode != 0:
            return False, note_launch_failure(entry, ps, now, "exit %d %s" % (r.returncode, out[:300].replace("\n", " ")))
        job_id = parse_job_id(r.stdout or out)
        if not job_id:
            return False, note_launch_failure(entry, ps, now, "no job id in output: %s" % out[:200].replace("\n", " "))
        ps["launch_failures"] = 0
        ps["job_id"] = job_id
        ps["started_at"] = now
        ps["engine"] = eng
        if reason == "answered":
            sent = dict(ps.get("answered_sent") or {})
            for d in new_answers(entry, ps):
                sent[d["id"]] = (d["answer"] or {}).get("ts")
            ps["answered_sent"] = sent
        ps["last_wake"] = {"reason": reason, "when": iso(now), "ts": now}
        ps["wakes"] = {"day": today(now), "count": wakes_today(ps, now) + 1}
        try:
            owner.append_log(entry["project"], "supervisor", "woke owner (%s, job %s): %s" % (eng, job_id, reason))
        except (OSError, ValueError) as e:
            return True, "launched %s; log.md failed: %s" % (job_id, e)
        return True, "job %s" % job_id
    # claude: existing nav-send behavior
    text = wake_text(entry, reason, ps, now, note)
    if dry:
        return True, "dry run"
    code, out = nav("send", entry["owner_key"], text)
    if code:
        return False, "send failed (exit %d) %s" % (code, out)
    if reason == "answered":
        sent = dict(ps.get("answered_sent") or {})
        for d in new_answers(entry, ps):
            sent[d["id"]] = (d["answer"] or {}).get("ts")
        ps["answered_sent"] = sent
    ps["last_wake"] = {"reason": reason, "when": iso(now), "ts": now}
    ps["wakes"] = {"day": today(now), "count": wakes_today(ps, now) + 1}
    try:
        owner.append_log(entry["project"], "supervisor", "woke owner: %s" % reason)
    except (OSError, ValueError) as e:
        return True, "sent; log.md failed: %s" % e
    return True, out


# ---------------------------------------------------------------- loud alerts (docs/supervisor.md, "Loud alerts")

def alert_reasons(d, owners, now):
    """Why Uri's open decision d must pull his attention now ([] = it must not).
    owners: the cycle's digest entries (each with project, status, paused)."""
    out = []
    try:
        asked = d.get("asked_at")
        asked = asked if isinstance(asked, (int, float)) else epoch(asked)
        if int(d.get("priority") or 3) == 1 and asked is not None and now - asked >= ALERT_P1_AGE:
            out.append("P1")
    except (TypeError, ValueError):
        pass
    dl = str(d.get("deadline") or "")
    if dl and dl <= today(now):
        out.append("due today" if dl == today(now) else "overdue since " + dl)
    did = str(d.get("id") or "")
    for o in owners:
        st = o.get("status") or {}
        b = st.get("blocked") if isinstance(st.get("blocked"), dict) else {}
        if (st.get("state") == "blocked" and str(b.get("unblocker") or "").strip().lower() == "uri"
                and blocked_by(o) and blocked_by(o) <= today(now) and did in [str(x) for x in st.get("needs_uri") or []]):
            out.append("blocks " + o["project"])
    return out


def alerts_due(open_list, owners, now):
    """[{id, reasons, title, body}] for each open decision that needs a loud alert, at most one per decision.
    A decision that a paused project waits on is left alone."""
    held = {str(x) for o in owners if o.get("paused") for x in (o.get("status") or {}).get("needs_uri") or []}
    due, seen = [], set()
    for d in open_list:
        did = str(d.get("id") or "")
        if not did or did in seen or did in held:
            continue
        why = alert_reasons(d, owners, now)
        if not why:
            continue
        seen.add(did)
        q = " ".join(str(d.get("question") or did).split())
        opts = " / ".join(str(x) for x in d.get("options") or [])
        body = "Why now: %s.%s%s" % ("; ".join(why), (" Options: %s." % opts) if opts else "",
                                     (" Lane: %s." % d["lane"]) if d.get("lane") else "")
        due.append({"id": did, "reasons": why, "body": body,
                    "title": "Decision for you: " + (q if len(q) <= ALERT_Q_CHARS else q[:ALERT_Q_CHARS - 1] + "…")})
    return due


def alert_skip(key, view, now):
    """Why an alert with this key must not be raised again now (open, or acked < 12 h ago), or None."""
    if any(a.get("key") == key for a in view.get("open") or []):
        return "alert already open"
    acks = [float(a["acked_at"]) for a in view.get("acked") or [] if a.get("key") == key and a.get("acked_at")]
    if acks and now - max(acks) < ALERT_REARM:
        return "acked %.1f h ago" % ((now - max(acks)) / 3600)
    return None


def raise_alerts(open_list, owners, now, dry):
    """Step 3b: raise the due alerts through `nav.py alert raise` (the Navigator dedupes on the key too).
    -> [{decision, reasons, raised, skip?}]"""
    due = alerts_due(open_list, owners, now)
    if not due:
        return []
    try:
        view = get("/api/alerts")
    except Exception:           # an older Navigator or a hiccup: its own dedupe still holds
        view = {}
    out = []
    for a in due:
        key = "decision:" + a["id"]
        rec = {"decision": a["id"], "reasons": a["reasons"], "raised": False}
        skip = alert_skip(key, view, now)
        if not skip and sum(1 for r in out if r["raised"]) >= ALERT_MAX_NEW:
            skip = "one new alert per cycle"
        if not skip and not dry:
            code, res = nav("alert", "raise", "--title", a["title"], "--body", a["body"], "--decision", a["id"],
                            "--key", key, full=True)
            raw = res
            try:
                res = json.loads(raw)
            except ValueError:
                res = {}
            if code:
                skip = "raise failed (exit %d) %s" % (code, " ".join(raw.split())[:200])
            elif isinstance(res, dict) and res.get("raised") is False:
                skip = "navigator: %s" % (res.get("reason") or "not raised")
        if skip:
            rec["skip"] = skip
        else:
            rec["raised"] = True
        out.append(rec)
    return out


# ---------------------------------------------------------------- register / digest

def register_row(entry, ps, now, paused):
    st = entry["status"] or {}
    ship = ps.get("last_shipped")
    ship_ts = epoch((ship or {}).get("when"))
    open_decs = [d for d in entry["decisions"] if d["status"] == "open"]
    ages = [(now - float(d["asked_at"])) / 86400 for d in open_decs if isinstance(d.get("asked_at"), (int, float))]
    oldest = round(max(ages), 1) if ages else None
    flags = []
    if not entry["status"] or entry["problem"] in ("no status this turn", "no status yet"):
        flags.append("no-status")
    elif entry["problem"].startswith("status "):
        flags.append("status-old")
    if int(ps.get("idle_checkins") or 0) >= 2 and st.get("state") in ("working", "picking-next"):
        flags.append("idle-2x")
    by = blocked_by(entry)
    if st.get("state") == "blocked" and by and by <= today(now):
        flags.append("blocked-past-due")
    if oldest is not None and oldest >= DECISION_OLD_DAYS:
        flags.append("decision-%dd" % DECISION_OLD_DAYS)
    if any(d.get("deadline") and str(d["deadline"]) < today(now) for d in open_decs):
        flags.append("decision-overdue")
    lw = ps.get("last_wake")
    job = entry.get("job") or {}
    job_id = ps.get("job_id") or (job.get("job_id") if job.get("running") else None)
    job_state = "running" if job.get("running") else ("hung-canceled" if job.get("hung") else (ps.get("last_job") or {}).get("ended"))
    if not job_state and job_id:
        job_state = "launched"
    return {
        "project": entry["project"], "project_key": entry["project_key"], "owner_key": entry["owner_key"],
        "engine": entry.get("engine") or engine_of(entry["project"]),
        "job": {"id": job_id, "state": job_state, "started_at": ps.get("started_at"),
                "status": job.get("status")},
        "state": st.get("state"), "summary": st.get("summary"), "updated_at": st.get("updated_at"),
        "stale": entry["stale"], "problem": entry["problem"],
        "last_shipped": ship, "days_since_shipped": round((now - ship_ts) / 86400, 1) if ship_ts else None,
        "decisions_open": len(open_decs), "oldest_decision_days": oldest,
        "idle_checkins": int(ps.get("idle_checkins") or 0), "wakes_today": wakes_today(ps, now),
        "last_wake": {"reason": lw.get("reason"), "when": lw.get("when")} if lw else None,
        "launch_failures": int(ps.get("launch_failures") or 0),
        "flags": flags, "paused": paused,
        "working_now": entry["live"]["working_now"], "idle_min": entry["live"]["idle_min"],
    }


def digest_md(d, now):
    out = ["# Supervisor digest %s%s" % (iso(now)[:16].replace("T", " "), " (dry run)" if d["dry_run"] else "")]
    od = d["decisions_overall"]
    out.append("Uri's open decisions: %d%s." % (od["open"], ", oldest %.1fd" % od["oldest_days"]
                                                 if od["oldest_days"] is not None else ""))
    w = "; ".join("%s (%s)" % (x["project"], x["reason"]) for x in d["wakes"]) or "none"
    out.append("%s this cycle: %s." % ("Would wake" if d["dry_run"] else "Woke", w))
    held = ["%s (%s: %s)" % (x["project"], x["reason"], x["skip"]) for x in d["skips"] if x["reason"]]
    if held:
        out.append("Due but not woken: " + "; ".join(held) + ".")
    al = d.get("alerts") or []
    if al:
        out.append("Loud alerts: " + "; ".join("%s (%s)%s" % (
            a["decision"], "; ".join(a["reasons"]),
            (" would raise" if d["dry_run"] else " raised") if a["raised"] else " held: " + a.get("skip", ""))
            for a in al) + ". Never raise these yourself.")
    for e in d["errors"]:
        out.append("ERROR %s: %s" % (e["project"], e["error"]))
    out.append("Never re-do these wakes; act on the flags in register.json.")
    for o in d["owners"]:
        st = o["status"] or {}
        r = o["register"]
        lv = o["live"]
        live = ("working now" if lv["working_now"] else "idle %s" % ago(now - lv["idle_min"] * 60, now)
                if lv["idle_min"] is not None else "session not found")
        out.append("")
        eng = o.get("engine") or (o.get("register") or {}).get("engine") or "claude"
        job = (o.get("register") or {}).get("job") or {}
        jbit = (" job %s (%s)" % (job.get("id"), job.get("state"))) if job.get("id") or job.get("state") else ""
        out.append("## %s [%s/%s] %s%s" % (o["project"], eng, st.get("state") or "no status", live, jbit))
        out.append("Owner %s. Status %s%s." % (o["owner_key"], st.get("updated_at") or "never",
                                              (" (%s)" % o["problem"]) if o["problem"] else ""))
        if st:
            out.append("Summary: %s" % st.get("summary"))
            out.append("Next: %s" % st.get("next"))
            if st.get("state") == "blocked" and st.get("blocked"):
                b = st["blocked"]
                out.append("Blocked: %s (unblocker %s, by %s)" % (b.get("what"), b.get("unblocker"), b.get("by")))
        if r["last_shipped"]:
            out.append("Last shipped (%sd ago): %s" % (r["days_since_shipped"], r["last_shipped"]["text"]))
        if o["scoreboard_problems"]:
            out.append("Scoreboard: " + "; ".join(o["scoreboard_problems"]))
        for x in o["decisions"]:
            if x["status"] == "answered":
                a = x["answer"] or {}
                out.append("Decision %s: answered by %s %s%s: %s%s" % (
                    x["id"], a.get("by"), ago(float(a.get("ts") or 0), now),
                    " (after the status)" if x["answered_after_status"] else "", a.get("choice"),
                    (" | " + a["note"]) if a.get("note") else ""))
            else:
                out.append("Decision %s: %s%s" % (x["id"], x["status"],
                                                  (", due " + str(x["deadline"])) if x.get("deadline") else ""))
        if r["flags"]:
            out.append("Flags: " + ", ".join(r["flags"]))
        if r["last_wake"]:
            out.append("Last wake: %s at %s; %d today" % (r["last_wake"]["reason"], r["last_wake"]["when"],
                                                         r["wakes_today"]))
        if o["log_tail"]:
            out.append("Log:")
            out.extend(ln if len(ln) <= LOG_CHARS else ln[:LOG_CHARS - 3] + "..." for ln in o["log_tail"])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- commands

def cycle(dry_run=False, now=None):
    now = now or time.time()
    dry = dry_run or dry_env()
    tag = " (dry run)" if dry else ""
    try:
        st = load_state()
        if quiet(now):
            return log_cycle("skip: quiet hours" + tag, now)
        if st.get("paused"):
            return log_cycle("skip: paused" + tag, now)
        errors = []
        try:
            board_rows = refresh_scoreboards(dry)
        except Exception as e:      # scoreboard trouble must not stop the cycle
            board_rows = {}
            errors.append({"project": "(scoreboard)", "error": str(e)})
        tree = get("/api/tree?window=7d")
        dec = get("/api/decisions")
        answers = load_answers(dec)
        open_list = [x for x in dec.get("open") or [] if isinstance(x, dict)]
        open_map = {str(x.get("id")): x for x in open_list}
        asked = [float(x["asked_at"]) for x in open_list if isinstance(x.get("asked_at"), (int, float))]
        paused_projects = set(st.get("paused_projects") or [])
        projects = st.get("projects") or {}
        owners, wakes, skips, rows, done = [], [], [], [], {}
        for pkey, info in sorted((tree.get("owners") or {}).items()):
            slug = owner.slug_of(pkey)
            try:
                ps = json.loads(json.dumps(projects.get(slug) or {}))   # private copy
                # Serialize check-launch-durable-tracking across cycles and manual wakes.
                # Lock, reload durable state (preserve raced job IDs/wakes), observe+decide,
                # launch if due, persist this owner's state before releasing.
                with state_lock():
                    cur = load_state() if not dry else {"projects": projects, "paused_projects": list(paused_projects), "paused": st.get("paused")}
                    latest = (cur.get("projects") or {}).get(slug) or {}
                    paused_projects = set(cur.get("paused_projects") or paused_projects)
                    ps = _merge_raced_durable(latest, ps)
                    entry = observe(pkey, info, tree, answers, open_map, board_rows, ps, now, dry=dry)
                    paused = slug in paused_projects or bool(cur.get("paused"))
                    reason = wake_reason(entry, ps, now)
                    skip = skip_reason(entry, reason, ps, now, paused) if reason else "nothing due"
                    if reason and not skip:
                        ok, out = do_wake(entry, reason, ps, now, dry)
                        if ok:
                            wakes.append({"project": slug, "reason": reason, "key": entry["owner_key"]})
                        else:
                            skip = out
                    if skip:
                        skips.append({"project": slug, "reason": reason, "skip": skip})
                    if not dry:
                        cur2 = load_state()
                        latest2 = (cur2.get("projects") or {}).get(slug) or {}
                        cur2.setdefault("projects", {})[slug] = _merge_raced_durable(latest2, ps)
                        write_json(path("state.json"), cur2)
                entry["wake"] = {"reason": reason, "skip": skip, "dry_run": dry}
                entry["paused"] = paused
                entry["register"] = register_row(entry, ps, now, paused)
                rows.append(entry["register"])
                owners.append(entry)
                done[slug] = ps
            except Exception as e:
                errors.append({"project": slug, "error": "%s: %s" % (type(e).__name__, e)})
                rows.append({"project": slug, "project_key": pkey, "owner_key": (info or {}).get("key"),
                             "error": str(e), "flags": ["error"]})
        try:
            alerts = raise_alerts(open_list, owners, now, dry)
        except Exception as e:      # alerts must never stop the digest and register
            alerts = []
            errors.append({"project": "(alerts)", "error": "%s: %s" % (type(e).__name__, e)})
        digest = {"generated_at": iso(now), "ts": now, "dry_run": dry,
                  "decisions_overall": {"open": len(open_list),
                                        "oldest_days": round((now - min(asked)) / 86400, 1) if asked else None},
                  "owners": owners, "wakes": wakes, "skips": skips, "alerts": alerts, "errors": errors}
        write_json(path("digest.json"), digest)
        owner.write_atomic(path("digest.md"), digest_md(digest, now))
        write_json(path("register.json"), {"generated_at": iso(now), "ts": now, "dry_run": dry, "rows": rows})
        if not dry:
            with state_lock():
                cur = load_state()
                cur["last_cycle"] = iso(now)
                write_json(path("state.json"), cur)
        line = "cycle%s: %d owners; %s %s; skips %s%s%s" % (
            tag, len(rows), "would wake" if dry else "woke",
            ", ".join("%s (%s)" % (w["project"], w["reason"]) for w in wakes) or "none",
            "; ".join("%s: %s" % (s["project"], s["skip"]) for s in skips) or "none",
            ("; %s %s" % ("would alert" if dry else "alerts", ", ".join(
                "%s (%s)" % (a["decision"], "; ".join(a["reasons"])) for a in alerts if a["raised"])))
            if any(a["raised"] for a in alerts) else "",
            "; errors " + "; ".join("%s: %s" % (e["project"], e["error"]) for e in errors) if errors else "")
        log_cycle(line, now)
    except Exception as e:      # launchd has no one to show a traceback to
        try:
            log_cycle("error%s: %s: %s" % (tag, type(e).__name__, e), now)
        except OSError:
            pass
    return 0


def resolve_project(tree, needle):
    """(project key, owner info) for a registered owner matching key, slug or display name."""
    needle = str(needle or "").strip()
    owners = tree.get("owners") or {}
    if needle in owners:
        return needle, owners[needle]
    low = owner.slug_of(needle).lower()
    names = {str(r.get("key")): str(r.get("name") or "").lower() for r in tree.get("repos") or [] if isinstance(r, dict)}
    hits = [k for k in owners if owner.slug_of(k).lower() == low or names.get(k) == needle.lower()]
    if len(hits) == 1:
        return hits[0], owners[hits[0]]
    raise ValueError("no registered owner for %r (known: %s)" % (
        needle, ", ".join(sorted(owner.slug_of(k) for k in owners)) or "none"))


_RACE_KEEP = ("job_id", "started_at", "last_wake", "wakes", "answered_sent", "launch_failures", "last_job")


def _merge_raced_durable(latest, local):
    """Preserve durable tracking written by a concurrent actor (job IDs/wakes must not be lost)."""
    latest = dict(latest or {})
    local = dict(local or {})
    latest_wake = ((latest.get("last_wake") or {}).get("ts") or 0)
    local_wake = ((local.get("last_wake") or {}).get("ts") or 0)
    latest_job = latest.get("job_id")
    local_job = local.get("job_id")
    local_resolved_latest = bool(latest_job and (local.get("last_job") or {}).get("job_id") == latest_job)
    local_cleared = (not local_job and local_resolved_latest)
    # FAIL CLOSED for races: if durable state has an active job_id we did NOT resolve, keep it.
    if latest_job and not local_resolved_latest and (not local_job or local_job != latest_job):
        for k in _RACE_KEEP:
            if k in latest:
                local[k] = latest.get(k)
        if latest.get("engine"):
            local["engine"] = latest["engine"]
        return local
    # latest resolved the same job terminally and has newer wake info with no new job: keep latest
    if latest_job and local_cleared and latest_wake > local_wake and not local_job:
        for k in _RACE_KEEP:
            if k in latest:
                local[k] = latest.get(k)
        if latest.get("engine"):
            local["engine"] = latest["engine"]
        return local
    if latest_wake > local_wake and not latest_job:
        for k in _RACE_KEEP:
            if k in latest:
                local[k] = latest.get(k)
        if latest.get("engine"):
            local["engine"] = latest["engine"]
    return local


def wake_cmd(project, note="", now=None):
    """Manual wake: same caps (pause, 30 min, 24 a day), serialized across check-launch-track."""
    now = now or time.time()
    dry = dry_env()
    tree = get("/api/tree?window=7d")
    pkey, info = resolve_project(tree, project)
    slug = owner.slug_of(pkey)
    dec = get("/api/decisions")
    with state_lock():
        st = load_state()
        ps = json.loads(json.dumps((st.get("projects") or {}).get(slug) or {}))
        entry = observe(pkey, info, tree, load_answers(dec), {str(x.get("id")): x for x in dec.get("open") or []},
                        {}, ps, now, dry=dry)       # observation mutates ps; only wake tracking is persisted below
        paused = bool(st.get("paused")) or slug in (st.get("paused_projects") or [])
        eng = entry.get("engine") or engine_of(slug)
        # Re-check durable tracking under lock: caps + fail-closed job checks (manual wake
        # is not blocked by working_now/waiting-on-uri the way a cycle wake is).
        skip = cap_reason(ps, now, paused)
        if not skip and eng == "claude" and not entry["owner_key"]:
            skip = "no owner session"
        if not skip and eng != "claude":
            job = entry.get("job") or {}
            tracked = job.get("job_id") or ps.get("job_id")
            if tracked and not job.get("terminal"):
                if job.get("unknown"):
                    skip = "job status unknown (%s)" % tracked
                elif job.get("cancel_failed"):
                    skip = "job cancel failed (%s)" % tracked
                else:
                    skip = "job running (%s)" % tracked
        res = {"ok": False, "project": slug, "reason": "manual", "dry_run": dry, "engine": eng}
        if skip:
            res["skip"] = skip
        else:
            ok, out = do_wake(entry, "manual", ps, now, dry, note)
            res.update(ok=ok, output=out)
            if ok and not dry:
                # Reload and merge to preserve any raced durable tracking (job IDs/wakes).
                # Manual wake persists only durable tracking, not observation/check-in state.
                cur = load_state()
                latest = dict((cur.get("projects") or {}).get(slug) or {})
                merged = _merge_raced_durable(latest, ps)
                for obs in ("seen_updated_at", "idle_checkins", "last_shipped"):
                    if obs in latest:
                        merged[obs] = latest[obs]
                    elif obs in merged and obs not in ps:
                        pass
                cur.setdefault("projects", {})[slug] = merged
                write_json(path("state.json"), cur)
    log_cycle("manual wake %s by %s: %s%s" % (slug, actor(), "sent" if res["ok"] else res.get("skip") or res.get("output"),
                                              " (dry run)" if dry else ""), now)
    return res


def set_pause(project, on):
    slug = None
    if project:
        try:
            pkey, _ = resolve_project(get("/api/tree?window=7d"), project)
            slug = owner.slug_of(pkey)
        except OSError:             # Navigator down: take the folder name as given
            slug = owner.slug_of(project)
    with state_lock():
        st = load_state()
        if slug:
            pp = set(st.get("paused_projects") or [])
            (pp.add if on else pp.discard)(slug)
            st["paused_projects"] = sorted(pp)
        else:
            st["paused"] = bool(on)
        write_json(path("state.json"), st)
    log_cycle("%s %s by %s" % ("pause" if on else "resume", slug or "all", actor()))
    return {"ok": True, "paused": bool(st.get("paused")), "paused_projects": st.get("paused_projects") or []}


def view():
    """What GET /api/supervisor returns: {register, digest_at, paused, paused_projects, cycles_tail, last_cycle}."""
    reg = owner.read_json(path("register.json")) or {}
    dg = owner.read_json(path("digest.json")) or {}
    st = load_state()
    return {"register": reg.get("rows") or [], "digest_at": dg.get("generated_at"),
            "paused": bool(st.get("paused")), "paused_projects": st.get("paused_projects") or [],
            "last_cycle": st.get("last_cycle"), "cycles_tail": cycles_tail()}


def status_table(v):
    out = ["supervisor %s; last cycle %s; digest %s" % (
        "PAUSED" if v["paused"] else "running", v["last_cycle"] or "never", v["digest_at"] or "none")]
    for r in v["register"]:
        if r.get("error"):
            out.append("%-18s ERROR %s" % (r["project"], r["error"]))
            continue
        lw = r.get("last_wake") or {}
        job = r.get("job") or {}
        j = (" job:%s(%s)" % (job.get("id"), job.get("state"))) if job.get("id") or job.get("state") else ""
        out.append("%-18s %-8s %-15s shipped %-5s dec %d%-7s idle %d wakes %d last %-12s%s %s%s" % (
            r["project"], r.get("engine") or "claude", r.get("state") or "no status",
            "%sd" % r["days_since_shipped"] if r.get("days_since_shipped") is not None else "-",
            r["decisions_open"], " (%sd)" % r["oldest_decision_days"] if r.get("oldest_decision_days") is not None else "",
            r["idle_checkins"], r["wakes_today"], lw.get("reason") or "-", j,
            ("[" + ", ".join(r["flags"]) + "] ") if r.get("flags") else "",
            "(paused) " if r.get("paused") else ""))
    return "\n".join(out)


def engine_cmd(project, eng):
    """Set the engine for one owner or 'all'. -> result dict."""
    if eng not in ENGINES:
        raise ValueError("engine must be one of: %s" % ", ".join(ENGINES))
    if str(project).lower() == "all":
        slugs = [o["slug"] for o in owner.list_owners()] if hasattr(owner, "list_owners") else []
        if not slugs:
            try:
                slugs = [n for n in os.listdir(owner.root()) if os.path.isdir(os.path.join(owner.root(), n))]
            except OSError:
                slugs = []
        for sl in sorted(set(slugs)):
            set_engine_value(sl, eng)
        log_cycle("engine all -> %s by %s" % (eng, actor()))
        return {"ok": True, "project": "all", "engine": eng, "count": len(slugs)}
    try:
        pkey, _ = resolve_project(get("/api/tree?window=7d"), project)
        slug = owner.slug_of(pkey)
    except (OSError, ValueError):
        slug = owner.slug_of(project)
    set_engine_value(slug, eng)
    log_cycle("engine %s -> %s by %s" % (slug, eng, actor()))
    return {"ok": True, "project": slug, "engine": eng}


def main(argv):
    p = argparse.ArgumentParser(prog="supervisor.py", description="Project-owner supervisor (docs/supervisor.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="one 15-minute cycle")
    c.add_argument("--dry-run", action="store_true", help="decide and log wakes, send nothing, save no state")
    s = sub.add_parser("status", help="the register as a table")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("wake", help="manual wake of one owner (same caps)")
    w.add_argument("project")
    w.add_argument("--reason", default="", help="what the owner should do")
    w.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        x = sub.add_parser(name, help="%s supervision (all, or one project)" % name)
        x.add_argument("project", nargs="?")
        x.add_argument("--json", action="store_true")
    e = sub.add_parser("engine", help="set an owner's engine (claude|opencode|devin)")
    e.add_argument("project", help="project key/slug, or 'all'")
    e.add_argument("engine", choices=ENGINES)
    e.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "cycle":
        return cycle(a.dry_run)
    if a.cmd == "status":
        v = view()
        print(json.dumps(v, indent=1, ensure_ascii=False) if a.json else status_table(v))
        return 0
    try:
        if a.cmd == "wake":
            res = wake_cmd(a.project, a.reason)
        elif a.cmd == "engine":
            res = engine_cmd(a.project, a.engine)
        else:
            res = set_pause(a.project, a.cmd == "pause")
    except (ValueError, OSError) as e:
        res = {"ok": False, "error": str(e)}
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(res.get("error") or res.get("skip") or res.get("output") or ("%s: ok" % a.cmd if res.get("ok") else json.dumps(res)))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
