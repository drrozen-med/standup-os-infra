#!/usr/bin/env python3
"""Sort visible sessions into what needs attention. Local rules only: nothing
leaves the machine.

Labels, in priority order:
  working    the agent is running right now
  needs-you  waiting on an approval, or its last message asks you something
  blocked    its last message reports a failure it could not get past
  done       its last message reports the work finished
  idle       quiet, touched in the last 3 days
  stale      quiet for more than 3 days

A label you set yourself (`nav mark`) is never overwritten by the sorter.
Transcripts are only re-read when a session changed since the last pass.

  python3 triage.py                 # one pass over the last 7 days, writes labels
  python3 triage.py --dry-run       # show what it would write
  python3 triage.py --loop 300      # keep running, one pass every 5 minutes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import nav
import navigator

STALE_SECONDS = 3 * 86400
CACHE = Path(os.path.expanduser(os.environ.get("CCC_NAV_TRIAGE_CACHE", "~/.ccc-navigator/triage-cache.json")))
TAIL = 600  # characters of the last message the rules look at

ASK_RE = navigator.ASK_RE
BLOCK_RE = re.compile(
    r"(\b(i'?m|i am|we'?re|still|currently) blocked\b|\bblocked (on|by)\b|\bi can(no|')t (proceed|continue|go further)\b"
    r"|\bi was unable to\b|\bstill failing\b|\bpermission denied\b|\bnot (authorized|authenticated)\b"
    r"|\bhit (a|the) rate limit\b|\bout of (credits|quota)\b|\bneeds? (your|you to)\b)",
    re.IGNORECASE)
DONE_RE = re.compile(
    r"(?<!not )(?<!never )(?<!n't )(\bmerged\b|\bdeployed\b|\bshipped\b|\ball (\d+ )?tests pass|\bdone\b|\bcompleted\b"
    r"|\bpushed\b|\bfinished\b|\bis live\b)",
    re.IGNORECASE)


def last_assistant_text(messages):
    for role, text in reversed(messages):
        if role == "assistant" and not text.startswith("[tool]"):
            return text
        if role.startswith("user"):
            return ""  # the human spoke last: the agent owes the next move
    return ""


def classify(item, messages, now=None):
    """(label, reason) for one tree item and its flattened messages."""
    now = now or time.time()
    if item.get("state") == "working":
        return "working", "agent is mid-turn"
    if item.get("needs_you"):
        return "needs-you", "waiting on an approval or question in CCC"
    tail = last_assistant_text(messages)[-TAIL:]
    first_line = next((ln.strip() for ln in tail.splitlines()[::-1] if ln.strip()), "")[:160]
    if tail and ASK_RE.search(tail):
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        hit = next((i for i in range(len(lines) - 1, -1, -1) if ASK_RE.search(lines[i])), None)
        asked = lines[hit] if hit is not None else ""
        if asked.rstrip("*").endswith(":") and hit + 1 < len(lines):  # a heading: show what follows it
            asked = asked + " " + lines[hit + 1]
        return "needs-you", (asked or first_line)[:160] or "last message asks you something"
    if item.get("state") == "needs-you":
        return "needs-you", "last message asks you something"
    if tail and BLOCK_RE.search(tail) and not DONE_RE.search(tail[-200:]):
        return "blocked", first_line
    if tail and DONE_RE.search(tail):
        return "done", first_line
    age = now - float(item.get("mtime") or 0)
    if age > STALE_SECONDS:
        return "stale", "quiet for %d days" % (age // 86400)
    return "idle", "quiet for %s" % _age(age)


def _age(seconds):
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    return "%d days" % (seconds // 86400)


def _load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, CACHE)


def run_pass(client, window="7d", dry_run=False, now=None):
    now = now or time.time()
    tree = client._request("/api/tree?window=%s" % window)
    items = [i for i in nav.flatten_tree(tree) if not i.get("hidden")]
    cache = _load_cache()
    entries, out, errors = {}, [], []
    for item in items:
        key = item["key"]
        if item.get("triage_by") == "you":
            out.append({"key": key, "label": item.get("triage"), "by": "you"})
            continue
        prev = cache.get(key) or {}
        unchanged = (prev.get("mtime") == item.get("mtime")
                     and (prev.get("state") == item.get("state") or "state" not in prev))
        if unchanged and prev.get("messages") is not None:
            messages = [tuple(m) for m in prev["messages"]]
        elif item.get("state") == "working" or item.get("needs_you"):
            messages = []
        else:
            try:
                messages = nav.flatten_events(nav.fetch_events(client, item.get("sid") or "", item.get("engine") or ""))[-6:]
            except nav.NavError as exc:
                errors.append({"key": key, "error": str(exc)[:200]})
                continue
        label, reason = classify(item, messages, now)
        cache[key] = {"mtime": item.get("mtime"), "state": item.get("state"),
                      "messages": [[r, t[-TAIL:]] for r, t in messages]}
        entries[key] = {"label": label, "reason": reason, "by": "sorter"}
        out.append({"key": key, "title": item.get("nick") or item.get("original"), "label": label, "reason": reason})
    changed = 0
    if not dry_run and entries:
        changed = client.post("/api/triage", {"entries": entries}).get("changed", 0)
        _save_cache({k: v for k, v in cache.items() if k in entries} or cache)
    counts = {}
    for o in out:
        counts[o["label"]] = counts.get(o["label"], 0) + 1
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "window": window, "sessions": len(items),
            "counts": counts, "changed": changed, "dry_run": dry_run, "errors": errors, "results": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("CCC_NAV_URL", nav.DEFAULT_URL))
    ap.add_argument("--window", default="7d", choices=["now", "1d", "7d", "all"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS")
    args = ap.parse_args(argv)
    client = nav.NavigatorClient(args.url)
    while True:
        try:
            res = run_pass(client, args.window, args.dry_run)
            if args.loop:
                res = {k: v for k, v in res.items() if k != "results"}
            print(json.dumps(res, ensure_ascii=False), flush=True)
        except nav.NavError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr, flush=True)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    sys.exit(main())
