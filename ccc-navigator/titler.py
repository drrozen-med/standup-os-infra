#!/usr/bin/env python3
"""Give every visible navigator session a short, sensible name.

A standalone background worker (python3 stdlib only). It talks to the running
CCC Navigator over its local JSON API, asks a cheap model for a two-to-five
word title per session, and writes that title back through the navigator's
rename endpoint. Nothing happens in the UI: the worker runs server-side and
the sidebar simply shows better names.

Privacy: by default NOTHING leaves this machine. Titles come from a local,
rule-based namer. The external model (see DEFAULT_CMD) is used only when
TITLER_ALLOW_EXTERNAL=1 is set, or when TITLER_CMD names a command the owner
chose. Sessions may contain medical, business and personal content.

Safety rules that shape the whole program:

* only sessions the navigator already shows (the tree endpoint hides hidden
  items unless asked otherwise) are ever touched;
* a title the user typed by hand is NEVER overwritten -- the worker only
  re-titles sessions it named itself, and only when the transcript grew by
  40% or more and ``--retitle`` was given;
* transcripts are flattened, truncated and stripped of obvious secrets before
  they are handed to the model;
* at most ``--max`` sessions are renamed per cycle.

Every cycle prints one JSON line. The exit code is 0 even when individual
sessions fail; only bad arguments are an error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8095"
DEFAULT_STATE = "~/.ccc-navigator/titler.json"
DEFAULT_MAX = 20
DEFAULT_LOOP_SECONDS = 600
BACKOFF_SECONDS = 300
MODEL_TIMEOUT = 30
TREE_WINDOW = "7d"
FIRST_USER_LIMIT = 700
RECENT_LIMIT = 450
RECENT_COUNT = 3
MIN_MESSAGES = 2
TITLE_MAX_CHARS = 40
TITLE_MAX_WORDS = 7
# markdown decoration and quotes that a model may wrap a title in
MARKUP_CHARS = "#*_`~>-\t\"'`“”‘’«»"
GROWTH_FACTOR = 1.4
# A cycle stops after --max renames; this bounds how many candidates may be
# examined looking for them (a session that cannot be titled costs nothing).
SCAN_FACTOR = 2

# External lane. NOT used unless TITLER_ALLOW_EXTERNAL=1 (see external_allowed).
# The "paid, non-training" claim about this lane has not been verified.
DEFAULT_CMD = ["droid", "exec", "-m", "custom:MiniMax-M3-direct", "--auto", "low"]

PROMPT = (
    "Name this conversation for a busy doctor-founder scanning a sidebar. "
    "Reply with ONLY the title: 2 to 5 words, Title Case, specific (what + object), "
    "concrete not generic, no quotes, no trailing punctuation, never use words like "
    "Session, Chat, Help, Conversation. Conversation excerpt follows."
)

REDACTION = "[REDACTED]"
SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), REDACTION),
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTION),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), REDACTION),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer " + REDACTION),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), REDACTION),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"), REDACTION),
    # personal data: emails, URLs, phone numbers and long digit runs (IDs)
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), REDACTION),
    (re.compile(r"https?://\S+"), REDACTION),
    (re.compile(r"\+?\d[\d\s().\-]{7,}\d"), REDACTION),
)


class NavigatorError(Exception):
    """A request to the navigator failed."""


class TitlerError(Exception):
    """The model call could not produce a title."""


# ---------------------------------------------------------------- helpers


def now_iso():
    """UTC timestamp used in the state file."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_secrets(text):
    """Remove obvious secrets (API keys, bearer tokens, long hex/base64)."""
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def flatten_items(tree):
    """Flatten the navigator tree into a flat list of session items."""
    items = []
    if not isinstance(tree, dict):
        return items
    for repo in tree.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        for group in repo.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for raw in group.get("items") or []:
                if isinstance(raw, dict):
                    items.append(raw)
    for raw in tree.get("items") or []:
        if isinstance(raw, dict):
            items.append(raw)
    return items


def extract_events(payload):
    """Pull the event list out of whatever shape the transcript came in."""
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        for key in ("events", "messages", "history"):
            value = payload.get(key)
            if isinstance(value, list):
                return [event for event in value if isinstance(event, dict)]
        for key in ("conversation", "data", "result"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = extract_events(value)
                if nested:
                    return nested
    return []


def _text_of(value):
    """Plain text of a content field that may be a string, a list or a dict."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if isinstance(value.get(key), str):
                return value[key]
        return ""
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                piece = _text_of(part)
                if piece:
                    parts.append(piece)
        return "\n".join(parts)
    return str(value)


def flatten_events(events):
    """Flatten navigator/CCC/pi events into (role, text) conversation messages.

    Thinking blocks and tool calls are noise for a title, so only user text
    and assistant text count as messages.
    """
    messages = []
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "").strip().lower()
        if etype in ("user_text", "user", "user_message", "human"):
            text = _text_of(event.get("text") if event.get("text") is not None else event.get("content"))
            if text.strip():
                messages.append(("user", text.strip()))
            continue
        if etype == "assistant":
            pieces = []
            blocks = event.get("blocks")
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, str):
                        if block.strip():
                            pieces.append(block.strip())
                        continue
                    if not isinstance(block, dict):
                        continue
                    kind = str(block.get("kind") or block.get("type") or "").strip().lower()
                    if kind in ("thinking", "tool_use", "tool", "tool_call"):
                        continue
                    text = _text_of(block.get("text") if block.get("text") is not None else block.get("content"))
                    if text.strip():
                        pieces.append(text.strip())
            if not pieces:
                text = _text_of(event.get("text") if event.get("text") is not None else event.get("content"))
                if text.strip():
                    pieces.append(text.strip())
            if pieces:
                messages.append(("assistant", "\n".join(pieces)))
            continue
    return messages


def build_snippet(messages, first_limit=FIRST_USER_LIMIT, recent_limit=RECENT_LIMIT, recent_count=RECENT_COUNT):
    """First user message plus the last few messages, as one plain-text block."""
    parts = []
    first_user = next((text for role, text in messages if role == "user"), "")
    if first_user:
        parts.append("USER (first message):\n" + first_user[:first_limit])
    for role, text in messages[-recent_count:]:
        parts.append(role.upper() + ":\n" + text[:recent_limit])
    snippet = "\n\n".join(parts)
    return strip_secrets(snippet)


def sanitize_title(reply):
    """Clean a model reply into a title, or return "" when it is unusable."""
    if not reply:
        return ""
    # reasoning models wrap their thinking in <think>...</think>; keep only what follows
    reply = str(reply)
    if "</think>" in reply:
        reply = reply.rsplit("</think>", 1)[1]
    reply = re.sub(r"(?is)<think>.*$", "", reply)
    line = ""
    for raw in str(reply).splitlines():
        candidate = raw.strip()
        if candidate:
            line = candidate
            break
    if not line:
        return ""
    if line.lower().startswith("error"):
        return ""
    # markdown decoration, surrounding quotes and a "Title:" label
    line = line.strip(MARKUP_CHARS)
    line = re.sub(r"(?i)^title\s*[:\-]\s*", "", line).strip()
    line = line.strip(MARKUP_CHARS)
    # trailing punctuation
    line = line.rstrip(" \t.,;:!?…-")
    # collapse whitespace
    line = re.sub(r"\s+", " ", line).strip()
    if not line:
        return ""
    if len(line.split()) > TITLE_MAX_WORDS:
        return ""
    if len(line) > TITLE_MAX_CHARS:
        cut = line[:TITLE_MAX_CHARS]
        line = (cut.rsplit(" ", 1)[0] if " " in cut and line[TITLE_MAX_CHARS] != " " else cut).rstrip(" \t.,;:!?…-")
    return line.strip()


def external_allowed(override=None):
    """True only when the owner explicitly allowed a model outside this machine."""
    if override or os.environ.get("TITLER_CMD"):
        return True
    return os.environ.get("TITLER_ALLOW_EXTERNAL") == "1"


LOCAL_STOPWORDS = frozenset(
    "a an the and or but to of in on for with from at by is are was were be i me my we our you your it this that "
    "please can could would should let lets need want help make do does did how what why when where which".split()
)


def local_title(messages):
    """Rule-based title from the first user message. Sends nothing anywhere."""
    first = next((text for role, text in messages if role == "user"), "")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", strip_secrets(first)) if w.lower() not in LOCAL_STOPWORDS]
    if len(words) < 2:
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in words[:5])


def model_command(prompt, override=None):
    """Build the argv used to ask the model; the prompt is the last argument."""
    raw = override if override is not None else os.environ.get("TITLER_CMD")
    parts = shlex.split(raw) if raw else list(DEFAULT_CMD)
    if not parts:
        raise TitlerError("no titler command configured")
    if any("{prompt}" in part for part in parts):
        return [part.replace("{prompt}", prompt) for part in parts]
    return parts + [prompt]


def call_model(prompt, override=None, timeout=MODEL_TIMEOUT):
    """Ask the model for a title and return its raw reply."""
    command = model_command(prompt, override)
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TitlerError("model call timed out after %ss" % timeout) from None
    except FileNotFoundError:
        raise TitlerError("model command not found: %s" % command[0]) from None
    except OSError as exc:
        raise TitlerError("model command failed: %s" % exc) from None
    reply = (proc.stdout or "").strip()
    if not reply:
        detail = (proc.stderr or "").strip().splitlines()
        raise TitlerError("model returned nothing: %s" % (detail[-1][:200] if detail else "exit %d" % proc.returncode))
    return reply


# ---------------------------------------------------------------- state


def state_path():
    """Where the worker remembers the titles it set."""
    return Path(os.environ.get("TITLER_STATE", DEFAULT_STATE)).expanduser()


def load_state(path):
    """Read the worker's memory of the titles it set ({} when unusable)."""
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    """Write the state file atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp-%d" % os.getpid())
    try:
        with open(str(tmp), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(target))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- navigator


class Navigator:
    """Small stdlib HTTP client for the navigator's local JSON API."""

    def __init__(self, base_url=DEFAULT_URL, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path, method="GET", payload=None, timeout=None):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise NavigatorError("navigator returned HTTP %s" % exc.code) from exc
        except Exception as exc:
            raise NavigatorError("cannot reach navigator at %s: %s" % (self.base_url, exc)) from exc
        try:
            return json.loads(raw or b"{}")
        except ValueError as exc:
            raise NavigatorError("navigator returned invalid JSON: %s" % exc) from exc

    def tree(self, window=TREE_WINDOW):
        """The visible tree; hidden items are excluded unless we ask for them."""
        query = urllib.parse.urlencode({"window": window})
        return self._request("/api/tree?" + query)

    def transcript(self, engine, sid):
        """The transcript of one session, in the navigator's event shape."""
        if engine == "pi":
            query = urllib.parse.urlencode({"sid": sid})
            return self._request("/api/pi/transcript?" + query, timeout=self.timeout)
        return self._request("/api/conversations/" + urllib.parse.quote(sid, safe=""), timeout=self.timeout)

    def rename(self, key, sid, engine, name):
        """Write a new title for a session."""
        payload = {"kind": "session", "key": key, "sid": sid, "engine": engine, "name": name}
        return self._request("/api/rename", method="POST", payload=payload, timeout=self.timeout)


# ---------------------------------------------------------------- selection


def select_candidates(items, state, retitle=False):
    """Split visible sessions into (candidates, skipped).

    A session is a candidate when it has no nickname at all, or when the
    nickname is exactly the title this worker set earlier. A nickname the user
    typed is never touched, and a session the worker already titled is only a
    candidate when --retitle is given (the growth check happens later, once
    the transcript has been fetched).
    """
    candidates = []
    skipped = []

    def skip(key, reason):
        skipped.append({"key": key, "reason": reason})

    for item in items:
        key = str(item.get("key") or "")
        if not key:
            continue
        if item.get("hidden"):
            skip(key, "hidden")
            continue
        if int(item.get("batch") or 0) > 0:
            skip(key, "batch")
            continue
        sid = str(item.get("sid") or "")
        if not sid:
            skip(key, "no-session-id")
            continue
        nick = str(item.get("nick") or "").strip()
        prior = state.get(key) if isinstance(state.get(key), dict) else None
        if prior:
            prior_title = str(prior.get("title") or "")
            if nick and nick != prior_title:
                skip(key, "user-renamed")
                continue
            if not retitle:
                skip(key, "already-titled")
                continue
            candidates.append({"key": key, "item": item, "prior": prior})
            continue
        if nick:
            skip(key, "user-titled")
            continue
        candidates.append({"key": key, "item": item, "prior": None})

    candidates.sort(key=lambda entry: float(entry["item"].get("mtime") or 0), reverse=True)
    skipped.sort(key=lambda entry: entry["key"])
    return candidates, skipped


def grew_enough(prior, events_count):
    """Whether a transcript grew by 40% or more since the worker titled it."""
    try:
        previous = int((prior or {}).get("events") or 0)
    except (TypeError, ValueError):
        previous = 0
    if previous <= 0:
        return True  # nothing to compare against
    return events_count >= previous * GROWTH_FACTOR


# ---------------------------------------------------------------- cycle


def title_candidate(client, candidate, opts, state):
    """Title one session. Returns a result record."""
    key = candidate["key"]
    item = candidate["item"]
    sid = str(item.get("sid") or "")
    engine = str(item.get("engine") or "")

    try:
        payload = client.transcript(engine, sid)
    except NavigatorError as exc:
        return {"key": key, "status": "error", "error": "transcript: %s" % exc}
    events = extract_events(payload)
    count = len(events)

    if candidate.get("prior") is not None and not grew_enough(candidate["prior"], count):
        return {"key": key, "status": "skipped", "reason": "not-grown", "events": count}

    messages = flatten_events(events)
    if len(messages) < MIN_MESSAGES:
        return {"key": key, "status": "skipped", "reason": "too-few-messages", "events": count}

    snippet = build_snippet(messages)
    if not snippet.strip():
        return {"key": key, "status": "skipped", "reason": "empty-snippet", "events": count}

    if external_allowed(opts.cmd):
        prompt = PROMPT + "\n\n" + snippet
        try:
            reply = call_model(prompt, opts.cmd)
        except TitlerError as exc:
            return {"key": key, "status": "error", "error": str(exc)}
    else:
        reply = local_title(messages)

    title = sanitize_title(reply)
    if not title:
        return {"key": key, "status": "error", "error": "unusable reply", "reply": reply[:200]}

    record = {"key": key, "sid": sid, "engine": engine, "title": title, "events": count}
    if opts.dry_run:
        record["status"] = "proposed"
        return record

    try:
        client.rename(key, sid, engine, title)
    except NavigatorError as exc:
        return {"key": key, "status": "error", "error": "rename: %s" % exc, "title": title}
    state[key] = {"title": title, "at": now_iso(), "events": count}
    record["status"] = "renamed"
    return record


def run_cycle(opts):
    """One pass: pick candidates, title them, report what happened."""
    summary = {
        "at": now_iso(),
        "url": opts.url,
        "dry_run": bool(opts.dry_run),
        "visible": 0,
        "candidates": 0,
        "renamed": 0,
        "proposed": 0,
        "proposals": [],
        "skipped": [],
        "results": [],
        "errors": [],
        "unreachable": False,
    }

    client = Navigator(opts.url)
    try:
        tree = client.tree()
    except NavigatorError as exc:
        summary["unreachable"] = True
        summary["errors"].append({"stage": "tree", "error": str(exc)})
        return summary

    items = flatten_items(tree)
    summary["visible"] = len(items)

    state = load_state(opts.state_path)
    candidates, skipped = select_candidates(items, state, opts.retitle)
    summary["skipped"] = skipped
    summary["candidates"] = len(candidates)

    renamed = 0
    examined = 0
    scan_cap = max(1, opts.max) * SCAN_FACTOR
    for candidate in candidates:
        if renamed >= opts.max or examined >= scan_cap:
            break
        examined += 1
        record = title_candidate(client, candidate, opts, state)
        summary["results"].append(record)
        if record["status"] == "renamed":
            renamed += 1
            summary["proposals"].append(
                {"key": record["key"], "title": record["title"], "engine": record.get("engine", "")}
            )
        elif record["status"] == "proposed":
            summary["proposed"] += 1
            summary["proposals"].append(
                {"key": record["key"], "title": record["title"], "engine": record.get("engine", "")}
            )
        elif record["status"] == "error":
            summary["errors"].append({"key": record["key"], "error": record.get("error", "")})

    summary["renamed"] = renamed
    if not opts.dry_run and state:
        try:
            save_state(opts.state_path, state)
        except OSError as exc:
            summary["errors"].append({"stage": "state", "error": str(exc)})
    return summary


def emit(summary):
    """Print one JSON line."""
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def run_loop(opts):
    """Daemon mode: run a cycle, sleep, repeat. Backs off when unreachable."""
    stop = {"flag": False}

    def handler(signum, frame):  # noqa: ARG001 - signal handler signature
        stop["flag"] = True

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass

    while not stop["flag"]:
        try:
            summary = run_cycle(opts)
        except Exception as exc:  # keep the daemon alive
            summary = {"at": now_iso(), "error": "cycle failed: %s" % exc, "unreachable": False}
        emit(summary)
        delay = BACKOFF_SECONDS if summary.get("unreachable") else opts.loop
        waited = 0.0
        while waited < delay and not stop["flag"]:
            time.sleep(min(1.0, delay - waited))
            waited += 1.0
    return 0


# ---------------------------------------------------------------- cli


class Options:
    """Runtime options for one worker process."""

    def __init__(self, url=DEFAULT_URL, max_sessions=DEFAULT_MAX, retitle=False,
                 dry_run=False, state_file=None, cmd=None):
        self.url = url
        self.max = max_sessions
        self.retitle = retitle
        self.dry_run = dry_run
        self.state_path = state_file or state_path()
        self.cmd = cmd


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single cycle and exit (default)",
    )
    parser.add_argument(
        "--loop",
        nargs="?",
        type=int,
        const=DEFAULT_LOOP_SECONDS,
        default=None,
        metavar="SECONDS",
        help="keep running, sleeping SECONDS between cycles (default %d)" % DEFAULT_LOOP_SECONDS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the proposed titles as JSON and change nothing",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX,
        metavar="N",
        help="rename at most N sessions per cycle (default %d)" % DEFAULT_MAX,
    )
    parser.add_argument(
        "--retitle",
        action="store_true",
        help="also re-title sessions the worker named before, when their transcript grew by 40%% or more",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CCC_NAV_URL", DEFAULT_URL),
        help="navigator base URL (default: %s, env: CCC_NAV_URL)" % DEFAULT_URL,
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max < 0:
        parser.error("--max must be zero or greater")
    if args.loop is not None and args.loop <= 0:
        parser.error("--loop must be greater than zero")

    opts = Options(
        url=args.url,
        max_sessions=args.max,
        retitle=args.retitle,
        dry_run=args.dry_run,
    )
    if args.loop is not None:
        opts.loop = args.loop
        return run_loop(opts)
    emit(run_cycle(opts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
