"""Decision inbox: the things only the owner can decide, and his answers.

Two sources of questions, merged by id:
  - Watchtower's queue (decisions.json in the watchtower repo; read-only here)
  - the navigator's own queue (~/.ccc-navigator/decisions.json), filled by `nav ask`
    or POST /api/decisions/ask, which is how an agent puts a question to the owner

Answers live in ~/.ccc-navigator/answers.json ({id: {choice, note, by, ts, ...}}),
so Watchtower's file is never edited; Watchtower skips answered ids. Every
answer, ask and reopen is also written to the change log (changes.py) with its actor.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
MAX_Q, MAX_OPT, MAX_OPTS, MAX_NOTE = 600, 80, 8, 1000
_lock = threading.Lock()


class DecisionError(ValueError):
    """code: bad_id, unknown_decision, bad_choice, bad_question, bad_options, bad_deadline, exists, not_answered."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _read(path, default):
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return default


def _write(path, data):
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def _days_left(deadline, now):
    try:
        d = datetime.date.fromisoformat(str(deadline))
    except ValueError:
        return None
    return (d - datetime.date.fromtimestamp(now)).days


class Inbox:
    def __init__(self, own_path, answers_path, watchtower_path=None, ranks_path=None):
        self.own_path, self.answers_path, self.watchtower_path = own_path, answers_path, watchtower_path
        if ranks_path is not None:
            self.ranks_path = ranks_path
        else:
            try:
                self.ranks_path = Path(answers_path).expanduser().with_name("ranks.json")
            except Exception:
                self.ranks_path = ranks_path

    def _questions(self):
        out = {}
        for src, path in (("watchtower", self.watchtower_path), ("navigator", self.own_path)):
            if not path:
                continue
            data = _read(path, [])
            for d in data if isinstance(data, list) else []:
                if isinstance(d, dict) and ID_RE.match(str(d.get("id") or "")) and d.get("status", "open") == "open":
                    out[d["id"]] = dict(d, origin=src)
        return out

    def answers(self):
        a = _read(self.answers_path, {})
        return a if isinstance(a, dict) else {}

    def ranks(self):
        r = _read(self.ranks_path, {}) if self.ranks_path else {}
        return r if isinstance(r, dict) else {}

    def set_ranks(self, ids, why=None, by="you", now=None):
        ids = [str(x) for x in (ids or [])]
        why = why or {}
        if not isinstance(why, dict):
            why = {}
        qs = self._questions()
        ans = self.answers()
        cleaned = []
        for i in ids:
            if i in qs and i not in ans and i not in cleaned:
                cleaned.append(i)
            if len(cleaned) >= 7:
                break
        clean_why = {}
        for k, v in why.items():
            k = str(k)
            if k in cleaned:
                clean_why[k] = str(v or "")[:140]
        rec = {"ids": cleaned, "why": clean_why, "by": by, "ts": now or time.time()}
        with _lock:
            before = self.ranks()
            _write(self.ranks_path, rec)
        return rec, before

    def clear_ranks(self, by="you", now=None):
        with _lock:
            before = self.ranks()
            rec = {"ids": [], "why": {}, "by": by, "ts": now or time.time()}
            _write(self.ranks_path, rec)
        return rec, before

    def list(self, now=None, answered_limit=15):
        """{"open": [...], "answered": [...]}: open sorted by priority, then deadline; answered newest first."""
        now = now or time.time()
        qs, ans = self._questions(), self.answers()
        ranks = self.ranks() or {}
        rank_ids = ranks.get("ids") or []
        rank_why = ranks.get("why") or {}
        if not isinstance(rank_ids, list):
            rank_ids = []
        if not isinstance(rank_why, dict):
            rank_why = {}
        open_, done = [], []
        for d in qs.values():
            d = dict(d)
            d["days_left"] = _days_left(d.get("deadline"), now) if d.get("deadline") else None
            if d["id"] in ans:
                done.append(dict(d, answer=ans[d["id"]]))
            else:
                if d["id"] in rank_ids and d["id"] not in ans:
                    try:
                        d["rank"] = rank_ids.index(d["id"]) + 1
                    except ValueError:
                        d["rank"] = None
                    d["why"] = str(rank_why.get(d["id"]) or "")
                else:
                    d["rank"] = None
                    d["why"] = ""
                open_.append(d)
        ranked = [d for d in open_ if d.get("rank") is not None]
        rest = [d for d in open_ if d.get("rank") is None]
        ranked.sort(key=lambda d: int(d.get("rank") or 999))
        rest.sort(key=lambda d: (int(d.get("priority") or 3), d["days_left"] if d["days_left"] is not None else 9999, d["id"]))
        open_ = ranked + rest
        done.sort(key=lambda d: -float(d["answer"].get("ts") or 0))
        return {"open": open_, "answered": done[:answered_limit]}

    def get(self, decision_id):
        return self._questions().get(decision_id)

    def answer(self, decision_id, choice, note="", by="you", now=None):
        """Record the owner's answer. choice must be one of the options; "other" (or an empty choice) needs a note."""
        d = self.get(decision_id)
        if not d:
            raise DecisionError("unknown_decision", "no open decision %s" % decision_id)
        choice, note = str(choice or "").strip(), str(note or "").strip()[:MAX_NOTE]
        opts = [str(o) for o in d.get("options") or []]
        if choice and choice not in opts and choice.lower() != "other":
            raise DecisionError("bad_choice", "choice must be one of: %s" % " / ".join(opts))
        if (not choice or choice.lower() == "other") and not note:
            raise DecisionError("bad_choice", "write the answer in the note when choosing other")
        rec = {"choice": choice or "other", "note": note, "by": by, "ts": now or time.time(),
               "question": d.get("question", ""), "lane": d.get("lane", ""), "session": d.get("session", "")}
        with _lock:
            a = self.answers()
            before = a.get(decision_id)
            a[decision_id] = rec
            _write(self.answers_path, a)
        return rec, before

    def reopen(self, decision_id):
        with _lock:
            a = self.answers()
            if decision_id not in a:
                raise DecisionError("not_answered", "%s has no answer" % decision_id)
            before = a.pop(decision_id)
            _write(self.answers_path, a)
        return before

    def ask(self, question, options, lane="", priority=2, deadline=None, session="", source="", decision_id="", by="cli",
            now=None):
        """Add a question to the navigator's own queue; returns the stored decision."""
        question = str(question or "").strip()
        if not 5 <= len(question) <= MAX_Q:
            raise DecisionError("bad_question", "question must be 5-%d characters" % MAX_Q)
        opts = [str(o).strip()[:MAX_OPT] for o in (options or []) if str(o).strip()]
        if not 1 <= len(opts) <= MAX_OPTS or len(set(opts)) != len(opts):
            raise DecisionError("bad_options", "give 1-%d distinct options" % MAX_OPTS)
        if deadline:
            try:
                datetime.date.fromisoformat(str(deadline))
            except ValueError:
                raise DecisionError("bad_deadline", "deadline must be YYYY-MM-DD")
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 2
        priority = min(3, max(1, priority))
        now = now or time.time()
        if not decision_id:
            slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40] or "q"
            decision_id = "%s-%s" % (slug, hex(int(now * 1000))[-5:])
        decision_id = str(decision_id).lower()
        if not ID_RE.match(decision_id):
            raise DecisionError("bad_id", "id must match %s" % ID_RE.pattern)
        d = {"id": decision_id, "lane": str(lane or "")[:40], "priority": priority, "question": question,
             "options": opts, "deadline": deadline or None, "source": str(source or by)[:120],
             "session": str(session or "")[:120], "status": "open", "asked_by": by, "asked_at": now}
        with _lock:
            if decision_id in self._questions():
                raise DecisionError("exists", "decision %s already exists" % decision_id)
            own = _read(self.own_path, [])
            own = own if isinstance(own, list) else []
            own.append(d)
            _write(self.own_path, own)
        return d


def message_for(decision, rec):
    """The text an agent receives when its question is answered."""
    text = "Uri answered your question (%s): %s" % (decision.get("id"), rec["choice"])
    if rec.get("note"):
        text += "\nNote: " + rec["note"]
    return text + "\nQuestion was: " + str(decision.get("question", ""))[:300]
