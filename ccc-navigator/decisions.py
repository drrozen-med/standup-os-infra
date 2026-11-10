"""Decision inbox: the things only the owner can decide, and his answers.

Two sources of questions, merged by id:
  - Watchtower's queue (decisions.json in the watchtower repo; read-only here)
  - the navigator's own queue (~/.ccc-navigator/decisions.json), filled by `nav ask`
    or POST /api/decisions/ask, which is how an agent puts a question to the owner

Answers live in ~/.ccc-navigator/answers.json ({id: {choice, note, by, ts, ...}}),
so Watchtower's file is never edited; Watchtower skips answered ids. Every
answer, ask and reopen is also written to the change log (changes.py) with its actor.

The gate: every question says what kind it is, and the kind decides who holds it.
  choice    a real choice that is the owner's alone (direction, money, clinical content, real people)
  errand    it needs his own hands (a login, 2FA, an account screen)
  approval  the asker goes ahead with its default unless he objects: when the deadline day ends, and
            never sooner than a day after it was asked, the default is recorded as the answer
  problem   something broken or shared, not his to decide: it waits on the supervisor's desk, and the
            same problem from several projects (same topic) is one item
A question from before kinds existed, or one in Watchtower's file, has no kind until someone sorts it
(`nav sort`; kept in triage.json so no source file is edited). Unsorted questions go to the owner.
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
MAX_UNDO, MAX_TOPIC = 300, 40
KINDS = ("choice", "errand", "approval", "problem")
NOTICE_S = 24 * 3600        # a default never goes ahead sooner than this after it was announced
DESK_MAX_S = 2 * 86400      # a problem the supervisor has not closed by then goes to the owner
WENT_DAYS = 7               # how long a default that went ahead stays listed (with its undo)
_lock = threading.Lock()
LATE_S = 3600      # a default this long overdue did not go ahead: the question is the owner's again

KIND_HELP = """say what kind of question this is with --kind:
  choice    a real choice only Uri can make (product direction, money, clinical content, messages to real people)
  errand    it needs Uri's own hands (his login, 2FA, an account or payment screen)
  approval  you go ahead unless he objects: add --default <one of your options> and --undo "how to roll it back"
  problem   something is broken or shared and not his to decide (a full server, a provider down): add --topic <short-name>
Example: nav.py ask --kind approval --question "Turn the new onboarding on for everyone?" --option "turn on" --option hold --default "turn on" --undo "switch the flag off" """

_W = r"(?<![a-z0-9])(?:%s)(?![a-z0-9])"
# What never goes ahead by itself, whatever the asker says (the owner's standing rules).
GUARDS = tuple((area, re.compile(rx, re.I)) for area, rx in (
    ("money", _W % (r"pric(?:e|es|ed|ing)|budgets?|spend(?:s|ing)?|pay(?:s|ing|ment|ments|out)?|paid|refunds?"
                    r"|charg(?:e|es|ed|ing)|coupons?|invoices?|billing|subscriptions?|purchas(?:e|es|ing)|buy(?:s|ing)?"
                    r"|bids?|costs?|fees?|usd|nis|shekels?|dollars?") + r"|[$₪€£]\s?\d|\d\s?[$₪€£]"),
    ("messages to real people", _W % (
        r"e-?mail(?:s|ed|ing)?|whatsapp|sms|newsletters?|outreach|announc(?:e|es|ed|ing|ement|ements)"
        r"|invit(?:e|es|ed|ing|ation|ations)|notif(?:y|ies|ied|ying|ications?)|tweets?|linkedin|facebook|instagram"
        r"|telegram|slack|dms?|mailing list|mail(?:s|ed|ing)?|broadcasts?|campaigns?|press release"
        r"|contact(?:s|ed|ing)?|respond(?:s|ed|ing)?|reach(?:es|ed|ing)? out|follow(?:s|ed|ing)?[- ]?ups?"
        # "send the reminder to all users": the verb, a few words, then to/out
        r"|(?:send|sends|sending|sent|message|messages|messaging|post|posts|posting|reply|replies|replying)"
        r"(?:\s+[^\s.?!]+){0,5}?\s+(?:to|out)")),
    ("clinical content", _W % (r"clinical(?:ly)?|medical(?:ly)?|patients?|diagnos\w*|dosage|dos(?:e|es|ing)|drugs?"
                               r"|medications?|treatments?|sign[- ]?offs?")),
    ("deleting data", _W % (r"delet(?:e|es|ed|ing|ion)|drop(?:s|ped|ping)?|wip(?:e|es|ed|ing)|purg(?:e|es|ed|ing)"
                            r"|eras(?:e|es|ed|ing)|truncat(?:e|es|ed|ing)|destroy(?:s|ed|ing)?|remov(?:e|es|ed|ing|al)"
                            r"|force[- ]?push(?:es|ed|ing)?|rm -rf")),
    ("accounts and access", _W % (r"passwords?|passcodes?|2fa|mfa|otp|credentials?|secrets?|api[- ]?keys?"
                                  r"|tokens?|oauth|permissions?|dns|ssh|domain transfer|access|accounts?"
                                  r"|grant(?:s|ed|ing)?|logins?|sudo|firewalls?|admins?|privileges?")),
    ("legal terms", _W % r"legal(?:ly)?|contracts?|terms of (?:service|use)|privacy policy|gdpr|hipaa|lawyers?"),
))


class DecisionError(ValueError):
    """code: bad_id, unknown_decision, bad_choice, bad_question, bad_options, bad_deadline, exists, not_answered,
    missing_kind, bad_kind, bad_default, bad_undo, bad_topic, needs_uri, answered, not_due, not_auto,
    defaults_off."""

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


def guard(*texts):
    """(area, word) when the text touches something only the owner decides (money, real people, clinical
    content, deleting, access, legal), else None. Such a question never goes ahead on a default."""
    text = " \n ".join(str(t or "") for t in texts)
    for area, rx in GUARDS:
        m = rx.search(text)
        if m:
            return area, m.group(0).strip()
    return None


def _kind_fields(kind, question, options, default="", undo="", topic="", deadline=None, now=None):
    """Checks a question's kind and what that kind must carry. -> ({kind, default?, undo?, topic?}, deadline)."""
    kind = str(kind or "").strip().lower()
    if not kind:
        raise DecisionError("missing_kind", KIND_HELP)
    if kind not in KINDS:
        raise DecisionError("bad_kind", "kind must be one of: %s\n%s" % (", ".join(KINDS), KIND_HELP))
    default = str(default or "").strip()[:MAX_OPT]
    undo = str(undo or "").strip()[:MAX_UNDO]
    out = {"kind": kind}
    if default and default not in options:
        raise DecisionError("bad_default", "--default must be one of your options: %s" % " / ".join(options))
    if kind == "approval":
        if not default:
            raise DecisionError("bad_default", "an approval names what you do if Uri says nothing: --default "
                                               "must be one of: %s" % " / ".join(options))
        if len(undo) < 5:
            raise DecisionError("bad_undo", 'an approval says how the step is rolled back: --undo "how to undo it"')
        hit = guard(question, *options)
        if hit:
            raise DecisionError("needs_uri", 'this touches %s ("%s"), which only Uri decides: ask it with '
                                             "--kind choice (it waits for his answer, your --default is shown "
                                             "as your suggestion)" % hit)
        if not deadline:
            deadline = (datetime.date.fromtimestamp(now or time.time()) + datetime.timedelta(days=1)).isoformat()
    if default:
        out["default"] = default
    if undo:
        out["undo"] = undo
    if kind == "problem":
        topic = re.sub(r"[^a-z0-9]+", "-", str(topic or "").lower()).strip("-")[:MAX_TOPIC].strip("-")
        if len(topic) < 2:
            raise DecisionError("bad_topic", "a problem names its topic, so the same problem from other projects "
                                             "becomes one item: --topic test-server-full")
        out["topic"] = topic
    return out, deadline


def _start(d):
    """When the question was put the way it stands now: asked, or sorted later (epoch), else None."""
    ts = [float(d[k]) for k in ("asked_at", "sorted_at") if isinstance(d.get(k), (int, float))]
    return max(ts) if ts else None


def auto_at(d):
    """When an approval's default is recorded as the answer (epoch), or None: it never goes ahead by itself."""
    opts = [str(o) for o in d.get("options") or []]
    if d.get("kind") != "approval" or d.get("default") not in opts or not d.get("undo") or guard(d.get("question"), *opts):
        return None
    start = _start(d)
    try:
        day = datetime.date.fromisoformat(str(d.get("deadline")))
    except ValueError:
        return None
    if start is None:
        return None
    end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min).timestamp()
    return max(end, start + NOTICE_S)


def _escalated(group, now, desk_open):
    """Why a problem (all open questions on one topic) is the owner's after all, or "" while the supervisor holds it."""
    if not desk_open:
        return "no supervisor on duty"
    if any(m.get("days_left") is not None and m["days_left"] < 0 for m in group):
        return "past its deadline"
    starts = [s for s in (_start(m) for m in group) if s]
    if starts and now - min(starts) > DESK_MAX_S:
        return "%d days with the supervisor, not solved" % int((now - min(starts)) // 86400)
    return ""


class Inbox:
    def __init__(self, own_path, answers_path, watchtower_path=None, ranks_path=None, triage_path=None):
        self.own_path, self.answers_path, self.watchtower_path = own_path, answers_path, watchtower_path

        def beside(name, given):
            if given is not None:
                return given
            try:
                return Path(answers_path).expanduser().with_name(name)
            except Exception:
                return None
        self.ranks_path = beside("ranks.json", ranks_path)
        self.triage_path = beside("triage.json", triage_path)
        self.switch_path = beside("auto-defaults.json", None)

    def _questions(self):
        out = {}
        for src, path in (("watchtower", self.watchtower_path), ("navigator", self.own_path)):
            if not path:
                continue
            data = _read(path, [])
            for d in data if isinstance(data, list) else []:
                if isinstance(d, dict) and ID_RE.match(str(d.get("id") or "")) and d.get("status", "open") == "open":
                    out[d["id"]] = dict(d, origin=src, kind=d.get("kind") if d.get("kind") in KINDS else "")
        for i, t in self.triage().items():
            if i in out and isinstance(t, dict) and t.get("kind") in KINDS:
                d = out[i]
                for k in ("default", "undo", "topic"):
                    d.pop(k, None)
                d.update({k: t[k] for k in ("kind", "default", "undo", "topic", "deadline") if t.get(k)})
                d["sorted_by"], d["sorted_at"] = str(t.get("by") or ""), t.get("ts")
        return out

    def answers(self):
        a = _read(self.answers_path, {})
        return a if isinstance(a, dict) else {}

    def ranks(self):
        r = _read(self.ranks_path, {}) if self.ranks_path else {}
        return r if isinstance(r, dict) else {}

    def triage(self):
        t = _read(self.triage_path, {}) if self.triage_path else {}
        return t if isinstance(t, dict) else {}

    def auto_on(self):
        """The switch for defaults going ahead by themselves (on unless someone turned it off)."""
        if not self.switch_path or not Path(self.switch_path).expanduser().exists():
            return True
        s = _read(self.switch_path, None)       # a switch file that cannot be read counts as off
        return isinstance(s, dict) and s.get("on") is not False

    def set_auto(self, on, by="you", now=None):
        with _lock:
            before = self.auto_on()
            _write(self.switch_path, {"on": bool(on), "by": by, "ts": now or time.time()})
        return before

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

    def list(self, now=None, answered_limit=15, desk_open=False):
        """{"open", "answered", "went_ahead", "counts", "auto_defaults", "desk_open"}.

        open: every open question, sorted by rank, then priority and deadline. Each says who holds it:
        holder "uri" (for_uri true), "auto" (an approval whose default goes ahead at auto_at), "supervisor"
        (a problem on the supervisor's desk) or "dupe" (same problem as dupe_of). desk_open: a supervisor
        is on duty; without one every problem is the owner's (still one item per topic).
        answered: newest first. went_ahead: defaults that were applied in the last WENT_DAYS days."""
        now = now or time.time()
        qs, ans = self._questions(), self.answers()
        auto_on = self.auto_on()
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

        topics = {}
        for d in open_:
            d["auto_at"] = auto_at(d)
            d["holder"] = "auto" if d["auto_at"] and auto_on else "uri"
            d["dupes"], d["dupe_of"], d["escalated"] = [], None, ""
            if d["holder"] == "auto" and now - d["auto_at"] > LATE_S:      # nothing applied it: do not let it hide
                d["holder"], d["escalated"] = "uri", "its default did not go ahead"
            if d["kind"] == "problem":
                topics.setdefault(d.get("topic") or d["id"], []).append(d)
        for group in topics.values():
            # the card that stands for the topic: the one ranked for the owner, else the first asked
            group.sort(key=lambda d: (d.get("rank") is None, d.get("rank") or 0, _start(d) or 0, d["id"]))
            lead, why = group[0], _escalated(group, now, desk_open)
            lead["escalated"], lead["holder"] = why, "uri" if why else "supervisor"
            lead["dupes"] = [{"id": m["id"], "lane": m.get("lane") or "", "source": m.get("source") or "",
                              "question": m.get("question") or ""} for m in group[1:]]
            for m in group[1:]:
                m["holder"], m["dupe_of"] = "dupe", lead["id"]
        counts = {"uri": 0, "auto": 0, "supervisor": 0, "dupe": 0}
        for d in open_:
            d["for_uri"] = d["holder"] == "uri"
            counts[d["holder"]] += 1

        done.sort(key=lambda d: -float(d["answer"].get("ts") or 0))
        went = []
        for i, a in ans.items():
            if isinstance(a, dict) and a.get("auto") and now - float(a.get("ts") or 0) < WENT_DAYS * 86400:
                q = qs.get(i) or {}
                went.append({"id": i, "question": q.get("question") or a.get("question") or "", "choice": a.get("choice"),
                             "undo": a.get("undo") or "", "ts": a.get("ts"), "lane": q.get("lane") or a.get("lane") or "",
                             "source": q.get("source") or "", "asked_by": q.get("asked_by") or "",
                             "session": q.get("session") or a.get("session") or ""})
        went.sort(key=lambda w: -float(w["ts"] or 0))
        return {"open": open_, "answered": done[:answered_limit], "went_ahead": went[:20], "counts": counts,
                "auto_defaults": auto_on, "desk_open": bool(desk_open)}

    def get(self, decision_id):
        return self._questions().get(decision_id)

    def answer(self, decision_id, choice, note="", by="you", now=None, extra=None, only_open=False):
        """Record an answer. choice must be one of the options; "other" (or an empty choice) needs a note.
        only_open: refuse when it already has an answer (code "answered") instead of replacing it."""
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
        rec.update(extra or {})
        with _lock:
            a = self.answers()
            before = a.get(decision_id)
            if only_open and before is not None:
                raise DecisionError("answered", "%s already has an answer" % decision_id)
            a[decision_id] = rec
            _write(self.answers_path, a)
        return rec, before

    def same_problem(self, decision_id):
        """The other open questions about the same problem (same topic): one answer covers them all."""
        qs = self._questions()
        d = qs.get(decision_id)
        if not d or d.get("kind") != "problem" or not d.get("topic"):
            return []
        ans = self.answers()
        return [q for q in qs.values() if q["id"] != decision_id and q["id"] not in ans
                and q.get("kind") == "problem" and q.get("topic") == d["topic"]]

    def reopen(self, decision_id, by="you", now=None):
        """Take the answer back. -> the answer it had. See reopen_all."""
        return self.reopen_all(decision_id, by, now)[decision_id]

    def reopen_all(self, decision_id, by="you", now=None):
        """Take the answer back, and with it the answers it gave to the same problem from other projects.
        A reopened approval becomes a choice: it waits for an answer, and its default (still shown as the
        asker's suggestion) no longer goes ahead by itself. -> {id: the answer it had}, decision_id first."""
        with _lock:
            a = self.answers()
            if decision_id not in a:
                raise DecisionError("not_answered", "%s has no answer" % decision_id)
            d = self._questions().get(decision_id)
            if d and d.get("kind") == "approval":
                # first, while it still has its answer: if anything stops us between the two files, the
                # question is answered or a choice, never an approval whose default could go ahead again
                tri = self.triage()
                rec = {"kind": "choice", "by": by, "ts": now or time.time(), "was": "approval"}
                if d.get("default"):
                    rec["default"] = d["default"]
                tri[decision_id] = rec
                _write(self.triage_path, tri)
            out = {decision_id: a.pop(decision_id)}
            for i in [i for i, r in a.items() if isinstance(r, dict) and r.get("same_as") == decision_id]:
                out[i] = a.pop(i)
            _write(self.answers_path, a)
        return out

    def ask(self, question, options, lane="", priority=2, deadline=None, session="", source="", decision_id="", by="cli",
            now=None, kind="", default="", undo="", topic=""):
        """Add a question to the navigator's own queue; returns the stored decision. kind is required (the gate)."""
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
        fields, deadline = _kind_fields(kind, question, opts, default, undo, topic, deadline, now)
        if not decision_id:
            slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40] or "q"
            decision_id = "%s-%s" % (slug, hex(int(now * 1000))[-5:])
        decision_id = str(decision_id).lower()
        if not ID_RE.match(decision_id):
            raise DecisionError("bad_id", "id must match %s" % ID_RE.pattern)
        d = {"id": decision_id, "lane": str(lane or "")[:40], "priority": priority, "question": question,
             "options": opts, "deadline": deadline or None, "source": str(source or by)[:120],
             "session": str(session or "")[:120], "status": "open", "asked_by": by, "asked_at": now}
        d.update(fields)
        with _lock:
            if decision_id in self._questions():
                raise DecisionError("exists", "decision %s already exists" % decision_id)
            own = _read(self.own_path, [])
            own = own if isinstance(own, list) else []
            own.append(d)
            _write(self.own_path, own)
        return d

    def sort(self, decision_id, kind, default="", undo="", topic="", deadline=None, by="cli", now=None):
        """Say what kind an open question is (any source; kept in triage.json). Sorting it as an approval
        starts a new day of notice before its default can go ahead. -> (record, previous record or None)."""
        now = now or time.time()
        d = self.get(decision_id)
        if not d or decision_id in self.answers():
            raise DecisionError("unknown_decision", "no open decision %s" % decision_id)
        if deadline:
            try:
                datetime.date.fromisoformat(str(deadline))
            except ValueError:
                raise DecisionError("bad_deadline", "deadline must be YYYY-MM-DD")
        opts = [str(o) for o in d.get("options") or []]
        fields, due = _kind_fields(kind, d.get("question"), opts, default, undo, topic, deadline or d.get("deadline"), now)
        rec = dict(fields, by=by, ts=now)
        with _lock:
            tri = self.triage()
            before = tri.get(decision_id)
            if deadline or (due and not d.get("deadline")):
                rec["deadline"] = due
            elif isinstance(before, dict) and before.get("deadline"):
                rec["deadline"] = before["deadline"]
            tri[decision_id] = rec
            _write(self.triage_path, tri)
        return rec, before

    def due_defaults(self, now=None):
        """Open approvals whose default goes ahead now: the deadline day is over and the notice was given."""
        now = now or time.time()
        ans = self.answers()
        return [d for d in self._questions().values() if d["id"] not in ans and (auto_at(d) or float("inf")) <= now]

    def apply_default(self, decision_id, now=None):
        """Record an approval's default as its answer (by "default", auto true). -> (record, None).
        Everything is read again in one step with the write, so an answer, a reopen or the switch a moment
        ago wins: codes defaults_off, unknown_decision, not_due, answered."""
        now = now or time.time()
        with _lock:
            if not self.auto_on():
                raise DecisionError("defaults_off", "defaults are switched off: nothing goes ahead by itself")
            d = self.get(decision_id)
            if not d:
                raise DecisionError("unknown_decision", "no open decision %s" % decision_id)
            a = self.answers()
            if a.get(decision_id) is not None:
                raise DecisionError("answered", "%s already has an answer" % decision_id)
            at = auto_at(d)
            if at is None or at > now:
                raise DecisionError("not_due", "%s has no default that is due" % decision_id)
            rec = {"choice": d["default"], "by": "default", "ts": now, "question": d.get("question", ""),
                   "lane": d.get("lane", ""), "session": d.get("session", ""), "auto": True, "undo": d["undo"],
                   "note": ("No answer by the deadline (%s), so the default went ahead. To undo: %s"
                            % (d.get("deadline"), d["undo"]))[:MAX_NOTE]}
            a[decision_id] = rec
            _write(self.answers_path, a)
        return rec, None

    def undo_default(self, decision_id, note="", by="you", now=None):
        """The owner objects to a default that went ahead: the answer becomes "undo it". -> (record, the default's record)."""
        note = str(note or "").strip()[:MAX_NOTE]
        with _lock:
            a = self.answers()
            cur = a.get(decision_id)
            if not isinstance(cur, dict) or not cur.get("auto"):
                raise DecisionError("not_auto", "%s did not go ahead on a default" % decision_id)
            text = "Undo this: the default (%s) went ahead and Uri objects. Roll it back" % cur.get("choice")
            text += " (your undo plan: %s)." % cur["undo"] if cur.get("undo") else "."
            rec = dict(cur, choice="other", note=(text + (" " + note if note else ""))[:MAX_NOTE], by=by,
                       ts=now or time.time(), auto=False, undone={"choice": cur.get("choice"), "ts": cur.get("ts")})
            a[decision_id] = rec
            _write(self.answers_path, a)
        return rec, cur


def message_for(decision, rec):
    """The text an agent receives when its question is answered."""
    did = decision.get("id")
    if rec.get("auto"):
        text = ("No answer from Uri by the deadline, so your default applies (%s): %s. Go ahead, and keep your undo "
                "ready: he can still undo it." % (did, rec["choice"]))
    elif rec.get("undone"):
        text = "Uri undid a default that went ahead (%s)." % did
    else:
        by = str(rec.get("by") or "")
        who = "Uri" if by in ("", "you", "cli") else "%s (not Uri)" % by
        text = "%s answered your question (%s): %s" % (who, did, rec["choice"])
    if rec.get("note"):
        text += "\nNote: " + rec["note"]
    return text + "\nQuestion was: " + str(decision.get("question", ""))[:300]
