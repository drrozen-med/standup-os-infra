#!/usr/bin/env python3
"""A running, plain-language log of what is happening in every project and session.

A small SQLite file (~/.ccc-navigator/briefs.db) any agent can query instead of
reading terminals:

  sessions  one row per session: title, about (what it is for), now (what is
            happening), state, when it was last summarised
  projects  one row per project: about, now
  events    the timeline: state changes and summaries, per project and session

The worker (``--loop``) walks the navigator tree, logs state changes for free
and asks the small model (llm.py: Groq, then Gemini) for a fresh one-line
summary only when a session really moved on. Without a key nothing leaves the
machine and only state changes are logged.

  python3 briefs.py --once                 # one pass
  python3 briefs.py --loop 120             # keep running
  python3 briefs.py --session <key>        # print one session's brief
  python3 briefs.py --project <name>       # print one project's brief

python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import llm
import nav

DB_PATH = Path(os.environ.get("NAV_BRIEFS_DB", os.path.expanduser("~/.ccc-navigator/briefs.db")))
GROW_EVENTS = 8          # new messages before a session is summarised again
GROW_RATIO = 0.3
SESSION_GAP = 600        # seconds between two summaries of one session
PROJECT_GAP = 1800       # seconds between two summaries of one project
MAX_PER_PASS = 8
KEEP_DAYS = 30
FIRST_LIMIT, RECENT_LIMIT, RECENT_COUNT = 800, 700, 6

SYSTEM = (
    "You write status lines for a busy non-technical founder (a physician) who directs many AI coding agents. "
    "Plain product-manager language. Use only facts in the text; never invent next steps. Never mention code, file names, functions, branches, commits, endpoints "
    "or error codes. Say what it does for the business or the user. Reply with JSON only."
)
SESSION_PROMPT = (
    'From this agent conversation, return JSON {"title": "...", "about": "...", "now": "..."}.\n'
    "title: 2 to 5 words, Title Case, specific (what + object).\n"
    "about: one sentence, at most 18 words: what this session is for.\n"
    "now: one sentence, at most 18 words: what is happening right now, or what it is waiting for.\n\n"
    "Conversation excerpt:\n"
)
PROJECT_PROMPT = (
    'These are the sessions of one project. Return JSON {"about": "...", "now": "..."}.\n'
    "about: one sentence, at most 18 words: what this project is working toward.\n"
    "now: one sentence, at most 25 words: what is moving right now and what needs the founder.\n\n"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  key TEXT PRIMARY KEY, project TEXT, grp TEXT, engine TEXT, title TEXT, about TEXT, now TEXT,
  state TEXT, mtime REAL, events INTEGER DEFAULT 0, provider TEXT, summarised_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY, about TEXT, now TEXT, summarised_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, project TEXT, session_key TEXT, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS ev_proj ON events(project, at);
CREATE INDEX IF NOT EXISTS ev_sess ON events(session_key, at);
"""

_init_lock = threading.Lock()


def connect(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    with _init_lock:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
    if fresh:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    return db


def _row(r):
    return dict(r) if r is not None else None


def log_event(db, project, key, kind, text, at=None):
    db.execute("INSERT INTO events(at, project, session_key, kind, text) VALUES(?,?,?,?,?)",
               (at or time.time(), project, key, kind, (text or "")[:500]))


HOOK_TEXT = {"Stop": "turn ended", "SessionEnd": "session ended", "SubagentStop": "background helper finished"}
_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")


def record_hook(db, payload, now=None):
    """Log one Claude Code hook (end of a turn or of a session) right away. -> session key, or None if ignored."""
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    sid = str(payload.get("session_id") or "")
    if event not in HOOK_TEXT or not _SID_RE.fullmatch(sid):
        return None
    key = "claude:" + sid
    row = db.execute("SELECT project FROM sessions WHERE key=?", (key,)).fetchone()
    project = (row["project"] if row else "") or os.path.basename(str(payload.get("cwd") or "").rstrip("/"))
    reason = str(payload.get("reason") or "")[:40]
    log_event(db, project, key, "hook", HOOK_TEXT[event] + (" (%s)" % reason if reason else ""), now)
    db.commit()
    return key


# ---------------------------------------------------------------- reads (used by the navigator and the CLI)


def session_brief(key, limit=20, db=None):
    db = db or connect()
    row = _row(db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone())
    ev = [dict(r) for r in db.execute(
        "SELECT at, kind, text FROM events WHERE session_key=? ORDER BY at DESC LIMIT ?", (key, limit))]
    return {"key": key, "session": row, "events": ev}


def project_brief(name, limit=30, db=None):
    db = db or connect()
    row = _row(db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone())
    sessions = [dict(r) for r in db.execute(
        "SELECT key, grp, title, about, now, state, updated_at FROM sessions WHERE project=? "
        "ORDER BY updated_at DESC LIMIT 50", (name,))]
    ev = [dict(r) for r in db.execute(
        "SELECT at, session_key, kind, text FROM events WHERE project=? ORDER BY at DESC LIMIT ?", (name, limit))]
    return {"project": name, "brief": row, "sessions": sessions, "events": ev}


def all_projects(db=None):
    db = db or connect()
    return [dict(r) for r in db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]


def events_since(project=None, key=None, since=0, limit=100, db=None):
    db = db or connect()
    q, args = "SELECT at, project, session_key, kind, text FROM events WHERE at>=?", [since]
    if project:
        q += " AND project=?"
        args.append(project)
    if key:
        q += " AND session_key=?"
        args.append(key)
    q += " ORDER BY at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.execute(q, args)]


# ---------------------------------------------------------------- summarising


def build_snippet(messages):
    """First real user message plus the last few messages, secret-stripped."""
    parts = []
    first = next((t for r, t in messages if r == "user" and not t.lstrip().startswith(("<", "/", "Caveat:"))), "")
    if first:
        parts.append("USER (first message):\n" + first[:FIRST_LIMIT])
    for role, text in messages[-RECENT_COUNT:]:
        parts.append(role.upper() + ":\n" + text[:RECENT_LIMIT])
    return llm.strip_secrets("\n\n".join(parts))


def _clean(s, words):
    s = " ".join(str(s or "").split()).strip().strip('"')
    w = s.split()
    return " ".join(w[:words]) if w else ""


def summarise_session(messages):
    """{"title","about","now","provider"} from the model, or None."""
    if not messages:
        return None
    res = llm.complete(SESSION_PROMPT + build_snippet(messages), system=SYSTEM, max_tokens=200, as_json=True)
    if not res:
        return None
    data = llm.parse_json(res[1])
    out = {"title": _clean(data.get("title"), 6).rstrip(".!?"), "about": _clean(data.get("about"), 26),
           "now": _clean(data.get("now"), 26), "provider": res[0]}
    return out if (out["about"] or out["now"]) else None


def refresh_session(client, item, db=None, now=None):
    """Summarise one tree item now and store it. Returns the stored brief dict or None."""
    db = db or connect()
    now = now or time.time()
    messages = nav.flatten_events(nav.fetch_events(client, item.get("sid") or "", item.get("engine") or ""))
    brief = summarise_session(messages)
    key, project = item["key"], item.get("repo_name") or item.get("repo") or ""
    if not brief:
        return None
    db.execute(
        "INSERT INTO sessions(key, project, grp, engine, title, about, now, state, mtime, events, provider, "
        "summarised_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "project=excluded.project, grp=excluded.grp, title=excluded.title, about=excluded.about, now=excluded.now, "
        "state=excluded.state, mtime=excluded.mtime, events=excluded.events, provider=excluded.provider, "
        "summarised_at=excluded.summarised_at, updated_at=excluded.updated_at",
        (key, project, item.get("group") or "", item.get("engine") or "", brief["title"], brief["about"],
         brief["now"], item.get("state") or "", item.get("mtime") or 0, len(messages), brief["provider"], now, now))
    log_event(db, project, key, "summary", brief["now"], now)
    db.commit()
    return dict(brief, events=len(messages))


def _due(row, item, now):
    if row is None or not row["summarised_at"]:
        return True
    if (item.get("mtime") or 0) <= (row["mtime"] or 0):
        return False
    return now - row["summarised_at"] >= SESSION_GAP


def _state_label(item):
    if item.get("live") or item.get("state") == "working":
        return "working"
    if item.get("needs_you"):
        return "needs you"
    return item.get("triage") or item.get("state") or "idle"


def summarise_project(db, name, now):
    rows = db.execute("SELECT title, about, now, state FROM sessions WHERE project=? AND updated_at>? "
                      "ORDER BY updated_at DESC LIMIT 15", (name, now - 3 * 86400)).fetchall()
    lines = ["- %s (%s): %s %s" % (r["title"] or "session", r["state"] or "", r["about"] or "", r["now"] or "")
             for r in rows if r["about"] or r["now"]]
    if not lines:
        return False
    res = llm.complete(PROJECT_PROMPT + "Project: %s\n%s" % (name, "\n".join(lines)), system=SYSTEM,
                       max_tokens=200, as_json=True)
    data = llm.parse_json(res[1]) if res else {}
    if not (data.get("about") or data.get("now")):
        return False
    about, cur = _clean(data.get("about"), 26), _clean(data.get("now"), 34)
    db.execute("INSERT INTO projects(name, about, now, summarised_at, updated_at) VALUES(?,?,?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET about=excluded.about, now=excluded.now, "
               "summarised_at=excluded.summarised_at, updated_at=excluded.updated_at", (name, about, cur, now, now))
    log_event(db, name, None, "project", cur, now)
    db.commit()
    return True


def run_pass(client, window="7d", db=None, now=None, max_per_pass=MAX_PER_PASS):
    db = db or connect()
    now = now or time.time()
    items = [i for i in nav.flatten_tree(client._request("/api/tree?window=%s" % window)) if not i.get("hidden")]
    use_llm = llm.enabled()
    logged, summarised, touched, errors = 0, 0, set(), []
    # most recent first: those are the ones the owner looks at
    for item in sorted(items, key=lambda i: -(i.get("mtime") or 0)):
        key, project = item["key"], item.get("repo_name") or item.get("repo") or ""
        row = db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
        label = _state_label(item)
        if row is not None and row["state"] != label:
            log_event(db, project, key, "status", label + (": " + item["triage_reason"] if item.get("triage_reason") else ""), now)
            db.execute("UPDATE sessions SET state=?, updated_at=? WHERE key=?", (label, now, key))
            logged += 1
            touched.add(project)
        if row is None and not use_llm:
            db.execute("INSERT OR IGNORE INTO sessions(key, project, grp, engine, title, state, mtime, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (key, project, item.get("group") or "", item.get("engine") or "",
                                                  item.get("nick") or item.get("original") or "", label,
                                                  item.get("mtime") or 0, now))
        if use_llm and summarised < max_per_pass and _due(row, item, now):
            try:
                if refresh_session(client, dict(item, state=label), db, now):
                    summarised += 1
                    touched.add(project)
            except nav.NavError as exc:
                errors.append({"key": key, "error": str(exc)[:160]})
    db.commit()
    projects = 0
    if use_llm:
        for name in touched:
            prow = db.execute("SELECT summarised_at FROM projects WHERE name=?", (name,)).fetchone()
            if prow is None or now - (prow["summarised_at"] or 0) >= PROJECT_GAP:
                projects += bool(summarise_project(db, name, now))
    db.execute("DELETE FROM events WHERE at<?", (now - KEEP_DAYS * 86400,))
    db.commit()
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sessions": len(items), "llm": use_llm,
            "status_events": logged, "summarised": summarised, "projects": projects, "errors": errors[:5],
            "usage": llm.usage().get("calls")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("CCC_NAV_URL", nav.DEFAULT_URL))
    ap.add_argument("--window", default="7d", choices=["now", "1d", "7d", "all"])
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--session")
    ap.add_argument("--project")
    args = ap.parse_args(argv)
    if args.session:
        print(json.dumps(session_brief(args.session), ensure_ascii=False, indent=2))
        return 0
    if args.project:
        print(json.dumps(project_brief(args.project), ensure_ascii=False, indent=2))
        return 0
    client = nav.NavigatorClient(args.url)
    while True:
        try:
            print(json.dumps(run_pass(client, args.window), ensure_ascii=False), flush=True)
        except (nav.NavError, sqlite3.Error) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr, flush=True)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    sys.exit(main())
