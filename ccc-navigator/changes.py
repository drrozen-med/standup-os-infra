"""Change log: who changed what in the navigator, and undo. See issue #14 (R3).

Append-only jsonl (0600), rotated at 5 MB with 3 old files kept. Each change
records only the state fields it touched, as {section: {key: value}}, before
and after (None = absent). Undo restores `before`, but only if every touched
field still holds this change's `after`; otherwise it refuses and names the
later change that touched the field.

Lines are either a change ({"type": "change", "id", ...}) or a marker that a
change was undone ({"type": "undone", "id", "by", "ts"}).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
MAX_BYTES = 5 * 1024 * 1024
KEEP = 3
SEND_TEXT_MAX = 200


class ChangeError(Exception):
    """code is one of: bad_actor, bad_fields, unknown_change, not_undoable, already_undone, conflict."""

    def __init__(self, code, message, later=None):
        super().__init__(message)
        self.code, self.later = code, later


def check_actor(actor):
    actor = str(actor or "").strip()
    if not ACTOR_RE.match(actor):
        raise ChangeError("bad_actor", "actor must match %s" % ACTOR_RE.pattern)
    return actor


def _check_fields(fields):
    if not isinstance(fields, dict) or not all(isinstance(v, dict) for v in fields.values()):
        raise ChangeError("bad_fields", "fields must be {section: {key: value}}")
    return fields


def _touched(change):
    return {(s, k) for part in ("before", "after") for s, kv in (change.get(part) or {}).items() for k in kv}


class ChangeLog:
    def __init__(self, path, max_bytes=MAX_BYTES, keep=KEEP):
        self.path = Path(os.path.expanduser(str(path)))
        self.max_bytes, self.keep = max_bytes, keep
        self.lock = threading.Lock()

    # ---- storage
    def _files(self):
        """Oldest first: <path>.3 ... <path>.1, <path>."""
        old = [self.path.with_name(self.path.name + ".%d" % i) for i in range(self.keep, 0, -1)]
        return [f for f in old + [self.path] if f.exists()]

    def _lines(self):
        for f in self._files():
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
            except OSError:
                continue

    def _rotate(self):
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep, 0, -1):
            src = self.path if i == 1 else self.path.with_name(self.path.name + ".%d" % (i - 1))
            if src.exists():
                os.replace(src, self.path.with_name(self.path.name + ".%d" % i))

    def _append(self, entry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(self.path, 0o600)

    def _all(self):
        """Changes oldest first, each with "undone" set from its marker (if any)."""
        changes, undone = [], {}
        for e in self._lines():
            if e.get("type") == "change":
                changes.append(e)
            elif e.get("type") == "undone":
                undone[e.get("id")] = e
        for c in changes:
            if c["id"] in undone:
                c["undone"] = {"by": undone[c["id"]].get("by"), "ts": undone[c["id"]].get("ts")}
        return changes

    # ---- API
    def record(self, actor, action, params, before, after, undoable=True, now=None):
        """Log one change; returns its id."""
        actor = check_actor(actor)
        params = dict(params or {})
        if isinstance(params.get("text"), str):
            params["text"] = params["text"][:SEND_TEXT_MAX]
        entry = {"type": "change", "id": "c" + secrets.token_hex(6), "ts": now or time.time(), "actor": actor,
                 "action": str(action), "params": params, "before": _check_fields(before or {}),
                 "after": _check_fields(after or {}), "undoable": bool(undoable)}
        with self.lock:
            self._append(entry)
        return entry["id"]

    def recent(self, limit=50, actor=None, key=None):
        """Newest first. key matches any touched state key or a key/keys param."""
        out = []
        for c in reversed(self._all()):
            if actor and c.get("actor") != actor:
                continue
            if key:
                p = c.get("params") or {}
                keys = {k for _, k in _touched(c)} | {p.get("key")} | set(p.get("keys") or [])
                if key not in keys:
                    continue
            out.append(c)
            if len(out) >= limit:
                break
        return out

    def get(self, change_id):
        return next((c for c in self._all() if c["id"] == change_id), None)

    def undo(self, change_id, actor, read, write, now=None):
        """Restore a change's `before`. read(section, key) -> current value (None = absent);
        write({section: {key: value}}) applies it. Returns the id of the new "undo" change."""
        actor = check_actor(actor)
        with self.lock:
            changes = self._all()
            c = next((x for x in changes if x["id"] == change_id), None)
            if not c:
                raise ChangeError("unknown_change", "no change %s" % change_id)
            if not c.get("undoable"):
                raise ChangeError("not_undoable", "%s (%s) cannot be undone" % (change_id, c.get("action")))
            if c.get("undone"):
                raise ChangeError("already_undone", "%s was already undone" % change_id)
            stale = [(s, k) for s, kv in c["after"].items() for k, v in kv.items() if read(s, k) != v]
            if stale:
                later = [x for x in changes if x["ts"] >= c["ts"] and x["id"] != c["id"]
                         and not x.get("undone") and _touched(x) & set(stale)]
                raise ChangeError("conflict", "changed again since %s: %s" % (change_id, ", ".join("%s.%s" % f for f in stale)),
                                  later=later[-1]["id"] if later else None)
            write(c["before"])
            ts = now or time.time()
            undo_entry = {"type": "change", "id": "c" + secrets.token_hex(6), "ts": ts, "actor": actor,
                          "action": "undo", "params": {"change": change_id}, "before": c["after"],
                          "after": c["before"], "undoable": False}
            self._append(undo_entry)
            self._append({"type": "undone", "id": change_id, "by": actor, "ts": ts})
        return undo_entry["id"]
