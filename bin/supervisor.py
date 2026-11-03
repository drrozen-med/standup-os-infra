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
no-status, blocked-due, keep-moving; first match wins), rewrite register.json, keep state.json and
append one line to cycles.log, all under SUPERVISOR_DIR (default ~/.ccc-navigator/supervisor).
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
GAP_MIN = 30            # never wake an owner woken this recently
MAX_WAKES = 24          # per owner per local day
DECISION_OLD_DAYS = 7   # flag decision-7d
LOG_TAIL = 5            # log.md lines in the digest
LOG_CHARS = 240         # digest.md cuts longer log lines (digest.json keeps them whole)
CYCLES_TAIL = 20        # cycles.log lines in `status --json` / GET /api/supervisor
URI_ACTORS = ("you", "uri", "")
REASONS = ("answered", "no-status", "blocked-due", "keep-moving")


def sup_dir():
    return os.environ.get("SUPERVISOR_DIR") or os.path.expanduser("~/.ccc-navigator/supervisor")


def path(name):
    return os.path.join(sup_dir(), name)


def nav_url():
    return os.environ.get("CCC_NAV_URL", "http://127.0.0.1:8095")


def dry_env():
    return os.environ.get("SUPERVISOR_DRY_RUN", "") not in ("", "0")


# ---------------------------------------------------------------- io (mocked in tests)

def get(p):
    with urllib.request.urlopen(nav_url() + p, timeout=60) as r:
        return json.load(r)


def nav(*args):
    """Run nav.py as the supervisor -> (exit code, first 200 chars of its output)."""
    r = subprocess.run([sys.executable, NAV] + list(args), capture_output=True, text=True, timeout=90,
                       env=dict(os.environ, NAV_ACTOR="supervisor"))
    return r.returncode, (r.stdout or r.stderr or "").strip()[:200].replace("\n", " ")


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


# ---------------------------------------------------------------- one owner

def observe(pkey, info, tree, answers, open_map, board_rows, ps, now):
    """Digest entry for one owner; updates ps (its row in state.json) with the observation:
    idle_checkins, seen_updated_at, last_shipped."""
    slug = owner.slug_of(pkey)
    key = (info or {}).get("key") or ""
    status = owner.read_json(os.path.join(owner.folder(slug), "status.json"))
    status = status if isinstance(status, dict) else None
    upd = (status or {}).get("updated_at")
    upd_ts = epoch(upd)
    live = live_of(find_item(tree, key), now)

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
        "project": slug, "project_key": pkey, "owner_key": key, "live": live,
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
    if not entry["owner_key"]:
        return "no owner session"
    if not entry["live"]["found"]:
        return "owner session not in the Navigator"
    if entry["live"]["working_now"]:
        return "working now"
    if (entry["status"] or {}).get("state") == "waiting-on-uri" and reason != "answered":
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
    elif reason == "keep-moving":
        body = (head + " Your session has been idle %d min while your state is %s. Your next action was: \"%s\". "
                "Do it now, or take the next item of your backlog toward the goals."
                % (entry["live"]["idle_min"] or 0, st.get("state"), st.get("next") or "-"))
    else:   # manual
        body = head + " " + (note or "Uri asked for a check-in: move your next item forward and report.")
    return "%s\nWhen done: %s. End with owner.py status set" % (body, cmd)


def do_wake(entry, reason, ps, now, dry, note=""):
    """Send one wake (unless dry) and record it in ps and log.md. -> (sent ok, short output)."""
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
    return {
        "project": entry["project"], "project_key": entry["project_key"], "owner_key": entry["owner_key"],
        "state": st.get("state"), "summary": st.get("summary"), "updated_at": st.get("updated_at"),
        "stale": entry["stale"], "problem": entry["problem"],
        "last_shipped": ship, "days_since_shipped": round((now - ship_ts) / 86400, 1) if ship_ts else None,
        "decisions_open": len(open_decs), "oldest_decision_days": oldest,
        "idle_checkins": int(ps.get("idle_checkins") or 0), "wakes_today": wakes_today(ps, now),
        "last_wake": {"reason": lw.get("reason"), "when": lw.get("when")} if lw else None,
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
                entry = observe(pkey, info, tree, answers, open_map, board_rows, ps, now)
                paused = slug in paused_projects
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
                entry["wake"] = {"reason": reason, "skip": skip, "dry_run": dry}
                entry["register"] = register_row(entry, ps, now, paused)
                rows.append(entry["register"])
                owners.append(entry)
                done[slug] = ps
            except Exception as e:
                errors.append({"project": slug, "error": "%s: %s" % (type(e).__name__, e)})
                rows.append({"project": slug, "project_key": pkey, "owner_key": (info or {}).get("key"),
                             "error": str(e), "flags": ["error"]})
        digest = {"generated_at": iso(now), "ts": now, "dry_run": dry,
                  "decisions_overall": {"open": len(open_list),
                                        "oldest_days": round((now - min(asked)) / 86400, 1) if asked else None},
                  "owners": owners, "wakes": wakes, "skips": skips, "errors": errors}
        write_json(path("digest.json"), digest)
        owner.write_atomic(path("digest.md"), digest_md(digest, now))
        write_json(path("register.json"), {"generated_at": iso(now), "ts": now, "dry_run": dry, "rows": rows})
        if not dry:
            with state_lock():      # merge into the current file: a pause or a manual wake may have landed meanwhile
                cur = load_state()
                cur_projects = cur.setdefault("projects", {})
                for slug, ps in done.items():
                    old = cur_projects.get(slug) or {}
                    if (old.get("last_wake") or {}).get("ts", 0) > (ps.get("last_wake") or {}).get("ts", 0):
                        for k in ("last_wake", "wakes", "answered_sent"):
                            ps[k] = old.get(k)
                    cur_projects[slug] = ps
                cur["last_cycle"] = iso(now)
                write_json(path("state.json"), cur)
        line = "cycle%s: %d owners; %s %s; skips %s%s" % (
            tag, len(rows), "would wake" if dry else "woke",
            ", ".join("%s (%s)" % (w["project"], w["reason"]) for w in wakes) or "none",
            "; ".join("%s: %s" % (s["project"], s["skip"]) for s in skips) or "none",
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
    """Manual wake: same caps (pause, 30 min, 24 a day). -> result dict."""
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
                        {}, dict(ps), now)       # observation is not persisted: only the cycle counts check-ins
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
    log_cycle("manual wake %s: %s%s" % (slug, "sent" if res["ok"] else res.get("skip") or res.get("output"),
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
    log_cycle("%s %s" % ("pause" if on else "resume", slug or "all"))
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
        out.append("%-18s %-15s shipped %-5s dec %d%-7s idle %d wakes %d last %-12s %s%s" % (
            r["project"], r.get("state") or "no status",
            "%sd" % r["days_since_shipped"] if r.get("days_since_shipped") is not None else "-",
            r["decisions_open"], " (%sd)" % r["oldest_decision_days"] if r.get("oldest_decision_days") is not None else "",
            r["idle_checkins"], r["wakes_today"], lw.get("reason") or "-",
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
    a = p.parse_args(argv)
    if a.cmd == "cycle":
        return cycle(a.dry_run)
    if a.cmd == "status":
        v = view()
        print(json.dumps(v, indent=1, ensure_ascii=False) if a.json else status_table(v))
        return 0
    try:
        res = wake_cmd(a.project, a.reason) if a.cmd == "wake" else set_pause(a.project, a.cmd == "pause")
    except (ValueError, OSError) as e:
        res = {"ok": False, "error": str(e)}
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(res.get("error") or res.get("skip") or ("%s: ok" % a.cmd if res.get("ok") else json.dumps(res)))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
