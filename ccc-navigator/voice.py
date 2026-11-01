"""Ride mode: the navigator spoken aloud, for the phone while the owner is on a bike.

The Mac does the heavy lifting so the phone only plays and records:
  - speech: Kokoro-82M (local ONNX) in its own venv, kept warm in a worker process
  - listening: Whisper (mlx-whisper, local) in its own venv, also kept warm
  - the briefing: open decisions, then sessions waiting on the owner, rewritten for the ear

Nothing leaves the Mac. Audio is cached by text hash under ~/.ccc-navigator/voice/.
Engines are optional: a missing venv makes /api/voice/* answer 503, the rest of the
navigator does not notice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

LOCAL = Path.home() / ".local" / "share"
KOKORO_PY = Path(os.environ.get("CCC_NAV_KOKORO_PY", LOCAL / "kokoro-venv" / "bin" / "python"))
KOKORO_DIR = Path(os.environ.get("CCC_NAV_KOKORO_DIR", LOCAL / "kokoro"))
WHISPER_PY = Path(os.environ.get("CCC_NAV_WHISPER_PY", LOCAL / "whisper-venv" / "bin" / "python"))
WHISPER_MODEL = os.environ.get("CCC_NAV_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
VOICE = os.environ.get("CCC_NAV_VOICE", "af_heart")
SPEED = float(os.environ.get("CCC_NAV_VOICE_SPEED", "1.1"))
HEBREW_VOICE = os.environ.get("CCC_NAV_HEBREW_VOICE", "Carmit")
CACHE_MAX = 400
MAX_TEXT = 4000
ID_RE = re.compile(r"^[0-9a-f]{20}$")

# ---------------------------------------------------------------- workers

KOKORO_WORKER = r"""
import json, sys
import numpy as np, soundfile as sf
from kokoro_onnx import Kokoro
d = sys.argv[1]
k = Kokoro(d + "/kokoro-v1.0.onnx", d + "/voices-v1.0.bin")
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    try:
        job = json.loads(line)
        s, sr = k.create(job["text"], voice=job["voice"], speed=job["speed"], lang="en-us")
        sf.write(job["out"], np.asarray(s), sr)
        print(json.dumps({"ok": True}), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:300]}), flush=True)
"""

WHISPER_WORKER = r"""
import json, sys
import numpy as np
import mlx_whisper
model = sys.argv[1]
mlx_whisper.transcribe(np.zeros(8000, dtype=np.float32), path_or_hf_repo=model)
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    try:
        job = json.loads(line)
        kw = {"path_or_hf_repo": model, "condition_on_previous_text": False, "temperature": 0.0}
        if job.get("lang"):
            kw["language"] = job["lang"]
        if job.get("prompt"):
            kw["initial_prompt"] = job["prompt"]
        r = mlx_whisper.transcribe(job["in"], **kw)
        print(json.dumps({"ok": True, "text": r.get("text", "").strip(), "lang": r.get("language", "")}), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:300]}), flush=True)
"""


class VoiceUnavailable(RuntimeError):
    pass


class Worker:
    """One long-lived helper process speaking JSON lines; restarted if it dies. One job at a time."""

    def __init__(self, python, code, arg, name):
        self.python, self.code, self.arg, self.name = Path(python), code, str(arg), name
        self.proc = None
        self.lock = threading.Lock()

    def available(self):
        return self.python.exists()

    def _start(self):
        if not self.available():
            raise VoiceUnavailable("%s is not installed (%s missing)" % (self.name, self.python))
        self.proc = subprocess.Popen([str(self.python), "-c", self.code, self.arg], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        ready = self.proc.stdout.readline()
        if not ready:
            self.proc = None
            raise VoiceUnavailable("%s failed to start" % self.name)

    def run(self, job):
        with self.lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(json.dumps(job) + "\n")
                    self.proc.stdin.flush()
                    line = self.proc.stdout.readline()
                    if line:
                        return json.loads(line)
                except (BrokenPipeError, OSError, ValueError):
                    pass
                self.proc = None
            raise VoiceUnavailable("%s stopped answering" % self.name)


class Voice:
    def __init__(self, cache_dir="~/.ccc-navigator/voice"):
        self.dir = Path(cache_dir).expanduser()
        self.tts = Worker(KOKORO_PY, KOKORO_WORKER, KOKORO_DIR, "Kokoro")
        self.stt = Worker(WHISPER_PY, WHISPER_WORKER, WHISPER_MODEL, "Whisper")

    def status(self):
        return {"speech": self.tts.available(), "listening": self.stt.available(), "voice": VOICE, "speed": SPEED}

    # ---- speech
    def audio_id(self, text, voice=None, speed=None):
        key = "%s|%s|%s" % (voice or VOICE, speed or SPEED, text)
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    def path(self, aid):
        return self.dir / (aid + ".mp3") if ID_RE.match(aid or "") else None

    def say(self, text, voice=None, speed=None):
        """Text -> cached mp3 id. Speaks the text as given; run it through spoken() first."""
        text = str(text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("nothing to say")
        aid = self.audio_id(text, voice, speed)
        out = self.path(aid)
        if out.exists():
            return aid
        self.dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for i, (lang, chunk) in enumerate(by_language(text)):
                wav = Path(tmp) / ("%03d.wav" % i)
                if lang == "he":      # Kokoro has no Hebrew; the Mac's own Hebrew voice reads those sentences
                    aiff = wav.with_suffix(".aiff")
                    subprocess.run(["say", "-v", HEBREW_VOICE, "-r", "195", "-o", str(aiff), chunk], check=True, timeout=60)
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "24000", "-ac", "1", str(wav)],
                                   check=True, timeout=60)
                else:
                    r = self.tts.run({"text": chunk, "voice": voice or VOICE, "speed": speed or SPEED, "out": str(wav)})
                    if not r.get("ok"):
                        raise VoiceUnavailable("Kokoro: " + str(r.get("error")))
                parts.append(wav)
            lst = Path(tmp) / "list.txt"
            lst.write_text("".join("file '%s'\n" % w for w in parts))
            part = out.with_suffix(".part.mp3")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-ar", "24000", "-ac", "1", "-b:a", "64k", str(part)], check=True, timeout=120)
            os.replace(part, out)
        self._trim()
        return aid

    def _trim(self):
        try:
            files = sorted(self.dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            for p in files[:-CACHE_MAX]:
                p.unlink()
        except OSError:
            pass

    # ---- listening
    def hear(self, data, hint="", lang=""):
        """Recorded audio bytes (any format ffmpeg reads) -> transcript dict."""
        if not data:
            raise ValueError("no audio")
        with tempfile.TemporaryDirectory() as tmp:
            src, wav = Path(tmp) / "in.bin", Path(tmp) / "in.wav"
            src.write_bytes(data)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                           check=True, timeout=60)
            # a fixed language skips Whisper's language detection, which roughly halves the wait
            r = self.stt.run({"in": str(wav), "prompt": hint[:600], "lang": lang if lang in ("en", "he") else ""})
        if not r.get("ok"):
            raise VoiceUnavailable("Whisper: " + str(r.get("error")))
        return {"text": r.get("text", ""), "lang": r.get("lang", "")}


# ---------------------------------------------------------------- text for the ear

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"(?<![\w])(?:~|\.{0,2})?/(?:[\w.@-]+/)+[\w.@-]*")
_FILE = re.compile(r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|json|md|html|css|sh|yml|yaml|toml|txt|plist)(?::\d+)?\b")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.M)


def spoken(text, limit=900):
    """Markdown from an agent -> sentences worth hearing: no code, paths, links, hashes or markup."""
    t = str(text or "")
    t = _FENCE.sub(" ", t)
    t = _TABLE.sub(" ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)          # [label](link) -> label
    t = _URL.sub(" a link ", t)
    t = re.sub(r"`([^`\n]{1,30})`", lambda m: m.group(1) if " " in m.group(1) or not re.search(r"[/._(){}=]", m.group(1)) else " ", t)
    t = re.sub(r"`[^`\n]*`", " ", t)
    t = _PATH.sub(" ", t)
    t = re.sub(r"\b(?:[\w.-]+/)+[\w.-]*\.(?:py|js|ts|tsx|jsx|json|md|html|css|sh|yml|yaml|toml|txt|plist)\b", " ", t)
    t = _FILE.sub(" ", t)
    t = _SHA.sub(" ", t)
    t = re.sub(r"\b(?:PR|pr)\s*#(\d+)", r"change \1", t)
    t = re.sub(r"\(\s*#\d+\s*\)|(?<![\w&])#\d+\b", " ", t)                       # issue numbers
    t = re.sub(r"\b(?:feat|fix|chore|claude|copilot|release|hotfix|refs/heads)/[\w./-]+", " ", t)   # branch names
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)               # headings
    t = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "\n", t)      # list markers -> sentence breaks
    t = re.sub(r"\*\*|__|\*|_(?=\w)|(?<=\w)_|~~|>", "", t)
    t = re.sub(r"\bTL;?DR:?\s*", "", t, flags=re.I)
    t = t.replace("—", ", ").replace("–", ", ").replace("→", " to ").replace("&", " and ")
    lines = [re.sub(r"\s+", " ", ln).strip(" :;,-") for ln in t.splitlines()]
    out = []
    for ln in lines:
        if len(re.sub(r"[^A-Za-z֐-׿]", "", ln)) < 3:
            continue
        out.append(ln if re.search(r"[.!?:]$", ln) else ln + ".")
    s = " ".join(out)
    s = re.sub(r"\b(?:commit|file|path|at|in|see|from|on|to)\s*(?=\s*[.,;()])", "", s)   # words whose object was stripped
    s = re.sub(r"\(\s*[,;\s]*\)", "", s)
    s = re.sub(r"\s*,\s*\)", ")", s)
    s = re.sub(r"\s+([.,!?)])", r"\1", s)
    s = re.sub(r"([.,!?])[.,]+", r"\1", s)
    if len(s) > limit:
        cut = s[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        s = cut[:end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "."
    return s.strip()


def first_sentence(text, limit=220):
    s = spoken(text, limit=limit * 2)
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "."


HEB, LAT = re.compile(r"[\u0590-\u05FF]"), re.compile(r"[A-Za-z]")


def by_language(text):
    """Split into runs of whole sentences, each tagged 'he' or 'en' by which script most of its letters use."""
    runs = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sent:
            continue
        lang = "he" if len(HEB.findall(sent)) > len(LAT.findall(sent)) else "en"
        if runs and runs[-1][0] == lang:
            runs[-1] = (lang, runs[-1][1] + " " + sent)
        else:
            runs.append((lang, sent))
    return runs


# ---------------------------------------------------------------- what is risky

RISKY = re.compile(r"\b(buy|pay|paid|purchase|subscribe|\$\d|merge|deploy|live|production|prod|publish|post|send|"
                   r"email|delete|remove|drop|wipe|revoke|rotate|sign|contract|invoice|charge|transfer|release|launch)\b",
                   re.I)


def needs_confirm(question, choice=""):
    """An answer that can be acted on irreversibly is held for a tap at a stop, never sent from the road.
    A 'no', 'later' or 'skip' is always safe."""
    if re.fullmatch(r"\s*(no|not now|later|skip|wait|hold)\s*", str(choice or ""), re.I):
        return False
    return bool(RISKY.search(str(question or "")) or RISKY.search(str(choice or "")))


# ---------------------------------------------------------------- the briefing

def _title(item):
    t = item.get("nick") or item.get("original") or item.get("ccc_label") or "a session"
    t = re.sub(r"\s*\(fork\)\s*", " ", str(t))
    return spoken(t, limit=80).rstrip(".") or "a session"


def waiting_sessions(tree, limit=6):
    """Sessions that need the owner, starred first, newest first."""
    rows = []
    for repo in tree.get("repos") or []:
        for g in repo.get("groups") or []:
            for it in g.get("items") or []:
                if it.get("state") == "needs-you" and not it.get("moved"):
                    starred = bool(it.get("pinned") or g.get("pinned") or repo.get("pinned"))
                    rows.append((not starred, -float(it.get("mtime") or 0), repo.get("name") or "", it))
    rows.sort(key=lambda r: r[:2])
    return [(r[2], r[3]) for r in rows[:limit]], max(0, len(rows) - limit)


def _numbered(n):
    names = "One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen".split()
    return names[n] if n < len(names) else str(n + 1)


def briefing(tree, inbox_list, last_text=None, max_decisions=5, max_sessions=6):
    """-> {"intro": str, "items": [...], "outro": str}. last_text(key) gives a session's newest agent message."""
    decs = (inbox_list or {}).get("open") or []
    decs = sorted(decs, key=lambda d: (d.get("rank") or 99, d.get("priority") or 9))[:max_decisions]
    sessions, more = waiting_sessions(tree, max_sessions)
    items = []
    for d in decs:
        opts = [str(o) for o in (d.get("options") or [])][:6]
        q = spoken(d.get("question"), limit=300)
        ask = q
        if len(opts) == 1:
            ask += " Say %s when it's done, or later." % opts[0]
        elif opts and [o.lower() for o in opts] != ["yes", "no"]:
            ask += " Options: " + ", ".join(opts[:-1]) + " or " + opts[-1] + "."
        items.append({"kind": "decision", "id": d.get("id"), "options": opts, "question": d.get("question"),
                      "confirm": needs_confirm(d.get("question")), "say": ask,
                      "lane": d.get("lane") or ""})
    for repo, it in sessions:
        name, repo = _title(it), spoken(repo, limit=60).rstrip(".")
        reason = spoken(it.get("needs_reason") or "", limit=160)
        latest = last_text(it.get("key")) if last_text else ""
        head = first_sentence(latest) if latest else ""
        say = "%s, in %s, is waiting for you." % (name, repo) if repo and repo.lower() not in name.lower() else \
              "%s is waiting for you." % name
        if head:
            say += " " + head
        elif reason:
            say += " " + reason
        items.append({"kind": "session", "key": it.get("key"), "engine": it.get("engine"), "title": name,
                      "repo": repo, "say": say, "more": spoken(latest, limit=1400) if latest else ""})
    for i, it in enumerate(items):
        it["say"] = _numbered(i) + ". " + it["say"]
    nd, ns = len(decs), len(sessions)
    if not items:
        intro = "All clear. Nothing needs you right now."
    else:
        parts = []
        if nd:
            parts.append("%d decision%s" % (nd, "" if nd == 1 else "s"))
        if ns:
            parts.append("%d session%s waiting" % (ns, "" if ns == 1 else "s"))
        intro = "%d thing%s need%s you: %s." % (len(items), "" if len(items) == 1 else "s",
                                               "s" if len(items) == 1 else "", " and ".join(parts))
    outro = ("That's everything. %d more sessions are waiting on the screen." % more) if more else \
            ("That's everything." if items else "")
    return {"intro": intro, "items": items, "outro": outro, "generated_at": time.time()}


# ---------------------------------------------------------------- what the owner said

_WORDS = {
    "yes": r"yes|yeah|yep|sure|ok(ay)?|approve[d]?|go ahead|do it|כן|בטח|יאללה",
    "no": r"no|nope|nah|reject|don'?t|do not|לא",
    "later": r"later|not now|snooze|skip( it)?|wait|אחר כך|תדלג",
    "repeat": r"repeat|again|say (that|it) again|what\??|pardon|שוב|תחזור",
    "next": r"next|move on|הבא",
    "back": r"back|previous|go back",
    "more": r"more|tell me more|details|read (it|more|the rest)|what did (it|they) say|עוד|פרטים",
    "stop": r"stop|pause|quiet|shut up|enough|עצור|די",
    "cancel": r"cancel|scratch that|never ?mind|undo|בטל",
    "brief": r"(brief|briefing|update|status|start over|what'?s new|from the top)",
}


def parse(text):
    """Transcript -> {"intent": ..., ...}. Short utterances are commands; anything else is a message.
    'talk to X' / 'tell X ...' address a session by name."""
    raw = str(text or "").strip()
    t = re.sub(r"[^\w\s'֐-׿?]", " ", raw.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return {"intent": "none"}
    m = re.match(r"^(?:talk|speak|switch|go) to (.+)$", t)
    if m:
        return {"intent": "talk", "target": m.group(1).strip()}
    m = re.match(r"^(?:tell|ask|message) (.+?) (?:that|to) (.+)$", t) or re.match(r"^(?:tell|ask|message) (\S+) (.+)$", t)
    if m and len(m.group(1).split()) <= 4 and len(t.split()) > 3:
        n = len(m.group(1).split())
        said = raw.split(None, 1 + n)[-1]
        return {"intent": "tell", "target": m.group(1).strip(), "text": re.sub(r"^(?:that|to)\s+", "", said, flags=re.I)}
    words = t.split()
    if len(words) <= 4:
        for intent, pat in _WORDS.items():
            if re.fullmatch(r"(?:(?:please|ok|okay) )?(?:%s)(?: please| thanks| thank you)?" % pat, t):
                return {"intent": intent}
        m = re.fullmatch(r"(?:option )?(one|two|three|four|five|1|2|3|4|5|first|second|third)", t)
        if m:
            n = {"one": 1, "1": 1, "first": 1, "two": 2, "2": 2, "second": 2, "three": 3, "3": 3, "third": 3,
                 "four": 4, "4": 4, "five": 5, "5": 5}[m.group(1)]
            return {"intent": "option", "n": n}
    return {"intent": "message", "text": raw}


def match_option(text, options):
    """An answer to a decision: index into options, or None. Accepts yes/no and option words."""
    p = parse(text)
    opts = [str(o).lower() for o in options or []]
    if p["intent"] == "option" and 1 <= p["n"] <= len(opts):
        return p["n"] - 1
    if p["intent"] in ("yes", "no") and p["intent"] in opts:
        return opts.index(p["intent"])
    if p["intent"] == "yes" and len(opts) == 1:
        return 0
    t = str(text or "").lower()
    hits = [i for i, o in enumerate(opts) if o and re.search(r"\b%s\b" % re.escape(o), t)]
    return hits[0] if len(hits) == 1 else None


def _compact(text):
    return re.sub(r"[^a-z0-9\u0590-\u05FF]", "", str(text or "").lower().replace("'s", "s"))


def find_session(target, tree):
    """Spoken name -> best (key, title, repo) among recent sessions, or None.
    Spoken names come out of speech-to-text split or misspelt ("boards bridge", "bord's bridge"),
    so names are compared with spaces and punctuation removed, and fuzzily."""
    import difflib
    want = _compact(re.sub(r"\b(the|session|agent|project|in)\b", " ", str(target or "").lower()))
    if len(want) < 2:
        return None
    best, best_score = None, 0.0
    for repo in tree.get("repos") or []:
        rname = str(repo.get("name") or "")
        for g in repo.get("groups") or []:
            for it in g.get("items") or []:
                title = _title(it)
                score = 0.0
                for name, weight in ((title, 1.0), (rname, 0.9), (repo.get("orig_name"), 0.9), (g.get("name"), 0.7)):
                    c = _compact(name)
                    if not c:
                        continue
                    if want == c:
                        sc = 1.0
                    elif len(want) >= 3 and (want in c or c in want):
                        sc = 0.85
                    else:
                        sc = difflib.SequenceMatcher(None, want, c).ratio()
                    score = max(score, sc * weight)
                if score < 0.6:
                    continue
                if it.get("engine") == "outsourcerer":      # background jobs cannot be talked to
                    continue
                score += 0.05 if it.get("state") == "needs-you" else 0
                score += 0.03 if it.get("engine") in ("claude", "codex") else 0
                score += min(float(it.get("mtime") or 0), 4e9) / 1e12     # newest wins a tie
                if score > best_score:
                    best, best_score = (it.get("key"), title, rname), score
    return best


def names_hint(tree, limit=40):
    """Project and session names, given to Whisper so it spells them right."""
    seen = []
    for repo in tree.get("repos") or []:
        for n in [repo.get("name")] + [_title(it) for g in repo.get("groups") or [] for it in g.get("items") or []][:3]:
            if n and n not in seen:
                seen.append(n)
    return ", ".join(seen[:limit])
