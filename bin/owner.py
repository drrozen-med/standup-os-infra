#!/usr/bin/env python3
"""Project owners (masterplan #79, contract: docs/owners.md).

Each project has an AI owner whose memory lives in ~/.ccc-navigator/owners/<slug>/:
goals.md (Uri), log.md (append-only), status.json (this tool only), scoreboard.json
(bin/owner-scoreboard.py).

  owner.py status set SLUG --json '<obj>' [--session KEY]   validate and write status.json
  owner.py status show SLUG                                 print status.json
  owner.py log SLUG [--actor A] TEXT                        append one log line
  owner.py brief SLUG [--session KEY]                       the prompt that starts or restarts the owner
  owner.py ls [--json]                                      every owner folder and its status

`--json -` reads the object from stdin (no shell quoting problems), `--json @FILE` from a file.
A rejected status writes nothing and exits 1, printing every reason.
Env OWNER_DIR overrides the root, OWNER_SESSION is the default --session.
"""
import argparse
import datetime
import json
import os
import re
import sys

STATES = ("working", "waiting-on-uri", "blocked", "picking-next")
FIELDS = ("state", "summary", "doing", "next", "shipped", "needs_uri", "blocked")
SUMMARY_MAX = 120
LOG_TAIL = 10
# rule 1 of #79: these read as "no progress" unless the owner is properly blocked
PASSIVE = ("nothing changed", "nothing new", "no changes", "no progress", "still waiting", "stuck",
           "can't", "cannot", "unable to", "permission", "not possible from this environment")
ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
OWNER_PY = "~/projects/standup-os/bin/owner.py"
NAV = "~/projects/standup-os/ccc-navigator/nav.py"
RECALL = "~/.claude/total-recall/plugins/total-recall/brain.dist/brain"
OSRC = "~/.claude/plugins/cache/outsourcerer/outsourcerer/*/skills/outsourcerer/scripts/outsourcerer.sh"


def root():
    return os.environ.get("OWNER_DIR") or os.path.expanduser("~/.ccc-navigator/owners")


def slug_of(name):
    """Folder name for a project: the basename of its navigator key without a folder:/desktop: prefix,
    path-safe (same rule as owner_slug in ccc-navigator/navigator.py)."""
    base = re.sub(r"^[a-z]+:", "", os.path.basename(str(name or "").rstrip("/")))
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base).lstrip(".") or "_"


def folder(slug):
    return os.path.join(root(), slug_of(slug))


def now_local():
    return datetime.datetime.now().astimezone()


# ---------------------------------------------------------------- files

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_log(slug, actor, text, when=None):
    """One line `- YYYY-MM-DD HH:MM [actor] text` in log.md. -> the line."""
    actor = str(actor or "owner").strip().lower()
    if not ACTOR_RE.match(actor):
        raise ValueError("actor must match %s" % ACTOR_RE.pattern)
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("log text is empty")
    line = "- %s [%s] %s" % ((when or now_local()).strftime("%Y-%m-%d %H:%M"), actor, text)
    d = folder(slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "log.md"), "a") as f:
        f.write(line + "\n")
    return line


def log_tail(slug, n=LOG_TAIL):
    try:
        with open(os.path.join(folder(slug), "log.md")) as f:
            lines = [ln.rstrip("\n") for ln in f if ln.startswith("- ")]
    except OSError:
        return []
    return lines[-n:]


# ---------------------------------------------------------------- validation

def _text(v):
    return v.strip() if isinstance(v, str) else ""


def passive_hits(text):
    t = (text or "").lower().replace("’", "'")
    return [p for p in PASSIVE if re.search(r"(?<![a-z])" + re.escape(p), t)]


def blocked_problems(b):
    """Reasons a `blocked` object is not good enough (empty list = valid)."""
    if not isinstance(b, dict):
        return ["state is blocked but `blocked` is not an object with what, tried, unblocker, by"]
    out = []
    if not _text(b.get("what")):
        out.append("blocked.what is missing: say what is blocked")
    tried = b.get("tried") if isinstance(b.get("tried"), list) else []
    distinct = {" ".join(_text(t).lower().split()) for t in tried} - {""}
    if len(distinct) < 2:
        out.append("blocked.tried needs at least 2 different ways you tried (has %d)" % len(distinct))
    if not _text(b.get("unblocker")):
        out.append("blocked.unblocker is missing: supervisor, uri, or who else can unblock it")
    try:
        datetime.date.fromisoformat(_text(b.get("by")))
    except ValueError:
        out.append("blocked.by must be a date YYYY-MM-DD (by when it gets unblocked)")
    return out


def validate(obj):
    """-> (clean status without updated_at/session, [every reason it is rejected])."""
    if not isinstance(obj, dict):
        return None, ["status must be a JSON object"]
    reasons = ["missing field: %s" % f for f in FIELDS if f not in obj]
    state = obj.get("state")
    if "state" in obj and state not in STATES:
        reasons.append("state must be one of: %s" % ", ".join(STATES))
    for f in ("summary", "doing", "next"):
        if f in obj and not _text(obj.get(f)):
            reasons.append("%s is empty" % f)
    summary = _text(obj.get("summary"))
    if len(summary) > SUMMARY_MAX:
        reasons.append("summary is %d chars (max %d)" % (len(summary), SUMMARY_MAX))
    lists = {}
    for f in ("shipped", "needs_uri"):
        v = obj.get(f)
        if f in obj and not isinstance(v, list):
            reasons.append("%s must be a list" % f)
        lists[f] = [_text(x) for x in v if _text(x)] if isinstance(v, list) else []
    blocked_ok = False
    if state == "blocked":
        bp = blocked_problems(obj.get("blocked"))
        reasons += bp
        blocked_ok = not bp
    if state == "waiting-on-uri" and not lists["needs_uri"]:
        reasons.append("state is waiting-on-uri but needs_uri is empty: list the decision ids in the navigator inbox")
    if not blocked_ok and state != "waiting-on-uri":   # waiting-on-uri is backed by needs_uri decision ids
        for f in ("summary", "doing"):
            hits = passive_hits(obj.get(f) if isinstance(obj.get(f), str) else "")
            if hits:
                reasons.append('%s reads as passive ("%s"): say what you did and what is next, or set state '
                               'blocked with what, 2+ ways tried, unblocker and by' % (f, '", "'.join(hits)))
    if reasons:
        return None, reasons
    b = None
    if state == "blocked":
        b = obj["blocked"]
        b = {"what": _text(b.get("what")), "tried": [_text(t) for t in b.get("tried") or [] if _text(t)],
             "unblocker": _text(b.get("unblocker")), "by": _text(b.get("by"))}
    return {"state": state, "summary": summary, "doing": _text(obj["doing"]), "next": _text(obj["next"]),
            "shipped": lists["shipped"], "needs_uri": lists["needs_uri"], "blocked": b}, []


def set_status(slug, obj, session=None, when=None):
    """Validate and write status.json, then log it. -> (status or None, reasons)."""
    clean, reasons = validate(obj)
    if reasons:
        return None, reasons
    path = os.path.join(folder(slug), "status.json")
    prev = read_json(path) or {}
    when = when or now_local()
    session = (session or os.environ.get("OWNER_SESSION") or prev.get("session") or "").strip()
    status = dict({"updated_at": when.isoformat(timespec="seconds"), "session": session}, **clean)
    write_atomic(path, json.dumps(status, indent=1, ensure_ascii=False) + "\n")
    append_log(slug, "owner", "%s: %s" % (status["state"], status["summary"]), when)
    return status, []


# ---------------------------------------------------------------- brief

RULES = """1. No passive status. Your end-of-turn status is validated by code. "Nothing changed", "stuck",
   "can't from this environment" are rejected unless state is blocked with: what is blocked, 2+ different
   ways you tried, who can unblock it (supervisor, uri, ...) and by when.
2. Always a next move. Keep a ranked backlog toward the dated goals below. Idle means: take the next item.
   Two idle check-ins in a row and the supervisor steps in (re-brief, fresh session or reassign).
3. Environment and permission blocks are infra bugs, not dead ends. You have full permissions and the
   toolbelt below. "Can't do X here" becomes a fix ticket for the supervisor the same day, not a stop.
4. Clear decision rights. Decide alone what goals.md lets you decide. If in doubt and reversible: decide,
   do it, log it.
5. Knock until released. You try, then ask the supervisor; the supervisor decides or turns it into ONE
   decision for Uri with options, a deadline and a default. Only money, 2FA/credentials, messages to people
   and product direction are truly Uri's, and they reach him as a crisp decision, never a "stuck" note.
6. Scored on outcomes: days since something shipped, open decisions and their age, idle check-ins."""


def _section(title, body):
    return "## %s\n%s\n" % (title, body.rstrip() or "(none)")


def brief(slug, session=None):
    s = slug_of(slug)
    d = folder(s)
    try:
        with open(os.path.join(d, "goals.md")) as f:
            goals = f.read().strip()
    except OSError:
        goals = ""
    board = read_json(os.path.join(d, "scoreboard.json"))
    status = read_json(os.path.join(d, "status.json"))
    tail = log_tail(s)
    sess = session or os.environ.get("OWNER_SESSION") or (status or {}).get("session") or ""
    cmd = "python3 %s status set %s%s --json - <<'EOF'\n" % (OWNER_PY, s, " --session " + sess if sess else "")
    example = {"state": "working", "summary": "<one line Uri reads on the project row, <=120 chars>",
               "doing": "<what is in progress now>", "next": "<the next concrete action>",
               "shipped": ["<what moved since the last status, e.g. merged #123>"], "needs_uri": [], "blocked": None}
    parts = [
        "You are the owner of the project %s: its AI manager (CEO), not a worker. You own outcomes: the dated "
        "goals below. You plan, start and review workers (free/cheap models first), and you review every change "
        "before it merges. Your memory is the folder %s; a fresh session must be able to take over from it, so "
        "write what matters there, not only in this chat." % (s, d),
        "",
        "Before you plan, deploy or tell anyone how something works: read the repo's own CLAUDE.md and AGENTS.md "
        "(and the skills they point to). They hold how this project really builds, deploys and is hosted, and "
        "they override your assumptions.",
        "",
        _section("Rules (masterplan #79)", RULES),
        _section("Goals (goals.md, written by Uri; propose edits in your log)",
                 goals or "No goals.md yet. Propose 1-3 dated outcome goals in your log and ask the supervisor to "
                          "get them agreed with Uri. Until then: main CI green, at most 5 open PRs, none idle 7d+."),
        _section("Scoreboard (scoreboard.json, computed from GitHub)",
                 json.dumps(board, indent=1, ensure_ascii=False) if board else "No scoreboard yet."),
        _section("Your last status (status.json)",
                 json.dumps(status, indent=1, ensure_ascii=False) if status else "No status yet."),
        _section("Last %d log entries (log.md)" % LOG_TAIL, "\n".join(tail) or "Log is empty."),
        _section("Toolbelt", "\n".join([
            "- gh: GitHub (PRs, issues, CI runs) for this project's repo.",
            "- orca CLI (/usr/local/bin/orca): worktrees and terminals; spawn or hand off agents.",
            "- Outsourcerer free models for grunt work, one git worktree per job: "
            "`OSRC_REQUIRE_INTERACTIVE=0 %s --provider opencode edit -m opencode-go/iceman-contributor-free \"task\"` "
            "(or opencode-go/beam-free). Free models ship bugs: read the full diff, run the checks and fix it yourself "
            "before it merges." % OSRC,
            "- python3 %s: the Navigator (sessions, decision inbox, `ask` puts a decision to Uri)." % NAV,
            "- Total Recall, before asking anything Uri may already have decided: "
            "%s search --query \"<topic>\" --mode preview --limit 5" % RECALL,
            "- Log anything worth remembering: python3 %s log %s \"<what, PR/issue numbers, job ids>\"" % (OWNER_PY, s),
        ])),
        _section("End of EVERY turn: write your status (required)",
                 "Run this as the last thing you do each turn. A rejected status prints every reason; fix it and "
                 "run it again.\n\n" + cmd + json.dumps(example, indent=1) + "\nEOF\n\n"
                 "state is one of: %s. waiting-on-uri needs needs_uri (decision ids). blocked needs "
                 '"blocked": {"what": "...", "tried": ["way 1", "way 2"], "unblocker": "supervisor | uri | <who>", '
                 '"by": "YYYY-MM-DD"}.' % ", ".join(STATES)),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- CLI

def list_owners():
    out = []
    try:
        names = sorted(n for n in os.listdir(root()) if os.path.isdir(os.path.join(root(), n)))
    except OSError:
        names = []
    for n in names:
        st = read_json(os.path.join(root(), n, "status.json")) or {}
        out.append({"slug": n, "state": st.get("state"), "summary": st.get("summary"),
                    "updated_at": st.get("updated_at"), "session": st.get("session"),
                    "goals": os.path.exists(os.path.join(root(), n, "goals.md"))})
    return out


def _read_obj(raw):
    if raw == "-":
        raw = sys.stdin.read()
    elif raw.startswith("@"):
        with open(os.path.expanduser(raw[1:])) as f:
            raw = f.read()
    return json.loads(raw)


def main(argv):
    p = argparse.ArgumentParser(prog="owner.py", description="Project owners: status, log, brief (docs/owners.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="set or show an owner's status.json")
    sts = st.add_subparsers(dest="sub", required=True)
    ss = sts.add_parser("set", help="validate and write status.json (exit 1 with every reason on reject)")
    ss.add_argument("slug")
    ss.add_argument("--json", required=True, metavar="OBJ", help="status object; '-' reads stdin, @FILE reads a file")
    ss.add_argument("--session", help="owner session key, e.g. claude:<sid> (default $OWNER_SESSION)")
    sh = sts.add_parser("show", help="print status.json")
    sh.add_argument("slug")
    lg = sub.add_parser("log", help="append one line to log.md")
    lg.add_argument("slug")
    lg.add_argument("--actor", default="owner")
    lg.add_argument("text", nargs="+")
    br = sub.add_parser("brief", help="print the owner prompt")
    br.add_argument("slug")
    br.add_argument("--session", help="session key to put in the status command")
    ls = sub.add_parser("ls", help="list owner folders")
    ls.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "status" and a.sub == "set":
        try:
            obj = _read_obj(a.json)
        except (OSError, ValueError) as e:
            print("rejected, nothing written:\n- --json is not valid JSON (%s)" % e, file=sys.stderr)
            return 1
        status, reasons = set_status(a.slug, obj, a.session)
        if reasons:
            print("rejected, nothing written:", file=sys.stderr)
            for r in reasons:
                print("- " + r, file=sys.stderr)
            return 1
        print(json.dumps(status, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "status":
        status = read_json(os.path.join(folder(a.slug), "status.json"))
        if status is None:
            print("no status yet for %s" % slug_of(a.slug), file=sys.stderr)
            return 1
        print(json.dumps(status, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "log":
        try:
            print(append_log(a.slug, a.actor, " ".join(a.text)))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if a.cmd == "brief":
        print(brief(a.slug, a.session))
        return 0
    rows = list_owners()
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
    else:
        for r in rows:
            print("%-20s %-15s %-25s %s" % (r["slug"], r["state"] or "-", r["updated_at"] or "no status",
                                            r["summary"] or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
