#!/usr/bin/env python3
"""Supervisor (masterplan #79, phase 2; contract: docs/supervisor.md).

Keeps every project owner moving and keeps Uri's view current. Routine work is code; judgment
stays with the AI supervisor (the Super-co-pilot), which reads digest.md and the register.

  supervisor.py cycle [--dry-run]              one 15-minute cycle (launchd com.standup-os.supervisor)
  supervisor.py status [--json]                the register as a table (JSON: what GET /api/supervisor returns)
  supervisor.py wake PROJECT [--reason TEXT]   manual wake, same caps as the cycle
  supervisor.py pause [PROJECT]                pause supervision (everything, or one project)
  supervisor.py resume [PROJECT]               resume it
  supervisor.py engine PROJECT|all session|jobs   how an owner gets its turn (docs/supervisor.md, "Engines")
  supervisor.py night on|off                   keep the cycle going through 00:00-07:00, under the night rules
  supervisor.py feedback PROJECT TEXT          put a note first in that owner's next turn (jobs engine)
  supervisor.py stop                           pause everything and cancel every owner job that is running

One cycle: refresh the scoreboards, write digest.json + digest.md, wake owners by rule (answered,
no-status, blocked-due, keep-moving; first match wins), raise loud alerts for the few decisions that
need Uri now (P1, at/past deadline, or blocking an owner on Uri past its date; at most one per decision),
rewrite register.json, keep state.json and append one line to cycles.log, all under SUPERVISOR_DIR
(default ~/.ccc-navigator/supervisor).
Skips 00:00-07:00 local (unless night mode is on) and while paused. A failing owner never stops the others;
the cycle always exits 0 after logging the error.

A wake is a message into the owner's session (engine `session`) or one free-model job that runs the owner's
turn from its brief (engine `jobs`): one job per owner, a few at a time, none while the Mac is overloaded, and
a turn that delivers no status moves that owner to the next provider.

SUPERVISOR_DRY_RUN=1 (or --dry-run) decides and logs wakes but sends nothing, starts and cancels no job,
appends nothing to any owner's log.md, does not write scoreboards (they are computed in memory) and does not
save state.json.
Env: SUPERVISOR_DIR, OWNER_DIR, CCC_NAV_URL, CCC_NAV_ANSWERS, SUPERVISOR_ENGINE, SUPERVISOR_CHAIN,
SUPERVISOR_JOBS_MAX, SUPERVISOR_LOAD_PER_CPU, SUPERVISOR_OSRC, SUPERVISOR_JOB_PATH.
"""
import argparse
import contextlib
import datetime
import glob
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

# Engines (docs/supervisor.md, "Engines"): how an owner gets its turn.
ENGINES = ("session", "jobs")       # a message into its session | one free-model job per turn
PROVIDERS = (                       # the jobs engine's chain, best measured first: label, Outsourcerer provider, verb, model
    ("iceman", "opencode", "edit", "opencode-go/iceman-contributor-free"),
    ("minimax", "droid", "yolo", "custom:MiniMax-M3-direct"),
    ("beam", "opencode", "edit", "opencode-go/beam-free"),
    ("devin", "devin", "edit", "swe-2-high"),
)
OSRC_GLOB = "~/.claude/plugins/cache/outsourcerer/outsourcerer/*/skills/outsourcerer/scripts/outsourcerer.sh"
JOB_PATH = ("~/.opencode/bin", "~/.local/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin")   # launchd's PATH is bare
JOB_LIVE = ("launching", "running", "queued", "starting")
# Outsourcerer's words for a job that is over. Any other word ("stalled?", "no-progress-writes", one it adds
# later) and a state that cannot be read count as alive: the job may still be working in the owner's folder.
JOB_ENDED = ("done", "done?", "failed", "canceled", "cancelled", "timeout", "interrupted", "wedged", "blocked",
             "permission-blocked")
JOBS_MAX = 4            # owner jobs at once (env SUPERVISOR_JOBS_MAX)
JOBS_MAX_NIGHT = 2      # ... during quiet hours, when night mode keeps the cycle going
LAUNCH_S = 120          # seconds a launch may take
HUNG_MIN = 90           # a job running longer than this is cancelled
STALL_MIN = 30          # a job Outsourcerer calls stalled or silent, this old and with no status written, is cancelled
LINGER_MIN = 10         # a job still running this long after its owner wrote the status is cancelled: the turn is over
RETRY_MIN = 5           # after a turn that delivered nothing, the next provider gets its try this soon (not GAP_MIN)
COOL_STREAK = 3         # a provider whose last turns all failed, this many in a row...
COOL_MIN = 60           # ...is left out this long, unless every provider is cooling off
LOAD_PER_CPU = 3.0      # no new job while the Mac's 5-minute load is above this per CPU (env SUPERVISOR_LOAD_PER_CPU)
FEEDBACK_CHARS = 1800   # tail of owners/<slug>/supervisor-feedback.md that opens a job's turn
PROVIDERS_DOWN_KEY = "supervisor:providers-down"

TURN_RULES = (
    "HOW THIS TURN RUNS. You are one turn of this project's owner, started by the supervisor on a free model. Nobody "
    "is typing to you and nobody answers inside this turn. Your shell starts in your owner folder %(folder)s; the "
    "code lives in %(repo)s. Never edit, commit in or switch branches of that main checkout: every change goes in "
    "your own git worktree under %(folder)s/wt/<name> (git -C %(repo)s worktree add ...). Never copy a .env file "
    "into it (a folder that holds one cannot start a worker; use `doppler run` for secrets). Use absolute paths.\n"
    "Work the top item of your plan (today-plan.md in your folder; write it first when it is missing or stale): "
    "delegate the coding to free workers through Outsourcerer, read every diff yourself, get a review from a "
    "DIFFERENT non-Claude model, and merge only when the tests pass, that review is in and a rollback is ready. A "
    "production deploy needs a go: ask it as an approval (rule 7) and take the next item meanwhile. What is Uri's "
    "stays his: ask with its kind and move on, never wait inside a turn. When a tool or a provider is blocked, "
    "write exactly what is missing and continue with the rest. Never stop at a plan: this turn ends with shipped "
    "work or a verified blocker, and then the status command."
)
NIGHT_RULES = (
    "NIGHT RULES (Uri is asleep until about 07:00). Do not spend money or change any budget, bid or price. Do not "
    "deploy to production or merge to a production branch: open the PR, merge to staging only, and ask the "
    "production go as an approval for the morning. At most 2 parallel workers. Never message real people. Never "
    "delete data or another agent's branch."
)


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


# ---------------------------------------------------------------- engines (docs/supervisor.md, "Engines")

def _engine(v):
    v = (str(v or "").split() or [""])[0].lower()
    return "session" if v == "claude" else v if v in ENGINES else ""


def engine_of(slug, st=None):
    """How this owner gets its turn: $SUPERVISOR_ENGINE (every owner; tests), else the one line in
    owners/<slug>/engine, else the fleet default in state.json (`engine all ...` sets it), else session."""
    try:
        with open(os.path.join(owner.folder(slug), "engine")) as f:
            own = _engine(f.read())
    except OSError:
        own = ""
    return _engine(os.environ.get("SUPERVISOR_ENGINE")) or own or _engine((st or {}).get("engine_default")) or "session"


def chain():
    """The providers a jobs owner may run on, in order ($SUPERVISOR_CHAIN: labels, comma separated)."""
    by = {p[0]: p for p in PROVIDERS}
    want = [x.strip() for x in os.environ.get("SUPERVISOR_CHAIN", "").split(",")]
    return [by[x] for x in want if x in by] or list(PROVIDERS)


def cooling(h, now):
    """A provider whose last COOL_STREAK turns all delivered nothing sits out COOL_MIN."""
    h = h if isinstance(h, dict) else {}
    return int(h.get("streak") or 0) >= COOL_STREAK and now - float(h.get("last_bad") or 0) < COOL_MIN * 60


def pick_provider(ps, st, now):
    """This owner's provider for its next turn: its own place in the chain (it moves on after a turn that delivered
    nothing and stays where a turn delivered), skipping providers that are cooling off, unless they all are."""
    ch = chain()
    i = int(ps.get("provider_idx") or 0) % len(ch)
    order = ch[i:] + ch[:i]
    health = st.get("providers") or {}
    return next((p for p in order if not cooling(health.get(p[0]), now)), order[0])


def score(st, ps, label, delivered, now):
    """Count one turn for its provider. A turn that delivered nothing moves the owner on to the next provider."""
    labels = [p[0] for p in chain()]
    h = st.setdefault("providers", {}).setdefault(label, {})
    if delivered:
        h.update(ok=int(h.get("ok") or 0) + 1, streak=0, last_ok=now)
        ps["turn_fails"] = 0
    else:
        h.update(bad=int(h.get("bad") or 0) + 1, streak=int(h.get("streak") or 0) + 1, last_bad=now)
        ps["turn_fails"] = int(ps.get("turn_fails") or 0) + 1
        if label in labels:
            ps["provider_idx"] = (labels.index(label) + 1) % len(labels)


def osrc_path():
    """Outsourcerer's script: $SUPERVISOR_OSRC, else the newest version in the plugin cache, else None."""
    if os.environ.get("SUPERVISOR_OSRC"):
        return os.environ["SUPERVISOR_OSRC"]

    def ver(p):
        return tuple(int(x) if x.isdigit() else 0 for x in p.split(os.sep)[-5].split("."))
    hits = sorted(glob.glob(os.path.expanduser(OSRC_GLOB)), key=ver)
    return hits[-1] if hits else None


_LOGIN_PATH = None


def login_path():
    """The PATH of Uri's login shell, read once: launchd's own is bare (opencode, droid, devin and gh are not on it)."""
    global _LOGIN_PATH
    if _LOGIN_PATH is None:
        try:
            out = subprocess.run(["/bin/zsh", "-lc", 'printf %s "$PATH"'], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _LOGIN_PATH = [p for p in str(out or "").split(os.pathsep) if p.startswith("/")]
    return _LOGIN_PATH


def job_env():
    """The environment an Outsourcerer call runs in."""
    seen, out = set(), []
    for p in (os.environ.get("SUPERVISOR_JOB_PATH", "").split(os.pathsep) + os.environ.get("PATH", "").split(os.pathsep)
              + [os.path.expanduser(x) for x in JOB_PATH] + login_path()):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return dict(os.environ, PATH=os.pathsep.join(out), OSRC_REQUIRE_INTERACTIVE="0")


def osrc_run(args, cwd=None, timeout=90):
    """Run outsourcerer.sh -> (exit code, stdout, stderr). 127: not installed, 124: timed out. Mocked in tests."""
    sh = osrc_path()
    if not sh:
        return 127, "", "outsourcerer.sh not found (%s)" % OSRC_GLOB
    try:
        r = subprocess.run([sh] + list(args), cwd=cwd, env=job_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %d s" % timeout
    except OSError as e:
        return 126, "", str(e)
    return r.returncode, r.stdout or "", r.stderr or ""


def jobs_listing():
    """{job id: its record} for Outsourcerer's recent jobs (`status --json`: status, cwd, started, model);
    {} when it cannot be read."""
    try:
        jobs = json.loads(osrc_run(["status", "--json"])[1]).get("jobs")
    except (ValueError, AttributeError):
        return {}
    return {str(j["job_id"]): j for j in jobs or [] if isinstance(j, dict) and j.get("job_id")}


def job_status(job_id, listing):
    """Outsourcerer's word for a job ('running', 'done', 'wedged', ...); None when it cannot be read."""
    j = (listing or {}).get(str(job_id))
    if j is None:
        try:
            j = json.loads(osrc_run(["status", "--json", str(job_id)], timeout=60)[1])
        except ValueError:
            return None
    return (str(j.get("status") or "").strip().lower() or None) if isinstance(j, dict) else None


def stray_job(slug, listing, skip=()):
    """A live job running in this owner's folder that the supervisor has no record of (a launch whose answer was
    lost, or another runner's), the newest first: (id, started, provider label), else None."""
    here = os.path.realpath(owner.folder(slug))
    live = [(j.get("started") if isinstance(j.get("started"), (int, float)) else 0, jid, j)
            for jid, j in (listing or {}).items()
            if jid not in skip and str(j.get("status") or "").lower() not in JOB_ENDED + ("",) and j.get("cwd")
            and os.path.realpath(str(j["cwd"])) == here]
    if not live:
        return None
    _, jid, j = max(live, key=lambda x: (x[0], x[1]))
    return jid, j.get("started"), next((p[0] for p in PROVIDERS if p[3] == j.get("model")), "other")


def job_started(v, now):
    """A job's start as epoch seconds, never in the future (milliseconds and junk are tolerated): a start time
    that cannot be trusted must not keep a job from ever counting as hung."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return now
    if t > 1e11:
        t /= 1000.0
    return t if 0 < t <= now else now


def settle_job(slug, ps, st, upd_ts, listing, now, dry):
    """Jobs-engine bookkeeping for one owner -> its job as the board shows it, or None when it has none:
    {"id", "provider", "state", "min", "running"}. A turn delivered when the owner wrote its status after the job
    started. Takes over a live job in the owner's folder that nobody recorded, closes the record of a job that
    ended, cancels one that hangs, stalls or lingers after the status (never in a dry run), and scores the provider.
    When a job ended, any other live job left in the folder is taken over next, so no turn starts beside it."""
    seen, view = set(), None
    while True:
        job = ps.get("job") if isinstance(ps.get("job"), dict) and ps["job"].get("id") else None
        if not job:
            ps.pop("job", None)
            found = stray_job(slug, listing, seen)
            if not found:
                return view
            job = ps["job"] = {"id": found[0], "started": job_started(found[1], now), "provider": found[2],
                               "adopted": True}
        seen.add(str(job["id"]))
        view = settle_one(ps, st, job, upd_ts, listing, now, dry)
        if view["running"]:
            return view


def settle_one(ps, st, job, upd_ts, listing, now, dry):
    """settle_job for the one job in ps["job"]: its view; when it ended, the record moves to ps["last_job"]."""
    started = job["started"] = job_started(job.get("started"), now)
    age = now - started
    delivered = upd_ts is not None and upd_ts > started
    word = job_status(job["id"], listing)
    alive = word not in JOB_ENDED       # it may still hold the owner's folder
    end = word
    if word is None:
        end = "lost" if age > HUNG_MIN * 60 else None
    elif alive:                         # running, or one of Outsourcerer's words for alive but silent
        end = ("hung" if age > HUNG_MIN * 60 else "done" if delivered and now - upd_ts > LINGER_MIN * 60
               else "stalled" if word not in JOB_LIVE and not delivered and age > STALL_MIN * 60 else None)
    view = {"id": job["id"], "provider": job.get("provider"), "state": word or "unknown", "min": int(age // 60),
            "running": True}
    if end is None or (dry and alive):
        return view
    if not dry and (alive or word in ("wedged", "blocked", "permission-blocked")):
        failed = osrc_run(["cancel", str(job["id"])], timeout=60)[0] != 0   # nothing may be left running in the folder
        if failed and alive and job_status(job["id"], None) not in JOB_ENDED:
            return dict(view, state="cancel failed")    # still there, however old: keep the record, try again next cycle
    ps["last_job"] = {"id": job["id"], "provider": job.get("provider"), "ended": end, "ts": now, "when": iso(now),
                      "delivered": delivered, "min": int(age // 60)}
    del ps["job"]
    if not job.get("stopped") and not job.get("adopted") and job.get("provider") in [p[0] for p in chain()]:
        score(st, ps, job["provider"], delivered, now)      # only a turn the supervisor started itself is scored
    return dict(view, state=end, running=False)


def jobs_problem(status, upd_ts, ps, now):
    """The Navigator's `problem` for an owner that has no session: no status yet / this turn / an old one."""
    if not status:
        return "no status yet"
    lj = ps.get("last_job") or {}
    if lj and not lj.get("delivered") and (upd_ts or 0) < float(lj.get("ts") or 0):
        return "no status this turn"
    if upd_ts and now - upd_ts > 24 * 3600:
        return "status %dh old" % ((now - upd_ts) // 3600)
    return ""


def overloaded():
    """Why the Mac cannot take one more job now ('' when it can): its 5-minute load, or critical memory pressure.
    Mocked in tests."""
    try:
        per = float(os.environ.get("SUPERVISOR_LOAD_PER_CPU") or LOAD_PER_CPU)
    except ValueError:
        per = LOAD_PER_CPU
    try:
        load5, limit = os.getloadavg()[1], per * (os.cpu_count() or 8)
        if load5 > limit:
            return "load %.0f, limit %.0f" % (load5, limit)
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True,
                             timeout=5).stdout
        if str(out).strip() == "4":
            return "memory pressure is critical"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def jobs_max(night):
    try:
        n = int(os.environ.get("SUPERVISOR_JOBS_MAX") or 0)
    except ValueError:
        n = 0
    n = n if n > 0 else JOBS_MAX
    return min(n, JOBS_MAX_NIGHT) if night else n


ENV_SKIP_DIRS = ("node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "Pods", "__pycache__")
ENV_KEEP = (".example", ".sample", ".template")


def stash_env(slug):
    """Outsourcerer refuses to start in a folder that holds a .env file, and owners keep making worktrees with one.
    Move each (never delete) to <next to the owners root>/owners-env-stash/<slug>/<same path> before a launch.
    -> how many moved."""
    root, moved = owner.folder(slug), 0
    stash = os.path.join(os.path.dirname(os.path.abspath(owner.root())), "owners-env-stash", slug)
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ENV_SKIP_DIRS]
        for fn in fns:
            if fn.startswith(".env") and not fn.endswith(ENV_KEEP):
                src = os.path.join(dp, fn)
                dst = os.path.join(stash, os.path.relpath(src, root))
                if not (os.path.realpath(os.path.dirname(dst)) + os.sep).startswith(os.path.realpath(stash) + os.sep):
                    continue            # a link inside the stash points elsewhere: never create or write outside it
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                subprocess.run(["git", "-C", dp, "update-index", "--skip-worktree", fn], capture_output=True, timeout=30)
                os.replace(src, dst)
                moved += 1
    return moved


def feedback_text(slug):
    try:
        with open(os.path.join(owner.folder(slug), "supervisor-feedback.md")) as f:
            return f.read().strip()[-FEEDBACK_CHARS:]
    except OSError:
        return ""


def job_prompt(entry, reason, ps, now, note="", night=False):
    """Everything one job needs to run this owner's turn: its brief, how a turn runs, the supervisor's feedback,
    the night rules while Uri sleeps, and what to do now (the wake text, which ends with the status command)."""
    slug = entry["project"]
    parts = [owner.brief(slug, entry["owner_key"] or None),
             TURN_RULES % {"folder": owner.folder(slug), "repo": entry["project_key"]}]
    fb = feedback_text(slug)
    if fb:
        parts.append("SUPERVISOR FEEDBACK (act on this FIRST):\n" + fb)
    if night:
        parts.append(NIGHT_RULES)
    parts.append(wake_text(entry, reason, ps, now, note))
    return "\n\n".join(parts)


def launch_job(entry, ps, st, now, text):
    """Start this owner's turn (text: job_prompt) as one Outsourcerer job in its owner folder. -> (ok, short text).
    On success the job is in ps["job"]; a launch that fails counts against the provider, so the next try uses the
    next one."""
    slug = entry["project"]
    folder = owner.folder(slug)
    if not os.path.isdir(folder):
        return False, "no owner folder %s" % folder
    label, prov, verb, model = pick_provider(ps, st, now)
    try:
        stash_env(slug)
    except (OSError, subprocess.SubprocessError):
        pass                # if a .env still blocks the launch, the launch says so
    rc, out, err = osrc_run(["--provider", prov, "bg", verb, "-m", model, text], cwd=folder, timeout=LAUNCH_S)
    m = re.search(r"\bjob (\S+) launched", out + "\n" + err)
    if not m:
        why = " ".join((err or out or "exit %d" % rc).split())[-160:]
        if rc in (124, 127):        # the Mac was too slow or Outsourcerer is missing: not this provider's failure
            return False, "could not start a job: %s" % why     # (a job that did start is taken over next cycle)
        ps["last_job"] = {"id": None, "provider": label, "ended": "not started", "ts": now, "when": iso(now),
                          "delivered": False, "min": 0}
        score(st, ps, label, False, now)
        return False, "could not start a job on %s: %s" % (label, why)
    ps["job"] = {"id": m.group(1), "started": now, "provider": label}
    return True, "job %s on %s" % (m.group(1), label)


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

def observe(pkey, info, tree, answers, open_map, board_rows, ps, now, st=None, listing=None, dry=False):
    """Digest entry for one owner; updates ps (its row in state.json) with the observation:
    idle_checkins, seen_updated_at, last_shipped and, on the jobs engine, its job (settle_job; st gets the
    provider's score, listing is jobs_listing())."""
    slug = owner.slug_of(pkey)
    key = (info or {}).get("key") or ""
    eng = engine_of(slug, st)
    status = owner.read_json(os.path.join(owner.folder(slug), "status.json"))
    status = status if isinstance(status, dict) else None
    upd = (status or {}).get("updated_at")
    upd_ts = epoch(upd)
    job = None
    if eng == "jobs":       # no session to watch: the job is the owner's turn, its status file and job its activity
        job = settle_job(slug, ps, st if st is not None else {}, upd_ts, listing, now, dry)
        # Someone may still drive this project in its registered session (Uri, or an owner session started by
        # hand): while that session is at work, or was within the last half hour, no job takes a turn beside it.
        sess = live_of(find_item(tree, key), now)
        in_session = sess["found"] and (sess["working_now"] or (
            (sess["idle_min"] or 0) < BUSY_MAX_IDLE_MIN and process_running(key)))
        seen_at = now - sess["idle_min"] * 60 if sess["found"] and sess["idle_min"] is not None else None
        last = max([t for t in (upd_ts, (ps.get("last_job") or {}).get("ts"), seen_at) if isinstance(t, (int, float))] or [0])
        running = bool(job and job["running"])
        live = {"found": True, "state": "", "live": running, "last_activity": iso(last) if last else None,
                "idle_min": round((now - last) / 60, 1) if last else None, "working_now": running or in_session,
                "in_session": in_session, "session_idle_min": sess["idle_min"] if sess["found"] else None}
        problem = jobs_problem(status, upd_ts, ps, now)
        stale = bool(problem)
    else:
        live = live_of(find_item(tree, key), now)
        if (live["found"] and not live["working_now"] and (live["idle_min"] or 0) < BUSY_MAX_IDLE_MIN
                and process_running(key)):
            live["working_now"] = True          # mid-turn: a process holds the session, never wake it now
        problem = (info or {}).get("problem") or ("" if status else "no status yet")
        stale = bool((info or {}).get("stale")) if info else True

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
        "stale": stale,
        "problem": problem,
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


def cap_reason(ps, now, paused, gap=GAP_MIN):
    """The caps every wake obeys, the manual one too."""
    if paused:
        return "paused"
    last = (ps.get("last_wake") or {}).get("ts")
    if isinstance(last, (int, float)) and now - last < gap * 60:
        return "woken %d min ago" % ((now - last) // 60)
    if wakes_today(ps, now) >= MAX_WAKES:
        return "%d wakes today" % MAX_WAKES
    return None


def wake_gap(entry, ps):
    """Minutes between two wakes of this owner. A jobs owner whose last turn delivered nothing gets the next
    provider after RETRY_MIN, until every provider of the chain has had its try in this streak."""
    fails = int(ps.get("turn_fails") or 0)
    return RETRY_MIN if entry.get("engine") == "jobs" and 0 < fails < len(chain()) else GAP_MIN


def job_words(job):
    return "job %s on %s, %d min" % (job.get("id"), job.get("provider") or "?", job.get("min") or 0)


def skip_reason(entry, reason, ps, now, paused):
    """Why a due wake must not happen (the never-wake rules), or None."""
    if paused:
        return "paused"
    if entry.get("engine") == "jobs":
        if (entry.get("job") or {}).get("running"):
            return "its turn is running (%s)" % job_words(entry["job"])
        if entry["live"]["working_now"]:
            return "its session is at work"
        used = entry["live"].get("session_idle_min")
        if used is not None and used < IDLE_MIN:
            return "its session was used %d min ago" % used
    else:
        if not entry["owner_key"]:
            return "no owner session"
        if not entry["live"]["found"]:
            return "owner session not in the Navigator"
        if entry["live"]["working_now"]:
            return "working now"
    if (entry["status"] or {}).get("state") == "waiting-on-uri" and reason not in ("answered", "keep-moving"):
        return "waiting on Uri, nothing newly answered"
    return cap_reason(ps, now, False, wake_gap(entry, ps))


def wake_text(entry, reason, ps, now, note=""):
    slug, st = entry["project"], entry["status"] or {}
    cmd = "python3 %s status set %s%s --json -" % (OWNER_PY, slug,
                                                  (" --session " + entry["owner_key"]) if entry["owner_key"] else "")
    head = "[Supervisor wake: %s]" % reason
    lj = ps.get("last_job") or {}
    if entry.get("engine") == "jobs" and lj and not lj.get("delivered") and reason != "answered":
        head += (" Your last turn (on %s) ended \"%s\" after %d min without a status. Look at what it left in your "
                 "folder, worktrees and open PRs before you redo anything."
                 % (lj.get("provider") or "?", lj.get("ended") or "?", lj.get("min") or 0))
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
                "for Uri (`nav.py ask --kind ...`, see rule 7 of your brief) with options, a deadline and a default."
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


def do_wake(entry, reason, ps, now, dry, note="", st=None, night=False):
    """Send one wake (unless dry) and record it in ps and log.md. -> (sent ok, short output).
    Engine session: one message into the owner's session. Engine jobs: one free-model job runs the turn."""
    jobs = entry.get("engine") == "jobs"
    text = job_prompt(entry, reason, ps, now, note, night) if jobs else wake_text(entry, reason, ps, now, note)
    if dry:
        return True, "dry run"
    if jobs:
        ok, out = launch_job(entry, ps, st if st is not None else {}, now, text)
        if not ok:
            return False, out
    else:
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
        owner.append_log(entry["project"], "supervisor", "woke owner: %s%s" % (reason, " (%s)" % out if jobs else ""))
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
    jobs = entry.get("engine") == "jobs"
    job = entry.get("job") if jobs else None
    if jobs and isinstance(ps.get("job"), dict) and (not job or job.get("id") != ps["job"].get("id")):
        job = {"id": ps["job"].get("id"), "provider": ps["job"].get("provider"), "state": "launching", "min": 0,
               "running": True}       # started in this cycle
    if jobs and int(ps.get("turn_fails") or 0) >= len(chain()):
        flags.append("turns-failing")       # every provider of the chain had its try and none delivered
    if job and job.get("running") and (job.get("min") or 0) > 2 * HUNG_MIN:
        flags.append("job-stuck")           # it cannot be read or cancelled: nothing starts beside it until it can
    return {
        "project": entry["project"], "project_key": entry["project_key"], "owner_key": entry["owner_key"],
        "engine": entry.get("engine") or "session", "job": job, "last_job": (ps.get("last_job") or None) if jobs else None,
        "state": st.get("state"), "summary": st.get("summary"), "updated_at": st.get("updated_at"),
        "stale": entry["stale"], "problem": entry["problem"],
        "last_shipped": ship, "days_since_shipped": round((now - ship_ts) / 86400, 1) if ship_ts else None,
        "decisions_open": len(open_decs), "oldest_decision_days": oldest,
        "idle_checkins": int(ps.get("idle_checkins") or 0), "wakes_today": wakes_today(ps, now),
        "last_wake": {"reason": lw.get("reason"), "when": lw.get("when")} if lw else None,
        "flags": flags, "paused": paused,
        "working_now": entry["live"]["working_now"] or bool(job and job["running"]), "idle_min": entry["live"]["idle_min"],
    }


def desk_row(d, now):
    """One problem on the supervisor's desk (holder "supervisor" in the Navigator's inbox) for the digest."""
    start = d.get("sorted_at") if isinstance(d.get("sorted_at"), (int, float)) else d.get("asked_at")
    lanes = [str(d.get("lane") or d.get("id"))] + [str(x.get("lane") or x.get("id")) for x in d.get("dupes") or []]
    return {"id": str(d.get("id")), "topic": str(d.get("topic") or ""), "question": " ".join(str(d.get("question") or "").split()),
            "raised_by": sorted(set(lanes)),
            "days": round((now - float(start)) / 86400, 1) if isinstance(start, (int, float)) else None}


def digest_md(d, now):
    out = ["# Supervisor digest %s%s" % (iso(now)[:16].replace("T", " "), " (dry run)" if d["dry_run"] else "")]
    od = d["decisions_overall"]
    out.append("Uri's open decisions: %d%s." % (od["open"], ", oldest %.1fd" % od["oldest_days"]
                                                 if od["oldest_days"] is not None else ""))
    jb = d.get("jobs")
    if jb:
        prov = "; ".join("%s %d delivered / %d failed%s" % (
            k, v["ok"], v["bad"], " (resting: %d failed in a row)" % v["streak"] if v["cooling"] else "")
            for k, v in jb["providers"].items())
        out.append("Owner turns run as free-model jobs: %d running, at most %d at once%s%s. Providers: %s."
                   % (jb["running"], jb["max"], " (night rules)" if jb.get("night") else "",
                      "; new turns are held, " + jb["held"] if jb.get("held") else "", prov))
        out.append("A turn that ends without a status already moves that owner to the next provider. To steer an "
                   "owner, do not type into a session: `python3 %s supervisor feedback <project> \"<what to do "
                   "differently>\"` opens its next turn, `supervisor wake <project>` starts one now." % NAV)
    desk = d.get("desk") or []
    if desk:
        out.append("On YOUR desk, not Uri's (%d): problems the owners raised. Solve each one; it goes to Uri only if "
                   "it is still open after two days or past its deadline. When solved, close it for every project "
                   "that raised it: `NAV_ACTOR=copilot python3 %s decide <id> other --note \"solved: <what you did>\"`."
                   % (len(desk), NAV))
        for x in desk:
            out.append("- %s [%s] raised by %s%s: %s" % (
                x["id"], x["topic"], ", ".join(x["raised_by"]),
                ", %.1fd on your desk" % x["days"] if x["days"] is not None else "", x["question"]))
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
        if r.get("engine") == "jobs":
            job, lj = r.get("job") or {}, r.get("last_job") or {}
            live = ("turn running (%s)" % job_words(job) if job.get("running")
                    else ("idle %s" % ago(now - lv["idle_min"] * 60, now) if lv["idle_min"] is not None else "no turn yet")
                    + ("; last turn on %s ended %s%s" % (lj.get("provider"), lj.get("ended"),
                                                        "" if lj.get("delivered") else " WITHOUT a status") if lj else ""))
        out.append("")
        out.append("## %s [%s] %s" % (o["project"], st.get("state") or "no status", live))
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

def beat(now):
    """The supervisor is on duty: stamp state.json. The Navigator keeps problems on its desk while this is fresh."""
    with state_lock():
        cur = load_state()
        cur["heartbeat"] = iso(now)
        write_json(path("state.json"), cur)


def raced(cur, slug, ps):
    """Why a job for this owner must not start, or stay, after all: a pause, a stop or a manual wake landed in
    state.json (cur) during this cycle. None: nothing did."""
    if cur.get("paused") or slug in (cur.get("paused_projects") or []):
        return "paused meanwhile"
    latest = (cur.get("projects") or {}).get(slug) or {}
    if ((latest.get("last_wake") or {}).get("ts") or 0) > ((ps.get("last_wake") or {}).get("ts") or 0):
        return "woken meanwhile"
    return None


JOB_KEYS = ("job", "last_job", "turn_fails", "provider_idx")      # an owner's job bookkeeping in its row of state.json
WAKE_KEYS = ("last_wake", "wakes", "answered_sent")


def keep(slug, ps, before):
    """Record one owner's row in state.json right after its job started, so the job is known even if this cycle
    dies. If a pause, a stop or another wake landed while the job was starting (before: the row as it was before
    this wake), the new job is cancelled and the newer row stays. -> why it was cancelled, or None.
    The lock is never held while Outsourcerer runs: a pause or a stop must always get through at once."""
    with state_lock():
        cur = load_state()
        why = raced(cur, slug, before)
        if not why:
            cur.setdefault("projects", {})[slug] = ps
            write_json(path("state.json"), cur)
            return None
    job = ps.pop("job")
    gone = osrc_run(["cancel", str(job["id"])], timeout=60)[0] == 0
    with state_lock():
        cur = load_state()
        row = cur.setdefault("projects", {}).setdefault(slug, {})
        if not gone and not row.get("job"):
            row["job"] = dict(job, stopped=True)    # it could not be cancelled: keep it known, so a stop or the
            write_json(path("state.json"), cur)     # next cycle ends it
        for k in JOB_KEYS + WAKE_KEYS:              # this wake never happened: the newer row counts
            if k in row:
                ps[k] = row[k]
            else:
                ps.pop(k, None)
    return why


def merge_providers(cur, base, mine):
    """The provider counts to save: this cycle's turns (mine, started from base) on top of what is in the file now
    (cur), where a manual wake may have scored meanwhile."""
    out = dict(cur or {})
    for label, m in (mine or {}).items():
        b, c = (base or {}).get(label), out.get(label)
        if c is None or c == b:
            out[label] = m              # nobody else touched it
        elif m != b:                    # both did: add this cycle's turns to theirs, keep this cycle's streak
            c = dict(c)
            for k in ("ok", "bad"):
                c[k] = int(c.get(k) or 0) + int(m.get(k) or 0) - int((b or {}).get(k) or 0)
            c.update({k: m[k] for k in ("streak", "last_ok", "last_bad") if k in m})
            out[label] = c
    return out


@contextlib.contextmanager
def cycle_lock():
    """One cycle at a time (a slow one can outlast the 15 minutes): yields False while another one runs."""
    os.makedirs(sup_dir(), exist_ok=True)
    try:
        import fcntl
    except ImportError:         # not on macOS/Linux: no lock
        yield True
        return
    with open(path("cycle.lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def providers_view(st, now):
    """{label: {ok, bad, streak, cooling}} for the chain, in its order: the digest and the board show it."""
    health = st.get("providers") or {}
    out = {}
    for label, _, _, _ in chain():
        h = health.get(label) if isinstance(health.get(label), dict) else {}
        out[label] = {"ok": int(h.get("ok") or 0), "bad": int(h.get("bad") or 0), "streak": int(h.get("streak") or 0),
                      "cooling": cooling(h, now)}
    return out


def providers_down_alert(st, now, dry):
    """One loud alert when every provider of the chain is cooling off: no owner can take a turn.
    -> its record for the digest, or None."""
    pv = providers_view(st, now)
    if not pv or not all(v["cooling"] for v in pv.values()):
        return None
    rec = {"decision": PROVIDERS_DOWN_KEY, "reasons": ["every provider failed %d turns in a row" % COOL_STREAK],
           "raised": False}
    try:
        skip = alert_skip(PROVIDERS_DOWN_KEY, get("/api/alerts"), now)
    except Exception:           # an older Navigator or a hiccup: its own dedupe still holds
        skip = None
    if not skip and not dry:
        code, _ = nav("alert", "raise", "--title", "No AI provider is working: the owners are stopped",
                      "--body", "Every provider failed its last %d turns (%s). The supervisor keeps trying by itself; "
                      "check the accounts and quotas." % (COOL_STREAK, ", ".join(pv)), "--key", PROVIDERS_DOWN_KEY)
        if code:
            skip = "raise failed (exit %d)" % code
    if skip:
        rec["skip"] = skip
    else:
        rec["raised"] = True
    return rec


def cycle(dry_run=False, now=None):
    now = now or time.time()
    dry = dry_run or dry_env()
    if dry:                     # changes nothing: it may run beside a real cycle
        return run_cycle(dry, now)
    with cycle_lock() as free:
        if free:
            return run_cycle(dry, now)
    try:
        log_cycle("skip: the previous cycle is still running", now)
    except OSError:
        pass
    return 0


def run_cycle(dry, now):
    tag = " (dry run)" if dry else ""
    try:
        st = load_state()
        base_providers = json.loads(json.dumps(st.get("providers") or {}))
        night = quiet(now)
        if night and not st.get("night"):
            if not dry and not st.get("paused"):
                beat(now)       # asleep, not gone: problems stay on its desk until the morning
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
        # The gate (ccc-navigator/decisions.py): approvals going ahead on a default, repeats of one problem and
        # problems on the supervisor's desk are not Uri's now. He is counted and alerted only for what is his.
        mine = [x for x in open_list if x.get("for_uri") is not False]
        desk = [desk_row(x, now) for x in open_list if x.get("holder") == "supervisor"]
        asked = [float(x["asked_at"]) for x in mine if isinstance(x.get("asked_at"), (int, float))]
        paused_projects = set(st.get("paused_projects") or [])
        projects = st.get("projects") or {}
        todo = sorted((tree.get("owners") or {}).items())
        any_jobs = any(engine_of(owner.slug_of(k), st) == "jobs" for k, _ in todo)
        listing = jobs_listing() if any_jobs else {}
        seen, failed = {}, {}       # slug -> (entry, its row of state.json) | the exception that stopped it
        for pkey, info in todo:
            slug = owner.slug_of(pkey)
            try:
                ps = json.loads(json.dumps(projects.get(slug) or {}))   # private copy
                seen[slug] = (observe(pkey, info, tree, answers, open_map, board_rows, ps, now, st, listing, dry), ps)
            except Exception as e:
                failed[slug] = e
        running = sum(1 for e, _ in seen.values() if (e.get("job") or {}).get("running"))
        cap, busy, result = jobs_max(night), None, {}
        # wakes, the owner that waited longest first: a job needs a free place and a Mac that can take it
        for slug in sorted(seen, key=lambda x: ((seen[x][1].get("last_wake") or {}).get("ts") or 0, x)):
            entry, ps = seen[slug]
            try:
                paused = slug in paused_projects
                jobs = entry["engine"] == "jobs"
                reason = wake_reason(entry, ps, now)
                skip = skip_reason(entry, reason, ps, now, paused) if reason else "nothing due"
                if reason and not skip and night and not jobs:
                    skip = "resting until 07:00"        # night mode keeps the jobs going; nobody types into a session at night
                if reason and not skip and jobs:
                    if running >= cap:
                        skip = "no free place: %d jobs running" % running
                    else:
                        busy = overloaded() if busy is None else busy
                        skip = ("the Mac is busy: " + busy) if busy else None if dry else raced(load_state(), slug, ps)
                woke = False
                if reason and not skip:
                    before = json.loads(json.dumps(ps))
                    woke, out = do_wake(entry, reason, ps, now, dry, st=st, night=night)
                    if not woke:
                        skip = out
                    elif jobs:
                        late = None if dry else keep(slug, ps, before)
                        if late:            # a pause, a stop or a manual wake landed while the job was starting
                            woke, skip = False, "started, then cancelled: " + late
                            try:
                                owner.append_log(slug, "supervisor", "that job was cancelled at once: " + late)
                            except (OSError, ValueError):
                                pass
                        if not late or ps.get("job"):       # its own job, or the one that stayed in its place
                            running += 1
                entry["wake"] = {"reason": reason, "skip": skip, "dry_run": dry}
                entry["paused"] = paused
                entry["register"] = register_row(entry, ps, now, paused)
                result[slug] = (reason, skip, woke)
            except Exception as e:
                failed[slug] = e
        owners, wakes, skips, rows, done = [], [], [], [], {}
        for pkey, info in todo:         # the digest and the register list the owners in project order
            slug = owner.slug_of(pkey)
            if slug in failed:
                e = failed[slug]
                errors.append({"project": slug, "error": "%s: %s" % (type(e).__name__, e)})
                rows.append({"project": slug, "project_key": pkey, "owner_key": (info or {}).get("key"),
                             "error": str(e), "flags": ["error"]})
                continue
            entry, ps = seen[slug]
            reason, skip, woke = result[slug]
            if woke:
                wakes.append({"project": slug, "reason": reason, "key": entry["owner_key"]})
            if skip:
                skips.append({"project": slug, "reason": reason, "skip": skip})
            rows.append(entry["register"])
            owners.append(entry)
            done[slug] = ps
        jobs_info = {"running": running, "max": cap, "held": busy or "", "night": night,
                     "providers": providers_view(st, now)} if any_jobs else None
        try:                        # loud alerts stay silent while Uri sleeps (night mode keeps only the work going)
            alerts = [] if night else raise_alerts(mine, owners, now, dry)
            down = providers_down_alert(st, now, dry) if any_jobs and not night else None
            if down:
                alerts.append(down)
        except Exception as e:      # alerts must never stop the digest and register
            alerts = []
            errors.append({"project": "(alerts)", "error": "%s: %s" % (type(e).__name__, e)})
        digest = {"generated_at": iso(now), "ts": now, "dry_run": dry,
                  "decisions_overall": {"open": len(mine),
                                        "oldest_days": round((now - min(asked)) / 86400, 1) if asked else None},
                  "desk": desk, "jobs": jobs_info,
                  "owners": owners, "wakes": wakes, "skips": skips, "alerts": alerts, "errors": errors}
        write_json(path("digest.json"), digest)
        owner.write_atomic(path("digest.md"), digest_md(digest, now))
        write_json(path("register.json"), {"generated_at": iso(now), "ts": now, "dry_run": dry, "rows": rows,
                                           "jobs": jobs_info})
        if not dry:
            with state_lock():      # merge into the current file: a pause or a manual wake may have landed meanwhile
                cur = load_state()
                cur_projects = cur.setdefault("projects", {})
                for slug, ps in done.items():
                    old = cur_projects.get(slug) or {}
                    if (old.get("last_wake") or {}).get("ts", 0) > (ps.get("last_wake") or {}).get("ts", 0):
                        for k in WAKE_KEYS:
                            ps[k] = old.get(k)
                        for k in JOB_KEYS:              # the job that manual wake started, and what it settled
                            if k in old:
                                ps[k] = old[k]
                    oj, pj = old.get("job"), ps.get("job")
                    if isinstance(oj, dict) and isinstance(pj, dict) and oj.get("stopped") and oj.get("id") == pj.get("id"):
                        pj["stopped"] = True            # a stop landed meanwhile: not the provider's failure
                    cur_projects[slug] = ps
                if any_jobs:
                    cur["providers"] = merge_providers(cur.get("providers"), base_providers, st.get("providers"))
                cur["last_cycle"] = cur["heartbeat"] = iso(now)
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


def wake_cmd(project, note="", now=None):
    """Manual wake: same caps (pause, 30 min, 24 a day; on the jobs engine one job per owner). -> result dict."""
    now = now or time.time()
    dry = dry_env()
    tree = get("/api/tree?window=7d")
    pkey, info = resolve_project(tree, project)
    slug = owner.slug_of(pkey)
    dec = get("/api/decisions")
    answers, open_map = load_answers(dec), {str(x.get("id")): x for x in dec.get("open") or []}
    if engine_of(slug, load_state()) == "jobs":
        res = wake_job(pkey, info, tree, answers, open_map, note, now, dry)
    else:
        with state_lock():
            st = load_state()
            ps = json.loads(json.dumps((st.get("projects") or {}).get(slug) or {}))
            entry = observe(pkey, info, tree, answers, open_map, {}, dict(ps), now, st)
            # observation is not persisted: only the cycle counts check-ins
            paused = bool(st.get("paused")) or slug in (st.get("paused_projects") or [])
            skip = cap_reason(ps, now, paused) or (None if entry["owner_key"] else "no owner session")
            res = {"ok": False, "project": slug, "reason": "manual", "dry_run": dry}
            if skip:
                res["skip"] = skip
            else:
                ok, out = do_wake(entry, "manual", ps, now, dry, note)
                res.update(ok=ok, output=out)
                if ok and not dry:
                    st.setdefault("projects", {})[slug] = ps
                    write_json(path("state.json"), st)
    log_cycle("manual wake %s by %s: %s%s" % (slug, actor(), "sent" if res["ok"] else res.get("skip") or res.get("output"),
                                              " (dry run)" if dry else ""), now)
    return res


def wake_job(pkey, info, tree, answers, open_map, note, now, dry):
    """wake_cmd on the jobs engine. Outsourcerer never runs under the state lock (a pause or a stop must get
    through at once): the row is read, its job settled and the new one started, then keep() writes the row back
    and cancels the new job if a pause, a stop or the cycle's own wake landed meanwhile."""
    slug = owner.slug_of(pkey)
    st = load_state()
    base = json.loads(json.dumps(st.get("providers") or {}))
    ps = json.loads(json.dumps((st.get("projects") or {}).get(slug) or {}))
    before = json.loads(json.dumps(ps))
    seen = dict(ps)
    entry = observe(pkey, info, tree, answers, open_map, {}, seen, now, st, jobs_listing(), dry)
    for k in JOB_KEYS:              # the observation is not persisted (only the cycle counts check-ins), except what
        if k in seen:               # became of the owner's job
            ps[k] = seen[k]
        else:
            ps.pop(k, None)
    paused = bool(st.get("paused")) or slug in (st.get("paused_projects") or [])
    skip = cap_reason(ps, now, paused)
    if not skip and (entry.get("job") or {}).get("running"):
        skip = "its turn is running (%s)" % job_words(entry["job"])
    if not skip and entry["live"].get("in_session"):
        skip = "its session is at work"
    res = {"ok": False, "project": slug, "reason": "manual", "dry_run": dry}
    if skip:
        res["skip"] = skip
        return res
    ok, out = do_wake(entry, "manual", ps, now, dry, note, st=st, night=quiet(now))
    res.update(ok=ok, output=out)
    if dry:
        return res
    if ok:
        late = keep(slug, ps, before)
        if late:
            res.update(ok=False, skip="started, then cancelled: " + late)
            res.pop("output", None)
    if (st.get("providers") or {}) != base:        # what this wake learned about the providers
        with state_lock():
            cur = load_state()
            cur["providers"] = merge_providers(cur.get("providers"), base, st.get("providers"))
            write_json(path("state.json"), cur)
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


def set_night(on):
    """Night mode: the cycle keeps going through the quiet hours, under the night rules. Loud alerts stay silent."""
    with state_lock():
        st = load_state()
        st["night"] = bool(on)
        write_json(path("state.json"), st)
    log_cycle("night %s by %s" % ("on" if on else "off", actor()))
    return {"ok": True, "night": bool(on)}


def engine_cmd(project, eng):
    """How one owner gets its turn, or every owner (`all`: also the default for owners registered later)."""
    e = _engine(eng)
    if not e:
        raise ValueError("engine must be one of: %s" % ", ".join(ENGINES))
    everyone = str(project or "").strip().lower() == "all"
    tree = get("/api/tree?window=7d")
    slugs = (sorted(owner.slug_of(k) for k in tree.get("owners") or {}) if everyone
             else [owner.slug_of(resolve_project(tree, project)[0])])
    for slug in slugs:
        os.makedirs(owner.folder(slug), exist_ok=True)
        owner.write_atomic(os.path.join(owner.folder(slug), "engine"), e + "\n")
    if everyone:
        with state_lock():
            st = load_state()
            st["engine_default"] = e
            write_json(path("state.json"), st)
    log_cycle("engine %s -> %s by %s" % ("all" if everyone else slugs[0], e, actor()))
    return {"ok": True, "engine": e, "projects": slugs}


def feedback_cmd(project, text):
    """Append a note to owners/<slug>/supervisor-feedback.md: the end of that file opens the owner's next job."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("feedback needs a text")
    try:
        slug = owner.slug_of(resolve_project(get("/api/tree?window=7d"), project)[0])
    except OSError:             # Navigator down: take the folder name as given
        slug = owner.slug_of(project)
    if not os.path.isdir(owner.folder(slug)):
        raise ValueError("no owner folder for %r" % project)
    with open(os.path.join(owner.folder(slug), "supervisor-feedback.md"), "a") as f:
        f.write("- %s [%s] %s\n" % (datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M"), actor(), text))
    log_cycle("feedback for %s by %s" % (slug, actor()))
    return {"ok": True, "project": slug, "engine": engine_of(slug, load_state())}


def stop_all():
    """Pause everything and cancel every job that runs an owner's turn or works in an owner's folder."""
    with state_lock():
        st = load_state()
        st["paused"] = True
        for ps in (st.get("projects") or {}).values():
            if isinstance(ps, dict) and isinstance(ps.get("job"), dict):
                ps["job"]["stopped"] = True         # cancelled by hand: not the provider's failure
        write_json(path("state.json"), st)
    jobs = {str(ps["job"]["id"]): slug for slug, ps in (st.get("projects") or {}).items()
            if isinstance(ps, dict) and isinstance(ps.get("job"), dict) and ps["job"].get("id")}
    listing = jobs_listing()
    root = os.path.realpath(owner.root()) + os.sep
    for jid, j in listing.items():      # and the workers the owners started, as far as Outsourcerer lists them
        cwd = os.path.realpath(str(j.get("cwd") or os.sep)) + os.sep
        if str(j.get("status") or "").lower() not in JOB_ENDED + ("",) and cwd.startswith(root) and cwd != root:
            jobs.setdefault(jid, cwd[len(root):].split(os.sep)[0])
    done, failed = [], []
    for jid, slug in sorted(jobs.items()):
        if str((listing.get(jid) or {}).get("status") or "").lower() in JOB_ENDED:
            continue                    # it ended by itself (a job that is not listed is cancelled without asking first)
        (failed if osrc_run(["cancel", jid], timeout=20)[0] else done).append({"project": slug, "job": jid})
    for slug in sorted({x["project"] for x in done}):
        try:
            owner.append_log(slug, "supervisor", "stopped by %s: the running job was cancelled" % actor())
        except (OSError, ValueError):
            pass
    gone = {x["job"] for x in done}
    reg = owner.read_json(path("register.json"))
    if gone and isinstance(reg, dict):          # the board reads the register, and no cycle will rewrite it while paused
        for r in reg.get("rows") or []:
            if isinstance(r.get("job"), dict) and r["job"].get("id") in gone:
                r["job"].update(running=False, state="canceled")
                r["working_now"] = False
        if isinstance(reg.get("jobs"), dict):
            reg["jobs"]["running"] = sum(1 for r in reg.get("rows") or [] if (r.get("job") or {}).get("running"))
        write_json(path("register.json"), reg)
    log_cycle("stop by %s: paused; cancelled %d job(s)%s" % (
        actor(), len(done), "; could not cancel %s" % ", ".join(x["job"] for x in failed) if failed else ""))
    return {"ok": True, "paused": True, "cancelled": done, "failed": failed}


def view():
    """What GET /api/supervisor returns: {register, digest_at, paused, paused_projects, cycles_tail, last_cycle,
    night, engine_default, jobs}. jobs (None when no owner runs as jobs): {running, max, held, night, providers}."""
    reg = owner.read_json(path("register.json")) or {}
    dg = owner.read_json(path("digest.json")) or {}
    st = load_state()
    return {"register": reg.get("rows") or [], "digest_at": dg.get("generated_at"),
            "paused": bool(st.get("paused")), "paused_projects": st.get("paused_projects") or [],
            "last_cycle": st.get("last_cycle"), "cycles_tail": cycles_tail(),
            "night": bool(st.get("night")), "engine_default": _engine(st.get("engine_default")) or "session",
            "jobs": reg.get("jobs")}


def status_table(v):
    out = ["supervisor %s%s; last cycle %s; digest %s" % (
        "PAUSED" if v["paused"] else "running", "; night mode on" if v.get("night") else "",
        v["last_cycle"] or "never", v["digest_at"] or "none")]
    jb = v.get("jobs")
    if jb:
        out.append("jobs: %d running of %d%s; providers: %s" % (
            jb["running"], jb["max"], "; new turns held, " + jb["held"] if jb.get("held") else "",
            ", ".join("%s %d/%d%s" % (k, x["ok"], x["ok"] + x["bad"], " resting" if x["cooling"] else "")
                      for k, x in (jb.get("providers") or {}).items())))
    for r in v["register"]:
        if r.get("error"):
            out.append("%-18s ERROR %s" % (r["project"], r["error"]))
            continue
        lw = r.get("last_wake") or {}
        job, lj = r.get("job") or {}, r.get("last_job") or {}
        turn = ("{%s} " % job_words(job) if job.get("running") else "{last turn on %s: %s%s} " % (
            lj.get("provider"), lj.get("ended"), "" if lj.get("delivered") else ", no status") if lj else
            "{jobs} " if r.get("engine") == "jobs" else "")
        out.append("%-18s %-15s shipped %-5s dec %d%-7s idle %d wakes %d last %-12s %s%s%s" % (
            r["project"], r.get("state") or "no status",
            "%sd" % r["days_since_shipped"] if r.get("days_since_shipped") is not None else "-",
            r["decisions_open"], " (%sd)" % r["oldest_decision_days"] if r.get("oldest_decision_days") is not None else "",
            r["idle_checkins"], r["wakes_today"], lw.get("reason") or "-", turn,
            ("[" + ", ".join(r["flags"]) + "] ") if r.get("flags") else "",
            "(paused) " if r.get("paused") else ""))
    return "\n".join(out)


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
    e = sub.add_parser("engine", help="how an owner gets its turn: session (a message) or jobs (a free-model job)")
    e.add_argument("project", help="project key, folder name or display name; or `all`")
    e.add_argument("engine", choices=ENGINES + ("claude",))
    e.add_argument("--json", action="store_true")
    n = sub.add_parser("night", help="keep the cycle going through 00:00-07:00, under the night rules")
    n.add_argument("state", choices=("on", "off"))
    n.add_argument("--json", action="store_true")
    f = sub.add_parser("feedback", help="a note that opens that owner's next turn (jobs engine)")
    f.add_argument("project")
    f.add_argument("text")
    f.add_argument("--json", action="store_true")
    k = sub.add_parser("stop", help="pause everything and cancel every owner job that is running")
    k.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "cycle":
        return cycle(a.dry_run)
    if a.cmd == "status":
        v = view()
        print(json.dumps(v, indent=1, ensure_ascii=False) if a.json else status_table(v))
        return 0
    try:
        res = (wake_cmd(a.project, a.reason) if a.cmd == "wake" else engine_cmd(a.project, a.engine) if a.cmd == "engine"
               else set_night(a.state == "on") if a.cmd == "night" else feedback_cmd(a.project, a.text) if a.cmd == "feedback"
               else stop_all() if a.cmd == "stop" else set_pause(a.project, a.cmd == "pause"))
    except (ValueError, OSError) as e:
        res = {"ok": False, "error": str(e)}
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(res.get("error") or res.get("skip") or ("%s: ok" % a.cmd if res.get("ok") else json.dumps(res)))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
