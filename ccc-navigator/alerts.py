"""Loud alerts (masterplan #79, escalation ladder; contract: docs/supervisor.md, "Loud alerts").

The few things that truly need Uri now. While one is open the Navigator page shows a red banner,
chimes, flashes the tab title and sends one system notification, until he acks it ("I'm on it")
or opens what it links to.

Store: ~/.ccc-navigator/alerts.json (env CCC_NAV_ALERTS), a list of
  {id, title, body, link: {kind: decision|session|url|report, ref} | null, key, raised_by, raised_at, acked_at, acked_by}
`report` links to a morning report (ref: morning-report-YYYY-MM-DD, read from the supervisor folder). An alert
with that key and no link of its own (the night watchdog raises it that way) is shown with the link anyway.
`key` dedupes: a raise with the key of an open alert returns that alert; an acked key may raise again only
12 h after its ack. Only the supervisor cycle raises (bin/supervisor.py); answering a decision acks the
alerts linked to it (navigator.App.answer_decision).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

KINDS = ("decision", "session", "url", "report")
REPORT_RE = re.compile(r"^morning-report-\d{4}-\d{2}-\d{2}$")
REARM = 12 * 3600          # an acked key may raise again this long after its ack
ACKED_SHOWN = 20           # GET /api/alerts: open + this many most recent acked
KEEP_ACKED = 200           # the file keeps this many acked alerts
MAX_TITLE, MAX_BODY, MAX_REF, MAX_KEY = 160, 1000, 500, 200
SESSION_RE = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")
_lock = threading.Lock()


class AlertError(ValueError):
    """code: bad_title, bad_link, bad_key, unknown_alert, bad_request."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def default_path():
    return os.environ.get("CCC_NAV_ALERTS") or "~/.ccc-navigator/alerts.json"


def _clean_link(link):
    if link in (None, "", {}):
        return None
    if not isinstance(link, dict):
        raise AlertError("bad_link", "link must be {kind, ref}")
    kind, ref = str(link.get("kind") or "").strip(), str(link.get("ref") or "").strip()
    if kind not in KINDS:
        raise AlertError("bad_link", "link kind must be one of: " + ", ".join(KINDS))
    if not ref or len(ref) > MAX_REF:
        raise AlertError("bad_link", "link ref must be 1-%d characters" % MAX_REF)
    if kind == "url" and not re.match(r"^https?://", ref, re.I):
        raise AlertError("bad_link", "url must start with http:// or https://")
    if kind == "session" and not SESSION_RE.match(ref):
        raise AlertError("bad_link", "session must be <engine>:<id>")
    if kind == "report" and not REPORT_RE.match(ref):
        raise AlertError("bad_link", "report must be morning-report-YYYY-MM-DD")
    return {"kind": kind, "ref": ref}


def _shown(a):
    """The alert as clients see it: a morning-report alert without a link gets the link to its report."""
    if not a.get("link") and REPORT_RE.match(str(a.get("key") or "")):
        return dict(a, link={"kind": "report", "ref": a["key"]})
    return a


class Alerts:
    def __init__(self, path=None):
        self.path = Path(os.path.expanduser(str(path or default_path())))

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return [a for a in data if isinstance(a, dict) and a.get("id")] if isinstance(data, list) else []

    def _write(self, rows):
        acked = sorted((a for a in rows if a.get("acked_at")), key=lambda a: float(a.get("acked_at") or 0))
        drop = {a["id"] for a in acked[:-KEEP_ACKED]} if len(acked) > KEEP_ACKED else set()
        rows = [a for a in rows if a["id"] not in drop]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(rows, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def list(self, acked_limit=ACKED_SHOWN):
        """{"open": newest raised first, "acked": newest acked first (at most acked_limit)}."""
        rows = self._read()
        opened = sorted((a for a in rows if not a.get("acked_at")), key=lambda a: -float(a.get("raised_at") or 0))
        acked = sorted((a for a in rows if a.get("acked_at")), key=lambda a: -float(a.get("acked_at") or 0))
        return {"open": [_shown(a) for a in opened], "acked": [_shown(a) for a in acked[:acked_limit]]}

    def raise_alert(self, title, body="", link=None, key="", by="cli", now=None):
        """-> {"raised": bool, "alert": the new or the existing alert, "reason"?: why nothing new was raised}."""
        title = " ".join(str(title or "").split())
        if not 1 <= len(title) <= MAX_TITLE:
            raise AlertError("bad_title", "title must be 1-%d characters" % MAX_TITLE)
        body = str(body or "").strip()[:MAX_BODY]
        link = _clean_link(link)
        key = str(key or "").strip() or ("%s:%s" % (link["kind"], link["ref"]) if link else "title:" + title.lower())
        if len(key) > MAX_KEY:
            raise AlertError("bad_key", "key must be at most %d characters" % MAX_KEY)
        now = now or time.time()
        with _lock:
            rows = self._read()
            same = [a for a in rows if a.get("key") == key]
            live = [a for a in same if not a.get("acked_at")]
            if live:
                return {"raised": False, "alert": live[0], "reason": "already open"}
            last = max((float(a["acked_at"]) for a in same if a.get("acked_at")), default=None)
            if last is not None and now - last < REARM:
                return {"raised": False, "alert": next(a for a in same if float(a.get("acked_at") or 0) == last),
                        "reason": "acked %.1f h ago; may raise again %d h after the ack" % ((now - last) / 3600,
                                                                                           REARM // 3600)}
            a = {"id": "al-" + secrets.token_hex(4), "title": title, "body": body, "link": link, "key": key,
                 "raised_by": by, "raised_at": now, "acked_at": None, "acked_by": None}
            rows.append(a)
            self._write(rows)
        return {"raised": True, "alert": a}

    def ack(self, alert_id=None, all_open=False, by="you", now=None, link=None):
        """Ack one alert (id), every open one (all_open), or every open one linked to `link` ({kind, ref}).
        -> the alerts acked by this call (an already acked id gives [])."""
        if not alert_id and not all_open and not link:
            raise AlertError("bad_request", "give an id, all, or a link")
        now = now or time.time()
        with _lock:
            rows = self._read()
            if alert_id and not any(a["id"] == alert_id for a in rows):
                raise AlertError("unknown_alert", "no alert %s" % alert_id)
            hit = []
            for a in rows:
                if a.get("acked_at"):
                    continue
                if all_open or a["id"] == alert_id or (link and (a.get("link") or {}) == link):
                    a["acked_at"], a["acked_by"] = now, by
                    hit.append(a)
            if hit:
                self._write(rows)
        return hit
