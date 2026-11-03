#!/usr/bin/env python3
"""CCC_CLAUDE_BIN shim: send session-title jobs to Groq's free tier, pass everything else to Claude Code.

Command Center names sessions by running `claude -p --model <haiku> "Produce a concise 4-8 word title..."`.
Each run leaves a Claude session behind, and Codex Desktop then imports those as Codex threads, which
burns Codex credits for no work. This shim answers only that one prompt from Groq (key from Doppler,
secret GROQ_FREE) and leaves no session behind. Any other call is exec'd into the real claude unchanged.

Privacy: the opening prompt of the session being titled (first 3000 characters) is sent to Groq.
Not Groq's paid tier, so assume free-tier terms apply. Remove CCC_CLAUDE_BIN from the CCC launchd plist to revert.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REAL = os.environ.get("CCC_REAL_CLAUDE") or os.path.expanduser("~/.local/bin/claude")
DOPPLER = os.environ.get("CCC_DOPPLER_BIN") or "/opt/homebrew/bin/doppler"
SECRET = ("GROQ_FREE", "shared-marketing", "prd")
MODEL = os.environ.get("CCC_TITLE_MODEL") or "openai/gpt-oss-20b"
TITLE_PREFIX = "Produce a concise 4-8 word title"
MAX_PROMPT_CHARS = 3000


def groq_key():
    name, project, config = SECRET
    out = subprocess.run([DOPPLER, "secrets", "get", name, "--project", project, "--config", config, "--plain"],
                         capture_output=True, text=True, timeout=20)
    key = (out.stdout or "").strip()
    if out.returncode != 0 or not key:
        raise RuntimeError("could not read %s from Doppler" % name)
    return key


def clip(prompt):
    # keep the instruction, shorten only the opening prompt that follows "Opening prompt:"
    head, sep, tail = prompt.partition("Opening prompt:\n")
    if not sep:
        return prompt[:MAX_PROMPT_CHARS]
    body, sep2, end = tail.rpartition("\n\nTitle:")
    if not sep2:
        body, end = tail, ""
    return head + sep + body[:MAX_PROMPT_CHARS] + ("\n\nTitle:" if sep2 else "")


def groq_title(prompt):
    key = groq_key()
    payload = json.dumps({"model": MODEL, "temperature": 0.2, "max_completion_tokens": 300, "reasoning_effort": "low",
                          "messages": [{"role": "user", "content": clip(prompt)}]}).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=payload, method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "User-Agent": "ccc-title-shim/1.0"})
    last = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = json.load(r)["choices"][0]["message"]["content"]
            break
        except urllib.error.HTTPError as e:
            last = "Groq HTTP %s" % e.code
            if e.code == 429 and attempt == 1:
                time.sleep(4)
                continue
            raise RuntimeError(last)
    else:
        raise RuntimeError(last or "Groq failed")
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip().strip('"').strip("'").rstrip(".")
        if line:
            return line[:120]
    raise RuntimeError("empty reply")


def main():
    args = sys.argv[1:]
    if "-p" in args and args and args[-1].startswith(TITLE_PREFIX):
        try:
            print(groq_title(args[-1]))
            return 0
        except Exception as e:  # CCC logs the error and retries later; never fall back to a paid engine
            print("ccc-claude-shim: %s" % str(e)[:200], file=sys.stderr)
            return 1
    os.execv(REAL, [REAL] + args)


if __name__ == "__main__":
    sys.exit(main())
