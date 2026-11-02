#!/usr/bin/env python3
"""One small, cheap model call for the navigator: Groq first, Gemini second.

stdlib only, python 3.9+. Keys come from the environment or from
~/.ccc-navigator/llm.env (KEY=value lines, mode 0600). Nothing is sent
anywhere when NAV_LLM=off or no key is present: complete() returns None and
callers fall back to their local rules.

A provider that answers 429 (or fails) is skipped for a cool-down; a daily
budget per provider is kept in ~/.ccc-navigator/llm-usage.json so the free
tiers are never exhausted. Prompt text is never logged.

  python3 llm.py "say hi"          # quick check, prints provider and reply
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser("~/.ccc-navigator"))
ENV_FILE = Path(os.environ.get("NAV_LLM_ENV", str(HOME / "llm.env")))
USAGE_FILE = Path(os.environ.get("NAV_LLM_USAGE", str(HOME / "llm-usage.json")))
TIMEOUT = 25

PROVIDERS = (
    {"name": "groq", "key": "GROQ_API_KEY", "model": os.environ.get("NAV_GROQ_MODEL", "openai/gpt-oss-20b"),
     "daily": int(os.environ.get("NAV_GROQ_DAILY", 800))},
    {"name": "gemini", "key": "GEMINI_API_KEY", "model": os.environ.get("NAV_GEMINI_MODEL", "gemini-flash-lite-latest"),
     "daily": int(os.environ.get("NAV_GEMINI_DAILY", 400))},
)
MIN_GAP = 2.0          # seconds between two calls to the same provider
COOLDOWN = 300         # seconds a failing provider is skipped

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

_lock = threading.Lock()
_last_call = {}
_down_until = {}


def strip_secrets(text):
    """Remove obvious secrets (API keys, bearer tokens, long hex/base64) and personal data."""
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _keys():
    keys = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip('"\'')
    except OSError:
        pass
    for p in PROVIDERS:
        if os.environ.get(p["key"]):
            keys[p["key"]] = os.environ[p["key"]]
    return keys


def enabled():
    """True when at least one provider has a key and the owner did not switch it off."""
    if os.environ.get("NAV_LLM", "").lower() in ("off", "0", "no"):
        return False
    keys = _keys()
    return any(keys.get(p["key"]) for p in PROVIDERS)


def _usage():
    try:
        data = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("day") != time.strftime("%Y-%m-%d"):
        data = {"day": time.strftime("%Y-%m-%d"), "calls": {}}
    return data


def _count(name, ok):
    data = _usage()
    c = data["calls"].setdefault(name, {"ok": 0, "fail": 0})
    c["ok" if ok else "fail"] += 1
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, USAGE_FILE)
    except OSError:
        pass


def usage():
    """Today's call counts per provider (no content)."""
    return _usage()


def _ssl_context():
    """Default trust store, plus macOS's own bundle: a python.org/Homebrew python may ship without one."""
    ctx = ssl.create_default_context()
    for cafile in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"):
        if os.path.exists(cafile):
            try:
                ctx.load_verify_locations(cafile)
            except (OSError, ssl.SSLError):
                pass
    return ctx


_SSL = _ssl_context()


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as r:
        return json.loads(r.read())


def _groq(model, key, system, prompt, max_tokens, as_json):
    body = {"model": model, "temperature": 0.2, "max_completion_tokens": max_tokens + 400,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    if as_json:
        body["response_format"] = {"type": "json_object"}
    res = _post("https://api.groq.com/openai/v1/chat/completions", body,
                {"Authorization": "Bearer " + key, "User-Agent": "ccc-navigator"})
    return ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _gemini(model, key, system, prompt, max_tokens, as_json):
    cfg = {"temperature": 0.2, "maxOutputTokens": max_tokens + 400, "thinkingConfig": {"thinkingLevel": "minimal"}}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
    res = _post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model, body,
                {"x-goog-api-key": key})
    parts = (((res.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
    return "".join(p.get("text") or "" for p in parts)


CALLERS = {"groq": _groq, "gemini": _gemini}


def complete(prompt, system="You are concise.", max_tokens=200, as_json=False):
    """(provider, reply) from the first provider that answers, or None. The prompt is secret-stripped first."""
    if not enabled():
        return None
    keys, prompt = _keys(), strip_secrets(prompt)
    used = _usage()["calls"]
    for p in PROVIDERS:
        name, key = p["name"], keys.get(p["key"])
        if not key or time.time() < _down_until.get(name, 0):
            continue
        if used.get(name, {}).get("ok", 0) + used.get(name, {}).get("fail", 0) >= p["daily"]:
            continue
        with _lock:
            wait = MIN_GAP - (time.time() - _last_call.get(name, 0))
            if wait > 0:
                time.sleep(wait)
            _last_call[name] = time.time()
        try:
            reply = (CALLERS[name](p["model"], key, system, prompt, max_tokens, as_json) or "").strip()
        except urllib.error.HTTPError as exc:
            retry = exc.headers.get("retry-after") if exc.headers else None
            _down_until[name] = time.time() + (float(retry) if retry and retry.isdigit() else COOLDOWN)
            _count(name, False)
            continue
        except Exception:
            _down_until[name] = time.time() + 60
            _count(name, False)
            continue
        _count(name, bool(reply))
        if reply:
            return name, reply
    return None


def parse_json(reply):
    """First JSON object in a model reply, or {}."""
    if not reply:
        return {}
    text = str(reply)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


if __name__ == "__main__":
    res = complete(" ".join(sys.argv[1:]) or "Reply with the single word: ok", max_tokens=20)
    print(json.dumps({"enabled": enabled(), "result": res, "usage": usage()}))
